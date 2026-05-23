<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue"/>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue"/>
  <img src="https://img.shields.io/badge/dependencies-1-success"/>
  <img src="https://img.shields.io/badge/server-none-success"/>
</p>

<h1 align="center">🧠 Hermes Memory Enhancer</h1>
<p align="center">
  <b>Persistent, SQLite-backed memory for <a href="https://github.com/NousResearch/hermes-agent">Hermes Agent</a>.</b><br>
  No server. No daemon. No vector DB. One-line install. Pure SQLite + Python stdlib.
</p>

<p align="center">
  <code>bash install.sh</code> → Hermes remembers everything. Done.
</p>

> **About this project:** 450 lines of Python. One dependency (PyYAML — for the installer only). No server. No daemon. No Docker. No cloud. No API keys. No vector DB. Just a single SQLite file and the Hermes plugin interface. We believe agent memory should be a **plugin**, not an infrastructure project. Thirty seconds from `git clone` to your first remembered fact.

---

## The Problem

**Hermes Agent has no long-term memory.** Every session starts from scratch. You tell it the same project context, the same research findings, the same infrastructure details — over and over.

Existing solutions are overkill:
- Vector DBs (Chroma, Qdrant) → server, pip install, embeddings API, Docker
- LangChain memory → framework lock-in, heavyweight
- Flat markdown files → no search, no structure, manual curation

**This is different.** It's not a vector DB. It's not a wiki. It's a **persistent knowledge base** that Hermes queries automatically — session to session, agent to agent, project to project.

### Why this over the alternatives?

| If you use… | Your best choice is… |
|------------|-------------------|
| **Hermes Agent only** | **Hermes Memory Enhancer** — 450 lines, 1 dependency, 30-second install. The simplest Hermes memory plugin you can run. |
| **Hermes Agent + Codex CLI** | **Hermes + Codex CLI Memory Enhancer** — same engine, two interfaces. Hermes remembers your research context; Codex remembers your build context. They stay isolated by design, but you use the same mental model for both. |
| **Claude Code / Cursor / generic MCP** | Consider **memoirs** or **agentmem** — they have broader MCP support. Our focus is Hermes + Codex. |
| **Need vector search / graph / cloud sync** | Consider **Mnemosyne** (Hermes + sqlite-vec) or **Mem0** (cloud). Our focus is simplicity, not features. |

We intentionally **don't try to do everything**. We specialize in the simplest possible memory for the two agents that power your daily workflow: your conversation agent (Hermes) and your coding agent (Codex). If you use both, you get a unified memory philosophy across both tools — without the complexity of MCP, vector DBs, or cloud APIs.

---

## Features

### 🧠 Session-to-session persistence
Memories survive across sessions. When you tell Hermes something important, it stays — no more repeating the same context.

### 📂 Filesystem-style knowledge hierarchy
Data is organized as `memory://` URIs:
```
memory://
├── user/hermes/
│   ├── memories/     ← Extracted and explicitly remembered facts
│   └── skills/       ← Procedural knowledge
└── resources/        ← Imported local files
```
Browse it like a filesystem: `memory_enhancer_browse(action="tree")`

### 🔍 FTS5 full-text search
SQLite FTS5 ranks results by relevance. Search across memories, resources, and skills in one query.

```
memory_enhancer_search(query="deployment config")
```

### 📖 Tiered context retrieval
Three detail levels — start with a summary, drill down only when needed:
- **Abstract (L0)** — ~100 tokens, skim the gist
- **Overview (L1)** — ~2k tokens, key points
- **Full (L2)** — complete content

### ⭐ Importance-scored memories
Memories carry an importance score. Future support for priority-based context injection.

### 📊 Memory statistics dashboard
```
memory_enhancer_stats()
```
Returns: total memories, category distribution, importance distribution, session count, message count, DB size.

### 🧩 L3 Knowledge Graph Layer
Connect memories with typed relations and visualize the graph — no external database needed.

```bash
# Point L3 to the same DB as the plugin
export L3_DB_PATH=~/.hermes/memory_enhancer/memory.sqlite3

# Tag a memory URI
python3 scripts/l3.py tag add "memory://user/hermes/memories/1" "gwas"

# Relate two nodes
python3 scripts/l3.py relate \
  "memory://user/hermes/memories/1" \
  "memory://user/hermes/memories/2" informs

# Trace connections
python3 scripts/l3.py trace "memory://user/hermes/memories/1" --depth 3

# Generate interactive HTML graph
python3 scripts/l3_graph.py graph.html --db ~/.hermes/memory_enhancer/memory.sqlite3
```

L3 creates three tables (`l3_tags`, `l3_node_tags`, `l3_relations`) alongside your existing memory schema — zero schema changes to your data.

### 🛡️ Built-in secret redaction
API keys, tokens, and passwords are automatically redacted from sync payloads and search results. Opt-out via `MEMORY_ENHANCER_REDACT_SECRETS=false`.

