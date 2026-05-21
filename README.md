# Hermes Memory Enhancer

**Hermes Memory Enhancer is a memory-provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

It is designed specifically for Hermes users who need more than a small profile note or a flat list of remembered facts. The plugin connects Hermes to a self-hosted context database that can store session-derived memories, indexed resources, and structured knowledge in a filesystem-like hierarchy, then retrieve the right context back into future Hermes conversations.


## Keywords

Hermes Agent memory provider, AI agent memory, persistent memory, semantic memory, long-term memory, self-hosted memory, SQLite memory backend, context database, session memory, agent knowledge base, retrieval-augmented memory, RAG memory, personal AI assistant memory, local-first AI, private AI memory, filesystem-style knowledge base, memory search, memory extraction, Hermes plugin.

## What this repository contains

This repository is the **Hermes integration layer**:

- Hermes memory-provider plugin
- Hermes tool schemas for search/read/browse/remember/resource ingestion
- install/remove scripts for one Hermes profile
- tests for the provider interface
- documentation for connecting Hermes to a Memory Enhancer server

This repository is **not** the Memory Enhancer server repository and does **not** contain server implementation, server deployment manifests, server database migrations, or production server hardening. A Memory Enhancer server must run separately and expose the REST API at `MEMORY_ENHANCER_ENDPOINT`.

## System requirements

### Required on the Hermes machine

- Linux/macOS/WSL with Bash
- Hermes Agent already installed
- Python 3.10+ in the same environment Hermes uses
- Python package: `httpx`
- Python package: `PyYAML` only for `install.sh` / `remove.sh` config editing
- network access from Hermes to `MEMORY_ENHANCER_ENDPOINT`

Install Python dependencies if they are not already present:

```bash
python3 -m pip install httpx PyYAML
```

If Hermes runs inside a virtual environment, install into that same environment:

```bash
/path/to/hermes/venv/bin/python -m pip install httpx PyYAML
```

### Required for the Memory Enhancer server

- a running Memory Enhancer server reachable by HTTP
- SQLite support in the server runtime
- write permission to the configured SQLite DB directory

SQLite is an embedded library/database file, not a separate daemon. On most Python builds, `sqlite3` is already included. Verify:

```bash
python3 - <<'PY'
import sqlite3
print(sqlite3.sqlite_version)
PY
```

If that fails on Debian/Ubuntu, install the OS packages:

```bash
sudo apt-get update
sudo apt-get install -y sqlite3 libsqlite3-dev
```

The plugin itself does **not** create a global SQLite service and does **not** write directly to SQLite. The server owns the SQLite database.

## Why this matters for Hermes

Hermes is useful because it can act across sessions: it remembers preferences, loads skills, searches previous work, and uses tools on the user's machine. But as usage grows, memory becomes the bottleneck.

Common failure modes in long-running agent use:

- important facts are buried in old session transcripts
- user preferences, project constraints, and environment details become mixed together
- the agent retrieves too much irrelevant memory or misses the relevant memory entirely
- memory is hard to inspect, prune, migrate, or share across profiles
- teams want self-hosted storage instead of sending all memory to a hosted SaaS backend

Hermes Memory Enhancer addresses this by giving Hermes a dedicated external memory layer: searchable, browsable, self-hostable, and separated from Hermes' built-in session/profile files.

## Background

Most AI agents started with short-term chat history. Hermes already goes further: it has persistent memory, skills, session search, profiles, and pluggable memory providers. That is the right architecture, but serious daily use creates a new need: memory must become an organized knowledge system, not just a recall buffer.

This plugin is built around that premise.

The goal is not to replace Hermes' built-in `MEMORY.md`, `USER.md`, skills, or session store. The goal is to complement them with a memory backend that can:

- extract useful facts from completed sessions
- store them outside the prompt and outside the core Hermes repository
- retrieve them by semantic relevance when needed
- expose a navigable knowledge tree for inspection and governance
- keep storage local or self-hosted when privacy matters

