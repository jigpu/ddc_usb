#!/bin/sh

set -e

if test -f requirements.txt; then
  echo "Refusing to distribute with a pre-existing requirements.txt"
  exit 1
fi

source ./venv/bin/activate
pip3 freeze --local > requirements.txt


COMMIT_ID=$(git describe --abbrev HEAD)
git archive HEAD \
    --prefix=ddc_usb/ \
    --add-file requirements.txt \
    -o "ddc_usb-${COMMIT_ID}.tar.gz" \
    ":!dist.sh"

rm requirements.txt
