#!/usr/bin/env bash
# Disable/remove only the Memory Enhancer Hermes memory-provider integration.
# This script deliberately does NOT remove SQLite databases, built-in Hermes memories,
# other provider data, Python packages, Docker images, or Memory Enhancer server data.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: remove.sh [--home PATH] [--remove-env] [--purge-app-db] [--no-backup]

Disable Memory Enhancer as the external Hermes memory provider for one profile.

Options:
  --home PATH   Hermes home/profile directory (default: $HERMES_HOME or ~/.hermes)
  --remove-env  Remove only MEMORY_ENHANCER_* lines from the profile .env file
                (default: keep .env values but disable memory.provider)
  --purge-app-db Delete only this program's app-owned SQLite DB if it is under
                <home>/memory_enhancer/ (never deletes system SQLite or other DBs)
  --no-backup   Do not create timestamped backups before editing config/.env
  -h, --help    Show this help

What this removes:
  - memory.provider=hermes_memory_enhancer from the selected config.yaml
  - optionally MEMORY_ENHANCER_* environment lines from the selected .env
  - optionally this program's own SQLite DB under <home>/memory_enhancer/

What this never removes:
  - SQLite databases
  - MEMORY.md / USER.md
  - other providers' config or data
  - Memory Enhancer server storage
  - Python packages, Docker images, or system services
EOF
}

HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
REMOVE_ENV=0
PURGE_APP_DB=0
BACKUP=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) HERMES_HOME_DIR="$2"; shift 2 ;;
    --remove-env) REMOVE_ENV=1; shift ;;
    --purge-app-db) PURGE_APP_DB=1; shift ;;
    --no-backup) BACKUP=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

CONFIG_PATH="$HERMES_HOME_DIR/config.yaml"
ENV_PATH="$HERMES_HOME_DIR/.env"

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Error: config.yaml not found at $CONFIG_PATH" >&2
  exit 1
fi

if [[ "$BACKUP" -eq 1 ]]; then
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  cp "$CONFIG_PATH" "$CONFIG_PATH.hermes_memory_enhancer-remove.bak.$TS"
  [[ -f "$ENV_PATH" ]] && cp "$ENV_PATH" "$ENV_PATH.hermes_memory_enhancer-remove.bak.$TS"
fi

python3 - "$CONFIG_PATH" <<'PY'
import sys
from pathlib import Path
try:
    import yaml
except Exception as exc:
    raise SystemExit(f"PyYAML is required to edit config.yaml: {exc}")

path = Path(sys.argv[1])
config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
mem = config.setdefault("memory", {})
if mem.get("provider") == "hermes_memory_enhancer":
    mem["provider"] = ""
path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY

DB_PATH=""
if [[ -f "$ENV_PATH" ]]; then
  DB_PATH="$(python3 - "$ENV_PATH" <<'PY'
import sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.startswith("MEMORY_ENHANCER_DB_PATH="):
        print(line.split("=", 1)[1])
        break
PY
)"
fi

if [[ "$REMOVE_ENV" -eq 1 && -f "$ENV_PATH" ]]; then
  python3 - "$ENV_PATH" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
kept = [line for line in lines if not line.startswith("MEMORY_ENHANCER_")]
path.write_text("\n".join(kept).rstrip() + ("\n" if kept else ""), encoding="utf-8")
PY
fi

if [[ "$PURGE_APP_DB" -eq 1 ]]; then
  SAFE_PREFIX="$HERMES_HOME_DIR/memory_enhancer/"
  if [[ -n "$DB_PATH" && "$DB_PATH" == "$SAFE_PREFIX"* && -f "$DB_PATH" ]]; then
    rm -f "$DB_PATH"
    # Remove empty app dir only; ignore if it contains anything else.
    rmdir "$HERMES_HOME_DIR/memory_enhancer" 2>/dev/null || true
  else
    echo "Skipped DB purge: MEMORY_ENHANCER_DB_PATH is empty, outside $SAFE_PREFIX, or not a file." >&2
  fi
fi

cat <<EOF
Memory Enhancer memory provider disabled for this Hermes profile only.

Hermes home: $HERMES_HOME_DIR
Config:      $CONFIG_PATH
Env cleanup: $([[ "$REMOVE_ENV" -eq 1 ]] && echo "MEMORY_ENHANCER_* removed" || echo "MEMORY_ENHANCER_* kept")
DB purge:    $([[ "$PURGE_APP_DB" -eq 1 ]] && echo "requested for app-owned DB only" || echo "not requested")

Restart Hermes CLI/gateway for the change to take effect.
EOF