## Who needs this

This is most useful for Hermes users who run the agent as a durable assistant rather than a disposable chatbot.

Examples:

- researchers managing many projects, manuscripts, datasets, and journal requirements
- developers using Hermes across multiple repositories and machines
- operators running several Hermes profiles or Telegram/Discord gateway bots
- teams that need local/self-hosted memory with auditability
- users who want memory retrieval without turning every session transcript into prompt context
- users who want a clean way to separate personal profile memory, project knowledge, and imported resources

## Why it is good

### 1. Hermes-native integration

The plugin uses Hermes' memory-provider interface and session lifecycle hooks. Hermes can call it as a normal memory backend rather than through an ad hoc script.

### 2. Self-hosted storage boundary

The memory service can run locally or on infrastructure controlled by the user. The Hermes plugin is only the integration layer; storage is kept separate from Hermes' own internal SQLite/session files.

### 3. Tiered retrieval

Not every query needs the full source text. The provider supports retrieval levels such as fast search, deeper search, summaries/abstracts, overviews, and full reads. This helps reduce prompt bloat while preserving access to detail.

### 4. Filesystem-style browsing

Memory should be inspectable. The plugin exposes browse/list/tree/stat-style access so users can understand what is stored instead of trusting an opaque vector index.

### 5. Safer lifecycle behavior

Installation configures only the selected Hermes profile and `MEMORY_ENHANCER_*` environment variables. Removal does not delete memory databases by default. Optional purge is restricted to this plugin's app-owned SQLite path.

### 6. Better long-term agent behavior

A Hermes instance with organized memory can maintain continuity across weeks or months: project context, stable preferences, decisions, constraints, and reusable resources remain available without re-explaining them every session.

## How it works

High-level flow:

```text
Hermes session
  ↓
Hermes memory-provider interface
  ↓
Hermes Memory Enhancer plugin
  ↓ HTTP
Memory Enhancer server
  ↓
App-owned SQLite database / indexed knowledge store
```

During normal use:

1. Hermes starts with `memory.provider: hermes_memory_enhancer`.
2. The plugin reads configuration from the selected Hermes profile `.env`.
3. Hermes calls provider methods for memory search, read, browse, remember, and resource ingestion.
4. At session end, the plugin can send session-derived material to the Memory Enhancer server for extraction/indexing.
5. In later sessions, Hermes queries the provider and receives relevant context back as structured results.

The plugin does **not** directly modify Hermes' built-in memory files or session database.

## What to put in AGENTS.md or MEMORY.md

### Required

You do **not** have to edit `AGENTS.md` or `MEMORY.md` for Hermes to use this provider. The required switch is:

```yaml
memory:
  provider: hermes_memory_enhancer
```

and the selected Hermes profile `.env` must contain `MEMORY_ENHANCER_ENDPOINT`.

### Recommended AGENTS.md instruction

For better agent behavior, add a short instruction to the relevant project/profile `AGENTS.md` or `SOUL.md`. This is an instruction, so it belongs in `AGENTS.md`/`SOUL.md`, not in `MEMORY.md`.

```markdown
## Memory Enhancer usage

This Hermes profile uses the Hermes Memory Enhancer provider.
Before asking the user to repeat project context, search Memory Enhancer.
Use:
- `memory_enhancer_search` for semantic lookup
- `memory_enhancer_read` for `memory://` URIs returned by search
- `memory_enhancer_browse` to inspect stored knowledge hierarchy
Store durable project facts with `memory_enhancer_remember` when they should survive across sessions.
Do not store secrets, credentials, raw private data, or temporary task progress.
```

### Recommended MEMORY.md content

`MEMORY.md` should contain facts, not operating instructions. If you keep a local note there, make it declarative and minimal:

```markdown
Hermes Memory Enhancer is configured for this profile at MEMORY_ENHANCER_ENDPOINT. Durable project knowledge may be available through the `memory_enhancer_*` tools.
```

Do **not** put API keys, private DB paths, or long usage manuals in `MEMORY.md`.

## Provided tools

- `memory_enhancer_search`: semantic search with `fast`, `deep`, or `auto` modes
- `memory_enhancer_read`: read a `memory://` URI at `abstract`, `overview`, or `full` detail
- `memory_enhancer_browse`: filesystem-style navigation using `list`, `tree`, or `stat`
- `memory_enhancer_remember`: store a fact for extraction on session commit
- `memory_enhancer_add_resource`: ingest URLs, files, or directories into the knowledge base

