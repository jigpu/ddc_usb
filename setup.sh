#!/bin/sh

set -e

cd -- "$(dirname -- "$0")"
python3 -m venv venv
source ./venv/bin/activate

if test -f requirements.txt; then
  pip3 install -r requirements.txt
else
  pip3 install serial pyftdi
fi
