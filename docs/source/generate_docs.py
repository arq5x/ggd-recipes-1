import os
import os.path as op
from collections import defaultdict
from jinja2.sandbox import SandboxedEnvironment
from sphinx.util import logging as sphinx_logging
from sphinx.util import status_iterator
from sphinx.util.parallel import ParallelTasks, parallel_available, make_chunks
from sphinx.util.rst import escape as rst_escape
from sphinx.util.osutil import ensuredir
from sphinx.jinja2glue import BuiltinTemplateLoader
from distutils.version import LooseVersion
from bioconda_utils.utils import RepoData, load_config
from bioconda_utils.recipe import Recipe, RecipeError
from typing import Any, Dict, List, Tuple, Optional
from sphinx import addnodes
from docutils import nodes
from docutils.parsers import rst
from docutils.statemachine import StringList
from sphinx.domains import Domain, ObjType, Index
from sphinx.directives import ObjectDescription
from sphinx.environment import BuildEnvironment
from sphinx.roles import XRefRole
from sphinx.util.docfields import Field, GroupedField
from sphinx.util.nodes import make_refnode
from sphinx.util.rst import escape as rst_escape
from sphinx.util.osutil import ensuredir
from sphinx.util.docutils import SphinxDirective
from sphinx.jinja2glue import BuiltinTemplateLoader
from conda.exports import VersionOrder


# Aquire a logger
try:
    logger = sphinx_logging.getLogger(__name__)
except AttributeError:  # not running within sphinx
    import logging
    logger = logging.getLogger(__name__)

try:
    from conda_build.metadata import MetaData
    from conda_build.exceptions import UnableToParse
except Exception:
    logging.exception("Failed to import MetaData")
    raise


BASE_DIR = op.dirname(op.abspath(__file__))
RECIPE_DIR = op.join(op.dirname(BASE_DIR), 'ggd-recipes', 'recipes')
OUTPUT_DIR = op.join(BASE_DIR, 'recipes')




def as_extlink_filter(text):
    """Jinja2 filter converting identifier (list) to extlink format

    Args:
      text: may be string or list of strings

    >>> as_extlink_filter("biotools:abyss")
    "biotools: :biotool:`abyss`"

    >>> as_extlink_filter(["biotools:abyss", "doi:123"])
    "biotools: :biotool:`abyss`, doi: :doi:`123`"
    """
    def fmt(text):
        assert isinstance(text, str), "identifier has to be a string"
        text = text.split(":", 1)
        assert len(text) == 2, "identifier needs at least one colon"
        return "{0}: :{0}:`{1}`".format(*text)

    assert isinstance(text, list), "identifiers have to be given as list"

    return list(map(fmt, text))


def underline_filter(text):
    """Jinja2 filter adding =-underline to row of text

    >>> underline_filter("headline")
    "headline\n========"
    """
    return text + "\n" + "=" * len(text)


def escape_filter(text):
    """Jinja2 filter escaping RST symbols in text

    >>> excape_filter("running `cmd.sh`")
    "running \`cmd.sh\`"
    """
    if text:
        return rst_escape(text)
    return text


class Renderer(object):
    """Jinja2 template renderer

    - Loads and caches templates from paths configured in conf.py
    - Makes additional jinja filters available:
      - underline -- turn text into a RSt level 1 headline
      - escape -- escape RST special characters
      - as_extlink -- convert (list of) identifiers to extlink references
    """
    def __init__(self, app):
        template_loader = BuiltinTemplateLoader()
        template_loader.init(app.builder)
        template_env = SandboxedEnvironment(loader=template_loader)
        template_env.filters['escape'] = escape_filter
        template_env.filters['underline'] = underline_filter
        template_env.filters['as_extlink'] = as_extlink_filter
        self.env = template_env
        self.templates = {}

    def render(self, template_name, context):
        """Render a template file to string

        Args:
          template_name: Name of template file
          context: dictionary to pass to jinja
        """
        try:
            template = self.templates[template_name]
        except KeyError:
            template = self.env.get_template(template_name)
            self.templates[template_name] = template

        return template.render(**context)

    def render_to_file(self, file_name, template_name, context):
        """Render a template file to a file

        Ensures that target directories exist and only writes
        the file if the content has changed.

        Args:
          file_name: Target file name
          template_name: Name of template file
          context: dictionary to pass to jinja

        Returns:
          True if a file was written
        """
        content = self.render(template_name, context)
        # skip if exists and unchanged:
        if os.path.exists(file_name):
            with open(file_name, encoding="utf-8") as f:
                if f.read() == content:
                    return False  # unchanged
        ensuredir(op.dirname(file_name))

        with open(file_name, "wb") as f:
            f.write(content.encode("utf-8"))
        return True