### 🚫 Zero external dependencies
```bash
# What you DON'T need:
# ❌ pip install chromadb
# ❌ pip install qdrant-client
# ❌ docker pull qdrant/qdrant
# ❌ OPENAI_API_KEY for embeddings
# ❌ A server process
# ❌ A REST API

# What you DO need:
# ✅ Python 3.10+ (stdlib + PyYAML for install script)
```

### 🔄 Portable
Your memory is a single SQLite file. Copy it, back it up, move it anywhere.

```bash
# Back up
cp ~/.hermes/memory_enhancer/memory.sqlite3 backup.sqlite3

# Move to new machine
scp user@old-server:.hermes/memory_enhancer/memory.sqlite3 .
```

---

## Quick Install

```bash
git clone <repo-url>
cd hermes-memory-enhancer
bash plugins/memory/hermes_memory_enhancer/install.sh
```

**What the installer does:**
1. Registers `hermes_memory_enhancer` as Hermes' memory provider
2. Creates the SQLite database at `~/.hermes/memory_enhancer/memory.sqlite3`
3. Adds `MEMORY_ENHANCER_*` environment variables to your Hermes profile `.env`

Then restart your gateway. Done.

---

## Provided Tools

Once installed, Hermes exposes these tools to the agent:

### `memory_enhancer_search(query, [limit], [scope])`
Full-text search across the knowledge base. Returns ranked results with `memory://` URIs.

### `memory_enhancer_read(uri, [level])`
Read content at a `memory://` URI. Level: `abstract` (L0), `overview` (L1), or `full` (L2).

### `memory_enhancer_browse(action, [path])`
Filesystem-style navigation. Actions: `list`, `tree`, `stat`.

### `memory_enhancer_remember(content, [category])`
Store a durable fact. Automatically categorized and indexed immediately.

### `memory_enhancer_stats()`
Returns memory statistics: totals, categories, importance distribution, DB size.

### `memory_enhancer_add_resource(url, [reason], [to], [parent])`
Import a local file into the knowledge base. Disabled by default — requires `MEMORY_ENHANCER_ENABLE_ADD_RESOURCE=true` and `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS`.

---

## Example Workflow

```
# Session start — check what's stored
memory_enhancer_stats

# Remember a finding
memory_enhancer_remember(content="GWAS summary stats at ~/data/B003/sumstats.txt", category="entity")

# Later — search before asking
memory_enhancer_search(query="GWAS B003")

# Browse the knowledge tree
memory_enhancer_browse(action="tree")

# Import a reference document
memory_enhancer_add_resource(url="/home/user/papers/review.pdf", reason="Literature review for B003 project")
```

---

## How It Works

```
Hermes session
  ↓
Hermes memory-provider interface
  ↓
Hermes Memory Enhancer plugin   (this repo — ~450 lines of Python)
  ↓  (direct SQLite — no network)
Local SQLite database           (~/.hermes/memory_enhancer/memory.sqlite3)
```

The plugin connects Hermes directly to a local SQLite database. There is no external server, no REST API, no Docker container — everything stays on your filesystem.

### Storage model

| Table | Purpose |
|-------|---------|
| `nodes` | Filesystem hierarchy (`memory://` URIs) with content + abstracts |
| `nodes_fts` | FTS5 search index over nodes |
| `sessions` | Conversation session tracking |
| `messages` | Per-turn message log |
| `memories` | Extracted + explicitly stored facts |
| `memories_fts` | FTS5 search index over memories |
| `resources` | Imported file metadata |
| `l3_tags` | Tag definitions (L3 layer) |
| `l3_node_tags` | Node-to-tag assignments (L3 layer) |
| `l3_relations` | Directed typed relations between nodes (L3 layer) |

### Automatic session extraction

When a session ends, user messages starting with `[Remember]` are automatically extracted into the `memories` table. You can also use `memory_enhancer_remember` directly.

---

## Comparison: Other Approaches

| Solution | Server | pip install | Setup time | Auto-extract | Secret filter | Offline | Import files |
|----------|:-----:|:-----------:|:----------:|:------------:|:-------------:|:-------:|:------------:|
| **Hermes Memory Enhancer** | ❌ | ❌* | **30 sec** | ✅ | ✅ | ✅ | ✅ |
| ChromaDB | ✅ | ✅ | 30 min | ❌ | ❌ | ✅ | ❌ |
| Mem0 | ✅ | ✅ | 15 min | ❌ | ❌ | ❌ | ❌ |
| LangChain Memory | ❌ | ✅ | 10 min | ❌ | ❌ | ✅ | ❌ |
| OpenAI Assistants | ✅ | ❌ | 5 min | ❌ | ❌ | ❌ | ❌ |
| Flat markdown files | ❌ | ❌ | 1 min | ❌ | ❌ | ✅ | ❌ |

*\* PyYAML required only for install/remove scripts*

---

## Configuration

