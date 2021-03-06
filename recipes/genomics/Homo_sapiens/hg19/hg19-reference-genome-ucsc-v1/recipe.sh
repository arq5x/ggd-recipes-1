#!/bin/sh
set -eo pipefail -o nounset
## Get the latest hg19 reference genome for USCS. (Patch 13) 
wget --quiet -O hg19-reference-genome-ucsc-v1.fa.gz http://hgdownload.cse.ucsc.edu/goldenPath/hg19/bigZips/hg19.fa.gz

## Unzip file using gzip
gzip -fd hg19-reference-genome-ucsc-v1.fa.gz

## Index the fasta file using samtools 
samtools faidx hg19-reference-genome-ucsc-v1.fa
