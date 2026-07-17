#!/usr/bin/env bash
#
# Batch session export. Runs each vendor's export one by one (incrementally:
# only changed sessions are re-rendered) into the output_dir from config.yml.
#
# Usage:
#   ./runapp.sh                # export all vendors, incremental
#   ./runapp.sh --thinking     # extra flags are forwarded to each run
#
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="${CONFIG:-config.yml}"
PY="${PYTHON:-python3}"

# Read the export root from config.yml (so the batch and the tool agree).
OUTPUT_DIR="$("$PY" - "$CONFIG" <<'PYEOF'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print(cfg.get("output_dir", "sessions_md"))
PYEOF
)"

echo "================================================================"
echo " Session export — output root: $OUTPUT_DIR"
echo " Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"

# Run each vendor as its own pass so one failure doesn't block the others.
status=0
for vendor in claude openai droid openclaw; do
  echo ""
  echo ">>> Exporting vendor: $vendor"
  if "$PY" export_sessions.py --vendor "$vendor" "$@"; then
    echo "<<< $vendor done"
  else
    echo "!!! $vendor failed (continuing)" >&2
    status=1
  fi
done

echo ""
echo "================================================================"
echo " All passes complete. Index: $OUTPUT_DIR/index.md"
echo " Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"
exit "$status"
