#!/bin/sh
# 统一入口启动脚本（独立包，可整体复制到其他目录/机器运行）。
# 运行时：系统 python3（需 3.11+），可用 PYTHON=/path/to/python3 指定解释器。
# 可选依赖：requests（AnySearch CLI 用）、aiotieba（贴吧深挖用），见 README「依赖与平台」。
# 用法：./run.sh source-adapter <命令>   或   ./run.sh <命令>
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "找不到 Python 解释器：$PYTHON" >&2
  echo "请安装 Python 3.11+，或用 PYTHON=/path/to/python3 指定解释器。" >&2
  exit 2
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "需要 Python 3.11+，当前版本：$("$PYTHON" -V 2>&1)" >&2
  exit 2
fi

export PYTHONPATH="$ROOT/adapter${PYTHONPATH:+:$PYTHONPATH}"
export ANYSEARCH_SKILL_DIR="$ROOT/skills/anysearch"

# 兼容 ./run.sh source-adapter <命令> 写法
if [ "${1:-}" = "source-adapter" ]; then
  shift
fi

exec "$PYTHON" -m source_adapter "$@"
