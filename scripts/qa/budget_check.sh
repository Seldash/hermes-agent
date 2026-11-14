#!/usr/bin/env bash
# 产物体积预算门：对构建产物做 gzip 尺寸断言，超限非零退出。
#
# 用法：bash budget_check.sh <dist_dir> [总预算KB=700] [单块预算KB=250]
#
# 为什么要单独一条：vite 只报单文件原始体积，不卡总量、不卡 gzip，
# 也没有「不许涨」的机制。本脚本用 gzip -c 实测每个文件，按目录合计。
# cost: 全部 JS 合计 gzip 超过总预算，或任一单块超过单块预算 -> exit 1
set -uo pipefail
DIST="${1:?用法: budget_check.sh <dist_dir> [总预算KB] [单块预算KB]}"
TOTAL_BUDGET="${2:-700}"
CHUNK_BUDGET="${3:-250}"

[ -d "$DIST" ] || { echo "目录不存在: $DIST" >&2; exit 2; }

total=0
fail=0
printf '%-46s %10s %10s\n' "文件" "原始KB" "gzip-KB"
while IFS= read -r f; do
  raw=$(stat -c %s "$f")
  gz=$(gzip -c "$f" | wc -c)
  rawk=$((raw / 1024)); gzk=$((gz / 1024))
  total=$((total + gzk))
  flag=""
  if [ "$gzk" -gt "$CHUNK_BUDGET" ]; then flag="  <== 超单块预算 ${CHUNK_BUDGET}KB"; fail=1; fi
  printf '%-46s %10d %10d%s\n' "${f#"$DIST"/}" "$rawk" "$gzk" "$flag"
 done < <(find "$DIST" \( -name '*.js' -o -name '*.css' \) -type f | sort)

printf '\n合计 gzip: %d KB（预算 %d KB）\n' "$total" "$TOTAL_BUDGET"
if [ "$total" -gt "$TOTAL_BUDGET" ]; then
  echo "超出总量预算 $((total - TOTAL_BUDGET)) KB" >&2
  fail=1
fi
[ "$fail" -eq 0 ] && echo "预算通过" || echo "预算未通过" >&2
exit "$fail"
