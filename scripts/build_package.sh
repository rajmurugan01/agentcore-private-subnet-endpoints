#!/usr/bin/env bash
# Builds the direct-code-deploy zip: main.py at the root plus boto3 and its pure-Python dependencies, targeting Linux arm64.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$HERE/package" "$HERE/probe.zip" && mkdir -p "$HERE/package"
python3 -m pip install --quiet --target "$HERE/package" --platform manylinux2014_aarch64 --python-version 3.13 --only-binary=:all: boto3
cp "$HERE/app/main.py" "$HERE/package/main.py"
( cd "$HERE/package" && zip -q -r "$HERE/probe.zip" . -x '*/__pycache__/*' '*.pyc' )
ls -l "$HERE/probe.zip" | awk '{print "probe.zip bytes:", $5}'