Environment variables in your Hermes profile `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `MEMORY_ENHANCER_DB_PATH` | Path to SQLite database | `~/.hermes/memory_enhancer/memory.sqlite3` |
| `MEMORY_ENHANCER_ACCOUNT` | Tenant account label | `default` |
| `MEMORY_ENHANCER_USER` | Tenant user label | `default` |
| `MEMORY_ENHANCER_AGENT` | Agent label | `hermes` |
| `MEMORY_ENHANCER_PREFETCH_TOP_K` | Prefetch result count (0–10) | `3` |
| `MEMORY_ENHANCER_MAX_ABSTRACT_CHARS` | Abstract character cap | `500` |
| `MEMORY_ENHANCER_SYNC_MAX_CHARS` | Session sync char cap | `4000` |
| `MEMORY_ENHANCER_REDACT_SECRETS` | Redact credentials from output | `true` |
| `MEMORY_ENHANCER_ENABLE_ADD_RESOURCE` | Enable file import | `false` |
| `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS` | `:`-separated upload allowlist | (empty) |

---

## Two-tier memory: Memory Enhancer + per-agent memory files

Memory Enhancer works alongside Hermes' built-in per-agent memory files (`MEMORY.md` / `USER.md`).

| Tier | Tool | Injected every turn? |
|------|------|:-------------------:|
| **Per-agent memory** | `memory` tool | ✅ Yes — always injected |
| **Shared knowledge base** | `memory_enhancer_*` tools | ❌ No — searched on demand |

**Why this matters:** Per-agent memory files are automatically injected every turn — ideal for identity, rules, and preferences. The Memory Enhancer DB stores shared knowledge that is queried when needed. Use both together.

---

## Requirements

- **Hermes Agent** installed
- **Python 3.10+** with built-in `sqlite3` module
- **PyYAML** only for `install.sh` / `remove.sh` (install via `pip install PyYAML`)
- **OS**: Linux, macOS, Windows WSL

### Agent configuration

After installing this plugin, add the following instruction to your agent's `SOUL.md`, `AGENTS.md`, or equivalent configuration file:

> **Primary memory system:** Use `memory_enhancer_*` tools (`search`, `remember`, `browse`, `read`, `stats`) over the basic `memory` tool. The basic `memory` tool is for identity and critical rules only — it is injected every turn. Use `memory_enhancer_*` for everything else: it is searchable, shared across agents, and does not bloat context.
>
> **Session start:** Call `memory_enhancer_stats` or `memory_enhancer_search` to restore context before asking the user to catch up.
>
> **During session:** Call `memory_enhancer_remember` for every important fact, decision, or finding.
>
> **Session end:** Ensure critical new facts are saved. The plugin auto-extracts `[Remember]` messages, but explicit `memory_enhancer_remember` calls are more reliable.

See [`AGENTS.md`](AGENTS.md) in this repository for the complete agent guidelines.

---

## Project Structure

```
hermes-memory-enhancer/
├── AGENTS.md                          ← Agent guidelines (read this if you're an AI)
├── README.md                           ← This file
├── SECURITY.md                         ← Security policy
├── LICENSE                             ← MIT
├── scripts/
│   ├── l3.py                           ← L3 CLI
│   └── l3_graph.py                     ← HTML graph generator
├── plugins/memory/hermes_memory_enhancer/
│   ├── __init__.py                     ← MemoryProvider plugin (~450 lines)
│   ├── plugin.yaml                     ← Plugin metadata
│   ├── install.sh                      ← One-shot install script
│   └── remove.sh                       ← Clean removal
└── tests/
    ├── plugins/memory/
    │   ├── test_hermes_memory_enhancer_provider.py
    │   └── test_security_defaults.py
    └── hermes_memory_enhancer_plugin/
        └── test_hermes_memory_enhancer.py
```

---

## v1 → v2 Migration

v2.0 is fully backward-compatible. Existing v1 databases are automatically migrated on first access:
- `importance` column added to `memories` (default: 3)
- `expires_at` column added (reserved for future TTL support)
- New `memory_enhancer_stats` tool available

No manual migration needed.

---

## Security

See [`SECURITY.md`](SECURITY.md) for the full security policy.

Key defaults:
- File import requires explicit `MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS`
- Secret redaction is on by default
- Do not store passwords, API keys, raw PHI/PII, or regulated data

---

## Roadmap

- [ ] TTL-based auto-expiry for memories
- [ ] Memory consolidation (merge duplicates)
- [ ] L3 graph web app (auto-refresh)
- [ ] Per-project namespace isolation
- [ ] Optional sentence-transformers semantic search

PRs welcome. Ideas welcome.

---

## Related

- **[Codex CLI Memory Enhancer](https://github.com/wmyung/codex-cli-memory-enhancer)** — the same memory system, adapted for OpenAI Codex CLI, with L3 graph layer included
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — the multi-provider agent framework this plugin extends
- **[SQLite FTS5](https://www.sqlite.org/fts5.html)** — the search engine behind it all. No vector DB needed.
