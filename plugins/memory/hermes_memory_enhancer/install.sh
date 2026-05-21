#!/usr/bin/env bash
# Install/enable the Memory Enhancer memory provider for a Hermes profile.
# This script configures the Hermes profile to use the SQLite-backed direct
# Memory Enhancer provider — no external server needed.
#
# This script only edits the selected Hermes profile configuration and .env.
# It does not install/remove SQLite, Python, Hermes, or other providers' data.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: install.sh [--home PATH] [--db-path PATH] [--account NAME] [--user NAME] [--agent NAME] [--no-backup]

Enable Memory Enhancer as the external Hermes memory provider for one profile.
Uses SQLite directly — no external server required.

Options:
 --home PATH    Hermes home/profile directory (default: $HERMES_HOME or ~/.hermes)
 --db-path PATH Path to the SQLite database file
               (default: /memory_enhancer/memory.sqlite3 under HOME)
 --account NAME Memory Enhancer account/tenant (default: default)
 --user NAME    Memory Enhancer user/tenant (default: default)
 --agent NAME   Memory Enhancer agent label (default: hermes)
 --no-backup    Do not create timestamped backups before editing config/.env
 -h, --help     Show this help

Safe uninstall:
 ./remove.sh --home PATH
EOF
}

HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
DB_PATH=""
ACCOUNT="default"
USER_NAME="default"
AGENT_NAME="hermes"
BACKUP=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --home) HERMES_HOME_DIR="$2"; shift 2 ;;
        --db-path) DB_PATH="$2"; shift 2 ;;
        --account) ACCOUNT="$2"; shift 2 ;;
        --user) USER_NAME="$2"; shift 2 ;;
        --agent) AGENT_NAME="$2"; shift 2 ;;
        --no-backup) BACKUP=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

CONFIG_PATH="$HERMES_HOME_DIR/config.yaml"
ENV_PATH="$HERMES_HOME_DIR/.env"
mkdir -p "$HERMES_HOME_DIR"
if [[ -z "$DB_PATH" ]]; then
    DB_PATH="$HERMES_HOME_DIR/memory_enhancer/memory.sqlite3"
fi
mkdir -p "$(dirname "$DB_PATH")"

if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "Error: config.yaml not found at $CONFIG_PATH" >&2
    echo "Create the Hermes profile first, or pass --home to an existing profile." >&2
    exit 1
fi

if [[ "$BACKUP" -eq 1 ]]; then
    TS="$(date -u +%Y%m%dT%H%M%SZ)"
    cp "$CONFIG_PATH" "$CONFIG_PATH.hermes_memory_enhancer.bak.$TS"
    [[ -f "$ENV_PATH" ]] && cp "$ENV_PATH" "$ENV_PATH.hermes_memory_enhancer.bak.$TS"
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
mem["provider"] = "hermes_memory_enhancer"
path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY

python3 - "$ENV_PATH" "$DB_PATH" "$ACCOUNT" "$USER_NAME" "$AGENT_NAME" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
values = {
    "MEMORY_ENHANCER_DB_PATH": sys.argv[2],
    "MEMORY_ENHANCER_ACCOUNT": sys.argv[3],
    "MEMORY_ENHANCER_USER": sys.argv[4],
    "MEMORY_ENHANCER_AGENT": sys.argv[5],
}
existing = []
if env_path.exists():
    existing = env_path.read_text(encoding="utf-8").splitlines()
kept = [line for line in existing if not any(line.startswith(k + "=") for k in values)]
for key, val in values.items():
    if val != "":
        kept.append(f"{key}={val}")
env_path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
PY

cat <<EOF

✅ Memory Enhancer provider enabled for profile at $HERMES_HOME_DIR

  SQLite DB: $DB_PATH
  Account:   $ACCOUNT
  User:      $USER_NAME
  Agent:     $AGENT_NAME

Configuration updated:
  - config.yaml: memory.provider = hermes_memory_enhancer
  - .env:        MEMORY_ENHANCER_DB_PATH and MEMORY_ENHANCER_*

Next steps:
  1. Restart Hermes CLI or gateway
  2. Verify with: hermes memory status
  3. Test in a Hermes session:
       memory_enhancer_browse with action=tree and path=memory://

EOF
