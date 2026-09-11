#!/usr/bin/env bash
# Reusable local UI checks. Exit codes are authoritative; reports stay external.
set -euo pipefail
NODE="${WB3_NODE:-node}"
REPORT="${WB3_REPORT_DIR:-$(mktemp -d)}"
ROOT="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$ROOT"
mkdir -p "$REPORT/logs"
for script in format_render.test wb3_probe_markets wb3_probe_nulls wb3_capture wb3_verify_viewport; do
  logfile="$REPORT/logs/$script.log"
  if [ -e "$logfile" ]; then echo "REFUSE_OVERWRITE $logfile"; exit 2; fi
  WB3_REPORT_DIR="$REPORT/$script" "$NODE" "qa/wb3/$script.cjs" > "$logfile" 2>&1
done
