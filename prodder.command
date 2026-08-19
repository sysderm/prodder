#!/bin/sh
# Shell launcher (run as ./prodder.command). Do NOT rely on double-clicking
# this file: Terminal delivers a .command by typing its path into the new
# shell, and this machine's zshrc exec's the asciinema session recorder on
# startup, which flushes queued tty input — the typed line is discarded and
# nothing runs. Double-click Prodder.app instead.
set -eu
cd "$(dirname "$0")"
# /usr/bin/python3 is 3.9 (no tomllib); prefer a Python that can run prodtop.
for PY in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$PY" >/dev/null 2>&1 \
            && "$PY" -c 'import tomllib' >/dev/null 2>&1; then
        exec "$PY" prodtop.py "$@"
    fi
done
echo "prodder: no Python >=3.11 with tomllib found" >&2
exit 1
