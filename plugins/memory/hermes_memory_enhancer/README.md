# Hermes Memory Enhancer

**Hermes Memory Enhancer is a memory-provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

It is designed specifically for Hermes users who need more than a small profile note or a flat list of remembered facts. The plugin stores agent knowledge in a **local SQLite database** — no external server or HTTP API needed. It provides a filesystem-like hierarchy (`memory://` URIs), tiered context retrieval, full-text search, automatic session memory extraction, and resource ingestion.

## What this repository contains

- Hermes memory-provider plugin
- Hermes tool schemas for search/read/browse/remember/resource ingestion
- install/remove scripts for one Hermes profile
- tests for the provider interface

## How it works

```
Hermes session
  ↓
Hermes memory-provider interface
  ↓
Hermes Memory Enhancer plugin
  ↓  (direct SQLite)
Local SQLite database (~/.hermes/memory_enhancer/memory.sqlite3)
```

No external server, no REST API, no Docker containers — just a SQLite database on your local filesystem.

## System requirements

- Linux/macOS/WSL with Bash
- Hermes Agent already installed
- Python 3.10+ in the same environment Hermes uses
- Python built-in `sqlite3` module (included in all standard Python builds)

No additional Python packages are required. The plugin uses only the Python standard library (`sqlite3`, `json`, `threading`, `re`, etc.) plus Hermes' own `MemoryProvider` interface.

## SQLite storage

The database is created at the path specified by `MEMORY_ENHANCER_DB_PATH` (default: `~/.hermes/memory_enhancer/memory.sqlite3`).

The schema includes:
- **`nodes`** — Filesystem-like hierarchy (`memory://` URIs) with content, abstracts, and overviews
- **`nodes_fts`** — Full-text search index (FTS5) over node names and content
- **`sessions`** — Conversation session tracking
- **`messages`** — Per-turn message history
- **`memories`** — Extracted and explicitly remembered facts
- **`memories_fts`** — Full-text search index over memories
- **`resources`** — Imported file resources

Important boundaries:
- The plugin does **not** modify Hermes' own session database or `MEMORY.md`/`USER.md`
- Built-in Hermes memory and this external storage operate side-by-side
- Removal never deletes the SQLite database by default
- Optional purge deletes only the app-owned DB under `<home>/memory_enhancer/`

## Installation

1. Confirm Hermes is installed and has a profile:

```bash
hermes config path
hermes memory status
```

2. SQLite is already included in Python — no extra dependencies needed:

```bash
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

3. Enable this plugin for one Hermes profile:

```bash
plugins/memory/hermes_memory_enhancer/install.sh \
  --home "$HOME/.hermes" \
  --db-path "$HOME/.hermes/memory_enhancer/memory.sqlite3"
```

What install changes:
- sets `memory.provider: hermes_memory_enhancer` in the selected Hermes `config.yaml`
- writes or updates only `MEMORY_ENHANCER_*` lines in the selected profile `.env`
- creates the parent directory for the SQLite DB path if needed
- creates timestamped backups unless `--no-backup` is passed

Manual setup is also possible:

```bash
hermes config set memory.provider hermes_memory_enhancer
mkdir -p ~/.hermes/memory_enhancer
printf '\nMEMORY_ENHANCER_DB_PATH=$HOME/.hermes/memory_enhancer/memory.sqlite3\nMEMORY_ENHANCER_AGENT=hermes\n' >> ~/.hermes/.env
```

Restart Hermes CLI/gateway after changing memory-provider configuration.

## Provided tools

- **`memory_enhancer_search`**: Full-text search across the knowledge base with `fast`, `deep`, or `auto` modes
- **`memory_enhancer_read`**: Read a `memory://` URI at `abstract` (~100 tokens), `overview` (~2k), or `full` detail
- **`memory_enhancer_browse`**: Filesystem-style navigation using `list`, `tree`, or `stat`
- **`memory_enhancer_remember`**: Store a fact or memory (extracted on session commit)
- **`memory_enhancer_add_resource`**: Opt-in local file ingestion. Disabled unless `MEMORY_ENHANCER_ENABLE_ADD_RESOURCE=true`; local uploads also require `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS`

## Configuration

Environment variables in the selected Hermes profile `.env`:

| Variable | Description | Default |
|---|---|---|
| `MEMORY_ENHANCER_DB_PATH` | Path to SQLite database file | `~/.hermes/memory_enhancer/memory.sqlite3` |
| `MEMORY_ENHANCER_ACCOUNT` | Tenant account label | `default` |
| `MEMORY_ENHANCER_USER` | Tenant user label | `default` |
| `MEMORY_ENHANCER_AGENT` | Agent label | `hermes` |
| `MEMORY_ENHANCER_PREFETCH_TOP_K` | Auto-prefetch result count (0–10) | `3` |
| `MEMORY_ENHANCER_MAX_ABSTRACT_CHARS` | Per-result abstract cap (100–2000) | `500` |
| `MEMORY_ENHANCER_SYNC_MAX_CHARS` | Per-message session sync cap (500–12000) | `4000` |
| `MEMORY_ENHANCER_REDACT_SECRETS` | Redact credentials from memory | `true` |
| `MEMORY_ENHANCER_ENABLE_ADD_RESOURCE` | Enable `add_resource` tool | `false` |
| `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS` | `:`-separated allowlist for file upload | (empty) |

Example `.env` block:

```bash
MEMORY_ENHANCER_DB_PATH=$HOME/.hermes/memory_enhancer/memory.sqlite3
MEMORY_ENHANCER_ACCOUNT=default
MEMORY_ENHANCER_USER=default
MEMORY_ENHANCER_AGENT=hermes
```

## Security notes

See [`../../../SECURITY.md`](../../../SECURITY.md) for the full security policy.

Important defaults:
- Local file ingestion requires `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS`
- Do not allow broad upload roots such as `/`, `/home`, `/home/user`, or `~`
- Keep `.env` files and SQLite databases out of Git
- Do not store passwords, API keys, private keys, raw PHI/PII, or regulated data
- Secret redaction is best-effort; it is not a complete data-loss-prevention system

## Removal

Disable this integration only:

```bash
plugins/memory/hermes_memory_enhancer/remove.sh --home "$HOME/.hermes"
```

Remove `MEMORY_ENHANCER_*` environment lines too:

```bash
plugins/memory/hermes_memory_enhancer/remove.sh --home "$HOME/.hermes" --remove-env
```

Also delete the app-owned SQLite DB:

```bash
plugins/memory/hermes_memory_enhancer/remove.sh --home "$HOME/.hermes" --remove-env --purge-app-db
```

Removal deliberately never deletes by default:
- SQLite databases (unless `--purge-app-db`)
- built-in Hermes memories
- other providers' config or data
- Python packages or system services
