#!/bin/sh
# 运行包内全量单元与契约测试。
# 运行时：系统 python3（需 3.11+），可用 PYTHON=/path/to/python3 指定解释器。
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "找不到 Python 解释器：$PYTHON（可用 PYTHON=/path/to/python3 指定）" >&2
  exit 2
fi

cd "$ROOT/adapter"
exec "$PYTHON" -m unittest discover -s tests -v
