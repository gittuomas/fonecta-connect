#!/bin/sh
# Python choice: $PYTHON, then a local engine/.venv, then python3 on PATH.
set -eu
ENGINE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -n "${PYTHON:-}" ]; then
  ENGINE_PYTHON=$PYTHON
elif [ -x "$ENGINE_DIR/.venv/bin/python" ]; then
  ENGINE_PYTHON="$ENGINE_DIR/.venv/bin/python"
else
  ENGINE_PYTHON=python3
fi
exec "$ENGINE_PYTHON" "$ENGINE_DIR/run.py" "$@"
