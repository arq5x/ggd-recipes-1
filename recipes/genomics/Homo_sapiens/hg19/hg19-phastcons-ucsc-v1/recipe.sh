#!/bin/sh
set -eo pipefail -o nounset
wget --quiet --no-check-certificate --output-document hg19-phastcons-ucsc-v1.bw http://hgdownload.cse.ucsc.edu/goldenpath/hg19/phastCons100way/hg19.100way.phastCons.bw