## SQLite storage model

This project uses SQLite as an embedded app-owned database file. It does **not** require installing a separate SQLite server.

Recommended default path:

```bash
$HOME/.hermes/memory_enhancer/memory.sqlite3
```

Important boundaries:

- no global SQLite service is installed
- the Hermes plugin does not write directly into Hermes' own SQLite/session databases
- built-in `MEMORY.md`, `USER.md`, skills, and other provider data remain separate
- removal never deletes SQLite by default
- optional purge deletes only this program's app-owned DB under `<home>/memory_enhancer/`

## Installation

1. Confirm Hermes is installed and has a profile:

```bash
hermes config path
hermes memory status
```

2. Confirm Python dependencies:

```bash
python3 - <<'PY'
import httpx, yaml, sqlite3
print('httpx ok')
print('PyYAML ok')
print('sqlite3', sqlite3.sqlite_version)
PY
```

3. Start the Memory Enhancer server separately and verify health:

```bash
curl -fsS http://127.0.0.1:1933/health
```

4. Enable this plugin for one Hermes profile:

```bash
plugins/memory/hermes_memory_enhancer/install.sh \
  --home "$HOME/.hermes" \
  --endpoint "http://127.0.0.1:1933" \
  --db-path "$HOME/.hermes/memory_enhancer/memory.sqlite3"
```

What install changes:

- sets `memory.provider: hermes_memory_enhancer` in the selected Hermes `config.yaml`
- writes or updates only `MEMORY_ENHANCER_*` lines in the selected profile `.env`
- creates the parent directory for the app-owned SQLite DB path if needed
- creates timestamped backups unless `--no-backup` is passed

What install does not change:

- Hermes built-in memory files
- Hermes session database
- other memory providers' data
- Memory Enhancer server storage
- Python packages, Docker images, or system services

Manual setup is also possible:

```bash
hermes config set memory.provider hermes_memory_enhancer
printf '\nMEMORY_ENHANCER_ENDPOINT=http://127.0.0.1:1933\n' >> ~/.hermes/.env
printf 'MEMORY_ENHANCER_DB_PATH=$HOME/.hermes/memory_enhancer/memory.sqlite3\n' >> ~/.hermes/.env
```

Restart Hermes CLI/gateway after changing memory-provider configuration.

## Verification

After restart:

```bash
hermes memory status
```

Then test from a Hermes session:

```text
Use memory_enhancer_browse with action=tree and path=memory://
```

or ask Hermes:

```text
Search Memory Enhancer for "my current project constraints" and show the memory:// URIs you found.
```

If the provider is not visible, check:

```bash
hermes config path
hermes config | grep -A5 '^memory:'
grep '^MEMORY_ENHANCER_' ~/.hermes/.env
curl -fsS http://127.0.0.1:1933/health
```

## Configuration

Environment variables in the selected Hermes profile `.env`:

