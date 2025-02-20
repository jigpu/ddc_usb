#!/bin/sh

cd -- "$(dirname -- "$0")"
source ./venv/bin/activate
./ddc_usb.py "$@"