def generate_readme(folder, repodata, renderer):
    """Generates README.rst for the recipe in folder

    Args:
      folder: Toplevel folder name in recipes directory
      repodata: RepoData object
      renderer: Renderer object

    Returns:
      List of template_options for each concurrent version for
      which meta.yaml files exist in the recipe folder and its
      subfolders
    """
    # Subfolders correspond to different versions
    versions = []
    for sf in os.listdir(op.join(RECIPE_DIR, folder)):
        if not op.isdir(op.join(RECIPE_DIR, folder, sf)):
            # Not a folder
            continue
        try:
            LooseVersion(sf)
        except ValueError:
            logger.error("'{}' does not look like a proper version!"
                         "".format(sf))
            continue
        versions.append(sf)

    # Read the meta.yaml file(s)
    try:
        recipe = op.join(RECIPE_DIR, folder, "meta.yaml")
        if op.exists(recipe):
            metadata = MetaData(recipe)
            if metadata.version() not in versions:
                versions.insert(0, metadata.version())
        else:
            if versions:
                recipe = op.join(RECIPE_DIR, folder, versions[0], "meta.yaml")
                metadata = MetaData(recipe)
            else:
                # ignore non-recipe folders
                return []
    except UnableToParse as e:
        logger.error("Failed to parse recipe {}".format(recipe))
        raise e

    ## Get all versions and build numbers for data package
    # Select meta yaml
    meta_fname = op.join(RECIPE_DIR, folder, 'meta.yaml')
    if not op.exists(meta_fname):
        for item in os.listdir(op.join(RECIPE_DIR, folder)):
            dname = op.join(RECIPE_DIR, folder, item)
            if op.isdir(dname):
                fname = op.join(dname, 'meta.yaml')
                if op.exists(fname):
                    meta_fname = fname
                    break
        else:
            logger.error("No 'meta.yaml' found in %s", folder)
            return []
    meta_relpath = meta_fname[len(RECIPE_DIR)+1:]

    # Read the meta.yaml file(s)
    try:
        recipe_object = Recipe.from_file(RECIPE_DIR, meta_fname)
    except RecipeError as e:
        logger.error("Unable to process %s: %s", meta_fname, e)
        return []

    # Format the README
    for package in sorted(list(set(recipe_object.package_names))):
        versions_in_channel = set(repodata.get_package_data(['version', 'build_number'],
                                                            channels='ggd-genomics', name=package))
        sorted_versions = sorted(versions_in_channel,
                                 key=lambda x: (VersionOrder(x[0]), x[1]),
                                 reverse=False)
        if sorted_versions:
            depends = [
                depstring.split(' ', 1) if ' ' in depstring else (depstring, '')
                for depstring in
                repodata.get_package_data('depends', name=package,
                                          version=sorted_versions[0][0],
                                          build_number=sorted_versions[0][1],
                )[0]
            ]
        else:
            depends = []



    # Format the README
    name = metadata.name()
    versions_in_channel = repodata.get_versions(name)

    template_options = {
        'name': name,
        'about': (metadata.get_section('about') or {}),
        'species': (metadata.get_section('about')["identifiers"]["species"] if "species" in metadata.get_section('about')["identifiers"] else {}),
        'genome_build': (metadata.get_section('about')["identifiers"]["genome-build"] if "genome-build" in metadata.get_section('about')["identifiers"] else {}),
        'ggd_channel': (metadata.get_section('about')["tags"]["ggd-channel"] if "ggd-channel" in metadata.get_section('about')["tags"] else "genomics"),
        'extra': (metadata.get_section('extra') or {}),
        'versions': ["-".join(str(w) for w in v) for v in sorted_versions],
        'gh_recipes': 'https://github.com/gogetdata/ggd-recipes/tree/master/recipes/',
        'recipe_path': op.dirname(op.relpath(metadata.meta_path, RECIPE_DIR)),
        'Package': '<a href="recipes/{0}/README.html">{0}</a>'.format(name)
    }

    renderer.render_to_file(
        op.join(OUTPUT_DIR, name, 'README.rst'),
        'readme.rst_t',
        template_options)

    recipes = []
    latest_version = "-".join(str(w) for w in sorted_versions[-1])
    for version, version_info in sorted(versions_in_channel.items()):
        t = template_options.copy()
        if 'noarch' in version_info:
            t.update({
                'Linux': '<i class="fa fa-linux"></i>' if 'linux' in version_info else '<i class="fa fa-dot-circle-o"></i>',
                'OSX': '<i class="fa fa-apple"></i>' if 'osx' in version_info else '<i class="fa fa-dot-circle-o"></i>',
                'NOARCH': '<i class="fa fa-desktop"></i>' if 'noarch' in version_info else '',
                'Version': latest_version ## The latest version
                #'Version': version
            })
        else:
            t.update({
                'Linux': '<i class="fa fa-linux"></i>' if 'linux' in version_info else '',
                'OSX': '<i class="fa fa-apple"></i>' if 'osx' in version_info else '',
                'NOARCH': '<i class="fa fa-desktop"></i>' if 'noarch' in version_info else '',
                'Version': latest_version ## The latest version
                #'Version': version
            })
        recipes.append(t)
    return recipes


