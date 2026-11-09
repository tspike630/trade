#!/bin/bash
# 一键运行 CityU WRDS 增量导出（fetch_cityu_lseg_incremental.py --mode wrds）
# 用法:
#   ./run_wrds_export.sh                      # 交互式输入用户名 + 密码
#   WRDS_USER=xxx ./run_wrds_export.sh        # 只交互输入密码
#   NOSAVE=1 ./run_wrds_export.sh             # 跑完删除 ~/.pgpass（不落盘密码）
set -euo pipefail

VENV="/tmp/wrds_venv_39"
FETCH="/tmp/trade_sync/scripts/fetch_cityu_lseg_incremental.py"
OUT_DIR="${OUT_DIR:-$HOME/Desktop/cityu_wrds_exports}"
WRDS_HOST="wrds-pgdata.wharton.upenn.edu"

if [ ! -x "$VENV/bin/python" ]; then
  echo "[ERROR] 虚拟环境不存在: $VENV"
  echo "        重建: /usr/bin/python3 -m venv $VENV && $VENV/bin/python -m pip install wrds pandas"
  exit 1
fi

WRDS_USER="${WRDS_USER:-}"
if [ -z "$WRDS_USER" ]; then
  read -r -p "WRDS username (CityU email / EID): " WRDS_USER
fi

# 如果已有可用的 ~/.pgpass 条目，则不重复问密码
if ! grep -q "^${WRDS_HOST}:9737:wrds:${WRDS_USER}:" "$HOME/.pgpass" 2>/dev/null; then
  read -r -s -p "WRDS password: " WRDS_PASS
  echo
  umask 077
  printf '%s:9737:wrds:%s:%s\n' "$WRDS_HOST" "$WRDS_USER" "$WRDS_PASS" >> "$HOME/.pgpass"
  chmod 600 "$HOME/.pgpass"
  echo "[INFO] 已写入 ~/.pgpass (600)"
fi

mkdir -p "$OUT_DIR"
echo "[INFO] 输出目录: $OUT_DIR"
echo "[INFO] 开始导出（TAQ / OptionMetrics / TRACE / RavenPack 等 15 张表，首次跑耗时较长）"

set +e
"$VENV/bin/python" "$FETCH" --mode wrds --wrds-user "$WRDS_USER" --out-dir "$OUT_DIR"
rc=$?
set -e

if [ "${NOSAVE:-0}" = "1" ]; then
  # 仅移除本次写入的那一行
  grep -v "^${WRDS_HOST}:9737:wrds:${WRDS_USER}:" "$HOME/.pgpass" > "$HOME/.pgpass.tmp" || true
  mv "$HOME/.pgpass.tmp" "$HOME/.pgpass"
  chmod 600 "$HOME/.pgpass"
  echo "[INFO] 已按 NOSAVE=1 移除 ~/.pgpass 中的本次条目"
fi

exit "$rc"