- `MEMORY_ENHANCER_ENDPOINT`: Memory Enhancer server URL. Default example: `http://127.0.0.1:1933`
- `MEMORY_ENHANCER_DB_PATH`: app-owned SQLite DB path used by the server. Default example: `$HOME/.hermes/memory_enhancer/memory.sqlite3`
- `MEMORY_ENHANCER_API_KEY`: optional API key
- `MEMORY_ENHANCER_ACCOUNT`: optional account/tenant label. Default: `default`
- `MEMORY_ENHANCER_USER`: optional user/tenant label. Default: `default`
- `MEMORY_ENHANCER_AGENT`: optional agent label. Default: `hermes`

Example `.env` block:

```bash
MEMORY_ENHANCER_ENDPOINT=http://127.0.0.1:1933
MEMORY_ENHANCER_DB_PATH=$HOME/.hermes/memory_enhancer/memory.sqlite3
MEMORY_ENHANCER_ACCOUNT=default
MEMORY_ENHANCER_USER=default
MEMORY_ENHANCER_AGENT=hermes
# MEMORY_ENHANCER_API_KEY=replace-if-server-requires-auth
```

## Security notes

See [`SECURITY.md`](SECURITY.md) for the full security policy.

Important defaults:

- Prefer loopback for local use: `MEMORY_ENHANCER_ENDPOINT=http://127.0.0.1:1933`.
- Non-loopback remote endpoints must use HTTPS unless `MEMORY_ENHANCER_ALLOW_INSECURE_REMOTE=true` is explicitly set.
- Non-loopback remote endpoints require `MEMORY_ENHANCER_API_KEY` unless `MEMORY_ENHANCER_ALLOW_UNAUTHENTICATED_REMOTE=true` is explicitly set.
- `memory_enhancer_add_resource` is disabled unless `MEMORY_ENHANCER_ENABLE_ADD_RESOURCE=true` is explicitly set.
- Local file/directory ingestion requires a narrow `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS` allowlist.
- Do not allow broad upload roots such as `/`, `/home`, `/home/user`, or `~`.
- Keep `.env` files, API keys, private DB paths, and SQLite databases out of Git.
- Do not store passwords, API keys, private keys, raw PHI/PII, or regulated data unless your own deployment policy explicitly allows it.
- Secret redaction is best-effort; it is not a complete data-loss-prevention system.

## Operational notes

- Restart Hermes CLI/gateway after changing `memory.provider` or `.env`.
- Configure one Hermes profile at a time. For multiple profiles, run `install.sh --home <profile-home>` for each profile.
- Keep real credentials and private DB paths out of Git.
- Back up the SQLite DB if it contains valuable long-term memory.
- Do not use this provider to store secrets, passwords, raw PHI/PII, or transient task logs unless your deployment policy explicitly allows it.

## Removal

Disable this integration only:

```bash
plugins/memory/hermes_memory_enhancer/remove.sh --home "$HOME/.hermes"
```

Disable and remove only `MEMORY_ENHANCER_*` environment lines:

```bash
plugins/memory/hermes_memory_enhancer/remove.sh --home "$HOME/.hermes" --remove-env
```

Disable and delete this program's own SQLite DB only when it is under `<home>/memory_enhancer/`:

```bash
plugins/memory/hermes_memory_enhancer/remove.sh --home "$HOME/.hermes" --remove-env --purge-app-db
```

Removal deliberately never deletes by default:

- SQLite databases
- built-in Hermes memories
- other providers' config or data
- Memory Enhancer server storage
- Python packages, Docker images, or system services

## Privacy and governance

This repository should not contain local deployment details. Public or shared examples should use placeholders only:

```bash
MEMORY_ENHANCER_ACCOUNT=default
MEMORY_ENHANCER_USER=default
MEMORY_ENHANCER_AGENT=hermes
MEMORY_ENHANCER_ENDPOINT=http://127.0.0.1:1933
MEMORY_ENHANCER_DB_PATH=$HOME/.hermes/memory_enhancer/memory.sqlite3
```

Use real project names, agent names, private paths, and credentials only in a local `.env` that is not committed.

## Current status

Public Hermes Memory Enhancer plugin integration layer. Server implementation is separate.