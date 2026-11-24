#!/usr/bin/env bash
# 统一中文社区来源适配器：一键验收脚本
#
# 依次执行：单元测试 → 提供器状态 → 贴吧公开搜索烟测 → AnySearch 烟测
#           → Just One API 调用清单 → 验证报告（verification/latest.json）
#
# 全程只读、匿名；不触发任何付费 API 调用。

set -uo pipefail

PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PKG_ROOT}" || exit 2

PYTHON="${PYTHON:-python3}"
STATUS=0

section() {
  printf '\n=== %s ===\n' "$1"
}

run_step() {
  local label="$1"
  shift
  section "${label}"
  if "$@"; then
    printf '[ok] %s\n' "${label}"
  else
    local code=$?
    printf '[fail:%s] %s\n' "${code}" "${label}"
    # 退出码 3 = --offline 未命中缓存；1 = 未取得证据，均视为烟测失败
    STATUS=1
  fi
}

section "环境"
"${PYTHON}" --version
printf '工作目录：%s\n' "${PKG_ROOT}"

run_step "单元与契约测试" "${PYTHON}" -m unittest discover -s tests -v
run_step "提供器状态（doctor --json）" "${PYTHON}" -m source_adapter doctor --json
run_step "贴吧公开搜索烟测（1 页）" \
  "${PYTHON}" -m source_adapter tieba-search "高中 暗恋" --pages 1 --output verification/tieba-smoke
run_step "AnySearch 匿名烟测（3 条）" \
  "${PYTHON}" -m source_adapter anysearch "高中 暗恋 故事" --max-results 3 --output verification/anysearch-smoke
run_step "Just One API 调用清单（B站）" \
  "${PYTHON}" -m source_adapter justone-manifest bilibili "校园 爱情 故事" --comments --limit 20 \
  --out verification/justone-manifest-bilibili.json
run_step "Just One API 调用清单（知乎）" \
  "${PYTHON}" -m source_adapter justone-manifest zhihu "学生时代的爱情故事" --comments --limit 20 \
  --out verification/justone-manifest-zhihu.json
run_step "验证报告写入 verification/latest.json" "${PYTHON}" -m source_adapter verify --json

printf '\n=== 结果 ===\n'
if [ "${STATUS}" -eq 0 ]; then
  printf '全部验收步骤通过。报告：verification/latest.json\n'
else
  printf '存在失败步骤，请查看上方输出与 verification/latest.json 的 gaps 字段。\n'
fi
exit "${STATUS}"
