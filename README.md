# Hermes Memory Enhancer

**Hermes Memory Enhancer is a memory-provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

It is designed specifically for Hermes users who need more than a small profile note or a flat list of remembered facts. The plugin stores agent knowledge in a **local SQLite database** with a filesystem-like hierarchy (`memory://` URIs), tiered context retrieval, full-text search, automatic session memory extraction, and resource ingestion — **no external server required**.

## Keywords

Hermes Agent memory provider, AI agent memory, persistent memory, semantic memory, long-term memory, self-hosted memory, SQLite memory backend, context database, session memory, agent knowledge base, retrieval-augmented memory, RAG memory, personal AI assistant memory, local-first AI, private AI memory, filesystem-style knowledge base, memory search, memory extraction, Hermes plugin.

## What this repository contains

- Hermes memory-provider plugin (SQLite-backed — no external server)
- Hermes tool schemas for search/read/browse/remember/resource ingestion
- install/remove scripts for one Hermes profile
- tests for the provider interface

## How it works

The plugin connects Hermes directly to a local SQLite database. There is no external server, no REST API, no Docker container — everything stays on your filesystem.

```
Hermes session
  ↓
Hermes memory-provider interface
  ↓
Hermes Memory Enhancer plugin
  ↓  (direct SQLite)
Local SQLite database (~/.hermes/memory_enhancer/memory.sqlite3)
```

## Provided tools

- **`memory_enhancer_search`**: Full-text search (FTS5) across the knowledge base
- **`memory_enhancer_read`**: Read content at `abstract`, `overview`, or `full` detail
- **`memory_enhancer_browse`**: Filesystem-style navigation (`list`, `tree`, `stat`)
- **`memory_enhancer_remember`**: Store durable facts (extracted on session commit)
- **`memory_enhancer_add_resource`**: Import local files into the knowledge base (opt-in)

## System requirements

- Linux/macOS/WSL with Bash
- Hermes Agent already installed
- Python 3.10+ with built-in `sqlite3` module (included in all standard Python builds)
- Python package: `PyYAML` only for `install.sh` / `remove.sh` config editing

No additional Python packages are required. The plugin uses only:
- Python standard library (`sqlite3`, `json`, `threading`, `re`, etc.)
- Hermes' `MemoryProvider` interface
- `PyYAML` for the install/remove shell scripts only

Install PyYAML if not already present:

```bash
python3 -m pip install PyYAML
```

## Installation

1. Confirm Hermes is installed:

```bash
hermes config path
hermes memory status
```

2. Enable this plugin for one Hermes profile:

```bash
plugins/memory/hermes_memory_enhancer/install.sh \
  --home "$HOME/.hermes" \
  --db-path "$HOME/.hermes/memory_enhancer/memory.sqlite3"
```

3. Restart Hermes CLI or gateway.

Manual setup is also possible:

```bash
hermes config set memory.provider hermes_memory_enhancer
mkdir -p ~/.hermes/memory_enhancer
printf '\nMEMORY_ENHANCER_DB_PATH=$HOME/.hermes/memory_enhancer/memory.sqlite3\nMEMORY_ENHANCER_AGENT=hermes\n' >> ~/.hermes/.env
```

## Verification

After restart:

```bash
hermes memory status
```

Then test in a Hermes session:

```
memory_enhancer_browse with action=tree and path=memory://
```

## SQLite storage model

The database is created at `MEMORY_ENHANCER_DB_PATH` (default: `~/.hermes/memory_enhancer/memory.sqlite3`).

Schema:
- **`nodes`** — Filesystem-like hierarchy (`memory://` URIs) with content and abstracts
- **`nodes_fts`** — FTS5 full-text search index
- **`sessions`** — Conversation session tracking
- **`messages`** — Per-turn message history
- **`memories`** — Extracted and explicitly remembered facts
- **`memories_fts`** — FTS5 search over memories
- **`resources`** — Imported file resources

Important boundaries:
- No global SQLite service is installed
- The plugin does not write into Hermes' own session databases
- Built-in `MEMORY.md`, `USER.md`, and skills remain separate
- Removal never deletes the SQLite DB by default
- Optional purge deletes only the app-owned DB under `<home>/memory_enhancer/`

