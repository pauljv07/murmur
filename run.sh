#!/bin/zsh
# Start Murmur and open it in the browser. First run downloads ~4 GB of models from Hugging Face;
# after that everything runs offline.
cd "$(dirname "$0")"
[ -d .venv ] || { python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
[ -x native/syscap ] || swiftc -O -o native/syscap native/syscap.swift || echo "native audio helper not built (needs Xcode command line tools)"
(sleep 4; open "http://127.0.0.1:${MURMUR_PORT:-8765}") &
exec .venv/bin/python server/app.py