def generate_recipes(app):
    """
    Go through every folder in the `ggd-recipes/recipes` dir,
    have a README.rst file generated and generate a recipes.rst from
    the collected data.
    """
    renderer = Renderer(app)
    load_config(os.path.join(os.path.dirname(RECIPE_DIR), "config.yaml"))
    repodata = RepoData()
    # Add ggd channels to repodata object
    #repodata.channels = ['ggd-genomics', 'conda-forge', 'bioconda', 'defaults']
    recipes = []
    ## Get each folder that contains a meat.yaml file
    recipe_dirs = []
    for root, dirs, files in os.walk(RECIPE_DIR):
        if "meta.yaml" in files:
            recipe_dirs.append(root)


    if parallel_available and len(recipe_dirs) > 5:
        nproc = app.parallel
    else:
        nproc = 1

    if nproc == 1:
        for folder in status_iterator(
                recipe_dirs,
                'Generating package READMEs...',
                "purple", len(recipe_dirs), app.verbosity):
            recipes.extend(generate_readme(folder, repodata, renderer))
    else:
        tasks = ParallelTasks(nproc)
        chunks = make_chunks(recipe_dirs, nproc)

        def process_chunk(chunk):
            _recipes = []
            for folder in chunk:
                _recipes.extend(generate_readme(folder, repodata, renderer))
            return _recipes

        def merge_chunk(chunk, res):
            recipes.extend(res)


        for chunk in status_iterator(
                chunks,
                'Generating package READMEs with {} threads...'.format(nproc),
                "purple", len(chunks), app.verbosity):
            tasks.add_task(process_chunk, chunk, merge_chunk)
        logger.info("waiting for workers...")
        tasks.join()

    updated = renderer.render_to_file("source/recipes.rst", "recipes.rst_t", {
        'recipes': recipes,
        # order of columns in the table; must be keys in template_options
        'keys': ['Package', 'Version', 'Linux', 'OSX', 'NOARCH'],
		'noarch_symbol': '<i class="fa fa-desktop"></i>',
		'linux_symbol': '<i class="fa fa-linux"></i>', 
		'osx_symbol': '<i class="fa fa-apple"></i>',
		'dot_symbol': '<i class="fa fa-dot-circle-o"></i>' 
    })
    if updated:
        logger.info("Updated source/recipes.rst")


def setup(app):
    app.connect('builder-inited', generate_recipes)
    return {
        'version': "0.0.0",
        'parallel_read_safe': True,
        'parallel_write_safe': True
    }
