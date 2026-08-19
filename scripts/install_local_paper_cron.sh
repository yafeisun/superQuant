#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
LOG_DIR="$ROOT_DIR/local_runs/logs"
mkdir -p "$LOG_DIR"

CRON_FILE="$(mktemp)"
crontab -l 2>/dev/null \
  | grep -v 'superQuant local paper' \
  | grep -v 'automation.run_local_paper_live' \
  | grep -v 'automation.run_local_intraday_paper' \
  > "$CRON_FILE" || true

cat >> "$CRON_FILE" <<EOF
# superQuant local paper after close, 16:15 China time
15 16 * * 1-5 cd $ROOT_DIR && $PYTHON -m automation.run_local_paper_live --config configs/smallcap_live.yaml >> $LOG_DIR/after_close.log 2>&1
# superQuant local paper intraday watch, 09:30-11:30 and 13:00-15:00 China time
30-59/5 9 * * 1-5 cd $ROOT_DIR && $PYTHON -m automation.run_local_intraday_paper --config configs/smallcap_live.yaml --no-fetch >> $LOG_DIR/intraday.log 2>&1
*/5 10 * * 1-5 cd $ROOT_DIR && $PYTHON -m automation.run_local_intraday_paper --config configs/smallcap_live.yaml --no-fetch >> $LOG_DIR/intraday.log 2>&1
0-30/5 11 * * 1-5 cd $ROOT_DIR && $PYTHON -m automation.run_local_intraday_paper --config configs/smallcap_live.yaml --no-fetch >> $LOG_DIR/intraday.log 2>&1
*/5 13-14 * * 1-5 cd $ROOT_DIR && $PYTHON -m automation.run_local_intraday_paper --config configs/smallcap_live.yaml --no-fetch >> $LOG_DIR/intraday.log 2>&1
0 15 * * 1-5 cd $ROOT_DIR && $PYTHON -m automation.run_local_intraday_paper --config configs/smallcap_live.yaml --no-fetch >> $LOG_DIR/intraday.log 2>&1
EOF

crontab "$CRON_FILE"
rm -f "$CRON_FILE"
echo "installed superQuant local paper cron jobs"