## Configuration

Environment variables in the selected Hermes profile `.env`:

| Variable | Description | Default |
|---|---|---|
| `MEMORY_ENHANCER_DB_PATH` | Path to SQLite database file | `~/.hermes/memory_enhancer/memory.sqlite3` |
| `MEMORY_ENHANCER_ACCOUNT` | Tenant account label | `default` |
| `MEMORY_ENHANCER_USER` | Tenant user label | `default` |
| `MEMORY_ENHANCER_AGENT` | Agent label | `hermes` |
| `MEMORY_ENHANCER_PREFETCH_TOP_K` | Prefetch result count (0–10) | `3` |
| `MEMORY_ENHANCER_MAX_ABSTRACT_CHARS` | Abstract cap (100–2000) | `500` |
| `MEMORY_ENHANCER_SYNC_MAX_CHARS` | Session sync cap (500–12000) | `4000` |
| `MEMORY_ENHANCER_REDACT_SECRETS` | Redact credentials from output | `true` |
| `MEMORY_ENHANCER_ENABLE_ADD_RESOURCE` | Enable file import tool | `false` |
| `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS` | `:`-separated upload allowlist | (empty) |

Example `.env` block:

```bash
MEMORY_ENHANCER_DB_PATH=$HOME/.hermes/memory_enhancer/memory.sqlite3
MEMORY_ENHANCER_ACCOUNT=default
MEMORY_ENHANCER_USER=default
MEMORY_ENHANCER_AGENT=hermes
```

## Security notes

See [`SECURITY.md`](SECURITY.md) for the full security policy.

Important defaults:
- Local file ingestion requires `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS`
- Do not allow broad upload roots such as `/`, `/home`, `/home/user`, or `~`
- Keep `.env` files and SQLite databases out of Git
- Do not store passwords, API keys, private keys, raw PHI/PII, or regulated data
- Secret redaction is best-effort

## Two-tier memory: Memory Enhancer + per-agent memory files

Memory Enhancer works alongside Hermes' built-in per-agent memory files
(`MEMORY.md` / `USER.md`). These two storage tiers serve different purposes:

| Tier | Tool | Storage | Injected every turn? |
|------|------|---------|---------------------|
| **Per-agent memory** | `memory` tool | `{hermes_home}/memories/MEMORY.md` / `USER.md` | ✅ Yes — always injected |
| **Shared knowledge base** | `memory_enhancer_*` tools | SQLite database (Memory Enhancer) | ❌ No — searched when needed |

**Why this matters:** The per-agent memory files are automatically injected into
every conversation turn, making them ideal for rules, preferences, and identity
that the agent should always follow. The Memory Enhancer DB stores durable
shared knowledge that is searched on demand — important rules stored only there
may be missed unless the agent actively searches.

### Workflow: promoting rules to per-agent memory

When an agent discovers a rule, preference, or pattern that should be injected
every turn:

1. Store it in Memory Enhancer via `memory_enhancer_remember` (shared DB)
2. **Also** write it to the agent's per-agent memory via the `memory` tool:

   ```
   memory(target='memory', action='add', content='...')   → MEMORY.md
   memory(target='user', action='add', content='...')     → USER.md
   ```

**When to promote:**
- A newly discovered user preference or behavioral rule
- A frequently-needed workflow pattern
- An identity or role definition
- A critical security or operational constraint

### Adding to AGENTS.md / SOUL.md

For better agent awareness, add this to each agent's `AGENTS.md` / `SOUL.md`:

```markdown
## Memory Enhancer usage
- `memory_enhancer_search` for semantic lookup
- `memory_enhancer_read` for `memory://` URIs returned by search
- `memory_enhancer_browse` to inspect stored knowledge hierarchy
- `memory_enhancer_remember` for durable project facts
- When you find a rule/pattern worth injecting every turn, also write it to
  MEMORY.md (for personal notes) or USER.md (for user facts) via the `memory` tool
```

## Removal

```bash
plugins/memory/hermes_memory_enhancer/remove.sh --home "$HOME/.hermes"
```

To also remove the SQLite database:

```bash
plugins/memory/hermes_memory_enhancer/remove.sh --home "$HOME/.hermes" --remove-env --purge-app-db
```
