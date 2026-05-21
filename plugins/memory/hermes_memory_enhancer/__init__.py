"""Hermes Memory Enhancer plugin — full bidirectional MemoryProvider interface.

SQLite-backed persistent memory layer that organizes agent knowledge into a
filesystem-like hierarchy (memory:// URIs) with tiered context loading,
automatic memory extraction, and session management — no external server required.

Config via environment variables (profile-scoped via each profile's .env):
  MEMORY_ENHANCER_DB_PATH  — SQLite database path (required)
  MEMORY_ENHANCER_ACCOUNT  — Tenant account (default: default)
  MEMORY_ENHANCER_USER     — Tenant user (default: default)
  MEMORY_ENHANCER_AGENT    — Tenant agent (default: hermes)

Capabilities:
  - Automatic memory extraction on session commit
  - Tiered context: L0 (~100 tokens), L1 (~2k), L2 (full)
  - Full-text search with hierarchical directory retrieval
  - Filesystem-style browsing via memory:// URIs
  - Resource ingestion (local files)
"""

from __future__ import annotations

import atexit
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_DEFAULT_PREFETCH_TOP_K = 3
_DEFAULT_MAX_ABSTRACT_CHARS = 500
_DEFAULT_SYNC_MAX_CHARS = 4000

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
)
_SENSITIVE_PATH_PARTS = {
    ".git", ".gnupg", ".hermes", ".ssh", ".aws", ".azure", ".config/gh",
    "auth.json", "credentials", "id_rsa", "id_ed25519",
}
_SENSITIVE_NAME_HINTS = (".env", "secret", "token", "credential", "password", "passwd", "private_key")

# ---------------------------------------------------------------------------
# Process-level atexit safety net
# ---------------------------------------------------------------------------
_last_active_provider: Optional["HermesMemoryEnhancerProvider"] = None


def _atexit_commit_sessions():
    global _last_active_provider
    provider = _last_active_provider
    if provider is None:
        return
    _last_active_provider = None
    try:
        provider.on_session_end([])
    except Exception:
        pass


atexit.register(_atexit_commit_sessions)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda m: m.group(1) + " [REDACTED]" if m.groups() else "[REDACTED]",
            redacted,
        )
    return redacted


def _truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    suffix = "\n\n[... truncated by Memory Enhancer client]"
    if max_chars <= len(suffix):
        return value[:max_chars]
    return value[: max_chars - len(suffix)] + suffix


def _configured_upload_roots() -> List[Path]:
    raw = os.environ.get("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS", "")
    roots: List[Path] = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        roots.append(Path(item).expanduser().resolve())
    return roots


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _local_upload_security_error(path: Path) -> str:
    resolved = path.expanduser().resolve()
    name_lower = resolved.name.lower()
    full_lower = str(resolved).lower()
    if any(part in resolved.parts for part in _SENSITIVE_PATH_PARTS) or any(hint in name_lower for hint in _SENSITIVE_NAME_HINTS):
        return f"Refusing to upload sensitive local path: {path}"
    if any(part in full_lower for part in ("/.ssh/", "/.hermes/", "/.config/gh/", "/.aws/")):
        return f"Refusing to upload sensitive local path: {path}"
    roots = _configured_upload_roots()
    if not roots:
        return (
            "Local resource uploads are disabled unless MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS "
            "is set to one or more allowed directories."
        )
    if not any(_path_is_under(resolved, root) for root in roots):
        return f"Local resource path is outside MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS: {path}"
    return ""


def _is_remote_resource_source(value: str) -> bool:
    return value.startswith(("http://", "https://", "git@", "ssh://", "git://"))


def _is_windows_absolute_path(value: str) -> bool:
    return (
        len(value) >= 3
        and value[0].isalpha()
        and value[1] == ":"
        and value[2] in ("/", "\\")
    )


def _is_local_path_reference(value: str) -> bool:
    if not value or "\n" in value or "\r" in value:
        return False
    if _is_remote_resource_source(value):
        return False
    if _is_windows_absolute_path(value):
        return True
    return (
        value.startswith(("/", "./", "../", "~/", ".\\", "..\\", "~\\"))
        or "/" in value
        or "\\" in value
    )


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

_SQLITE_INIT = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uri TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    parent_uri TEXT NOT NULL DEFAULT '',
    is_dir INTEGER NOT NULL DEFAULT 0,
    content TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    overview TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_uri);
CREATE INDEX IF NOT EXISTS idx_nodes_uri ON nodes(uri);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, content, abstract, overview,
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS nodes_fts_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, name, content, abstract, overview)
    VALUES (new.id, new.name, new.content, new.abstract, new.overview);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, content, abstract, overview)
    VALUES ('delete', old.id, old.name, old.content, old.abstract, old.overview);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, content, abstract, overview)
    VALUES ('delete', old.id, old.name, old.content, old.abstract, old.overview);
    INSERT INTO nodes_fts(rowid, name, content, abstract, overview)
    VALUES (new.id, new.name, new.content, new.abstract, new.overview);
END;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    turn_count INTEGER NOT NULL DEFAULT 0,
    committed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT DEFAULT '',
    parts_json TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    category TEXT NOT NULL DEFAULT 'general',
    content TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, category,
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, category)
    VALUES (new.id, new.content, new.category);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category)
    VALUES ('delete', old.id, old.content, old.category);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category)
    VALUES ('delete', old.id, old.content, old.category);
    INSERT INTO memories_fts(rowid, content, category)
    VALUES (new.id, new.content, new.category);
END;

CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uri TEXT UNIQUE NOT NULL,
    source_name TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    content TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_SQLITE_SEED_DIRS = """
INSERT OR IGNORE INTO nodes (uri, name, parent_uri, is_dir) VALUES ('memory://', '', '', 1);
INSERT OR IGNORE INTO nodes (uri, name, parent_uri, is_dir) VALUES ('memory://user', 'user', 'memory://', 1);
INSERT OR IGNORE INTO nodes (uri, name, parent_uri, is_dir) VALUES ('memory://user/$AGENT', '$AGENT', 'memory://user', 1);
INSERT OR IGNORE INTO nodes (uri, name, parent_uri, is_dir) VALUES ('memory://user/$AGENT/memories', 'memories', 'memory://user/$AGENT', 1);
INSERT OR IGNORE INTO nodes (uri, name, parent_uri, is_dir) VALUES ('memory://user/$AGENT/skills', 'skills', 'memory://user/$AGENT', 1);
INSERT OR IGNORE INTO nodes (uri, name, parent_uri, is_dir) VALUES ('memory://resources', 'resources', 'memory://', 1);
"""


class _MemoryEnhancerSQLite:
    """Direct SQLite backend for Memory Enhancer — replaces the HTTP client."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._agent = os.environ.get("MEMORY_ENHANCER_AGENT", "hermes")
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(_SQLITE_INIT)
            # Seed default directories, substituting agent name
            for stmt in _SQLITE_SEED_DIRS.split(";"):
                stmt = stmt.strip()
                if not stmt:
                    continue
                sql = stmt.replace("$AGENT", self._agent)
                cur.execute(sql)
            self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def health(self) -> bool:
        try:
            cur = self._conn.execute("SELECT 1")
            return cur.fetchone() is not None
        except Exception:
            return False

    def _node_from_row(self, row: sqlite3.Row) -> dict:
        return {
            "uri": row["uri"],
            "name": row["name"],
            "isDir": bool(row["is_dir"]),
            "type": "dir" if row["is_dir"] else "file",
            "abstract": row["abstract"] or "",
            "content": row["content"] or "",
            "overview": row["overview"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # -- Filesystem operations -----------------------------------------------

    def ls(self, uri: str) -> dict:
        children = []
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM nodes WHERE parent_uri = ? ORDER BY is_dir DESC, name ASC",
                (uri,),
            )
            for row in cur.fetchall():
                children.append(self._node_from_row(row))
        return {"entries": children}

    def tree(self, uri: str) -> list:
        """Recursive tree listing."""
        result = []
        with self._lock:
            self._build_tree(uri, result, 0)
        return result

    def _build_tree(self, uri: str, result: list, depth: int):
        cur = self._conn.execute(
            "SELECT * FROM nodes WHERE parent_uri = ? ORDER BY is_dir DESC, name ASC",
            (uri,),
        )
        for row in cur.fetchall():
            node = self._node_from_row(row)
            node["depth"] = depth
            result.append(node)
            if node["isDir"]:
                self._build_tree(row["uri"], result, depth + 1)

    def stat(self, uri: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM nodes WHERE uri = ?", (uri,))
            row = cur.fetchone()
            if row is None:
                return None
            return self._node_from_row(row)

    # -- Content operations --------------------------------------------------

    def read(self, uri: str) -> str:
        with self._lock:
            cur = self._conn.execute(
                "SELECT content FROM nodes WHERE uri = ?", (uri,)
            )
            row = cur.fetchone()
            return row["content"] if row else ""

    def abstract(self, uri: str) -> str:
        with self._lock:
            cur = self._conn.execute(
                "SELECT abstract FROM nodes WHERE uri = ?", (uri,)
            )
            row = cur.fetchone()
            return row["abstract"] if row else ""

    def overview(self, uri: str) -> str:
        with self._lock:
            cur = self._conn.execute(
                "SELECT overview FROM nodes WHERE uri = ?", (uri,)
            )
            row = cur.fetchone()
            return row["overview"] if row else ""

    # -- Search --------------------------------------------------------------

    def search(self, query: str, top_k: int = 10, scope: str = "") -> dict:
        """Full-text search across nodes and memories."""
        memories = []
        resources = []
        skills = []

        scope_filter = ""
        params: list = [query]
        if scope:
            scope_filter = " AND uri LIKE ?"
            params.append(scope + "%")

        with self._lock:
            try:
                cur = self._conn.execute(
                    f"SELECT n.*, rank FROM nodes_fts f JOIN nodes n ON f.rowid = n.id "
                    f"WHERE nodes_fts MATCH ?{scope_filter} ORDER BY rank LIMIT ?",
                    params + [top_k],
                )
                for row in cur.fetchall():
                    node = self._node_from_row(row)
                    node["score"] = 1.0 / (1.0 + abs(row["rank"])) if row["rank"] else 1.0
                    uri = node["uri"]
                    if uri.startswith("memory://resources"):
                        resources.append(node)
                    elif uri.startswith("memory://user"):
                        if "/skills/" in uri:
                            skills.append(node)
                        else:
                            memories.append(node)
                    else:
                        memories.append(node)
            except sqlite3.OperationalError:
                pass  # FTS query syntax error — return empty

        try:
            cur = self._conn.execute(
                "SELECT m.*, rank FROM memories_fts f JOIN memories m ON f.rowid = m.id "
                "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                [query, top_k],
            )
            for row in cur.fetchall():
                entry = {
                    "uri": f"memory://user/{self._agent}/memories/{row['id']}",
                    "score": 1.0 / (1.0 + abs(row["rank"])) if row["rank"] else 1.0,
                    "abstract": _truncate(row["content"], _DEFAULT_MAX_ABSTRACT_CHARS),
                    "type": "memory",
                    "category": row["category"],
                }
                memories.append(entry)
        except sqlite3.OperationalError:
            pass

        return {
            "memories": memories[:top_k],
            "resources": resources[:top_k],
            "skills": skills[:top_k],
            "total": len(memories) + len(resources) + len(skills),
        }

    # -- Session management --------------------------------------------------

    def ensure_session(self, session_id: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions (id) VALUES (?)",
                (session_id,),
            )
            self._conn.commit()

    def add_message(self, session_id: str, role: str, content: str = "",
                    parts: list | None = None):
        parts_json_str = json.dumps(parts, ensure_ascii=False) if parts else ""
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (session_id, role, content, parts_json) VALUES (?, ?, ?, ?)",
                (session_id, role, content, parts_json_str),
            )
            self._conn.commit()

    def increment_turn_count(self, session_id: str):
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET turn_count = turn_count + 1 WHERE id = ?",
                (session_id,),
            )
            self._conn.commit()

    def commit_session(self, session_id: str) -> int:
        """Commit session and extract [Remember] messages as memories.
        Returns the number of messages processed."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT turn_count FROM sessions WHERE id = ?", (session_id,)
            )
            row = cur.fetchone()
            if row is None:
                return 0
            turn_count = row["turn_count"]
            if turn_count == 0:
                return 0

            # Extract [Remember] messages as explicit memories
            cur = self._conn.execute(
                "SELECT content FROM messages WHERE session_id = ? AND role = 'user' AND content LIKE '[Remember%'",
                (session_id,),
            )
            extracted = 0
            for msg_row in cur.fetchall():
                text = msg_row["content"]
                category = "general"
                content_text = text
                if text.startswith("[Remember — "):
                    end_bracket = text.find("]")
                    if end_bracket > 0:
                        category = text[12:end_bracket].strip().lower()
                        content_text = text[end_bracket + 1:].strip()
                elif text.startswith("[Remember]"):
                    content_text = text[10:].strip()
                elif text.startswith("[Remember"):
                    rest = text[9:].strip()
                    if rest.startswith("— "):
                        end_bracket = rest.find("]")
                        if end_bracket > 0:
                            category = rest[2:end_bracket].strip().lower()
                            content_text = rest[end_bracket + 1:].strip()

                if content_text:
                    self._conn.execute(
                        "INSERT INTO memories (session_id, category, content) VALUES (?, ?, ?)",
                        (session_id, category, content_text),
                    )
                    extracted += 1

            self._conn.execute(
                "UPDATE sessions SET committed = 1 WHERE id = ?",
                (session_id,),
            )
            self._conn.commit()
            return turn_count

    def get_messages(self, session_id: str) -> List[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_session_messages_text(self, session_id: str, max_chars: int) -> str:
        """Get session messages as concatenated text for prefetch context."""
        messages = self.get_messages(session_id)
        parts = []
        remaining = max_chars
        for msg in messages:
            text = msg["content"]
            if not text:
                continue
            line = f"[{msg['role']}] {_redact_secrets(text)}"
            if len(line) > remaining:
                line = _truncate(line, remaining)
            parts.append(line)
            remaining -= len(line) + 1
            if remaining <= 0:
                break
        return "\n".join(parts)

    # -- Memory operations ---------------------------------------------------

    def store_memory(self, content: str, category: str, session_id: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO memories (session_id, category, content) VALUES (?, ?, ?)",
                (session_id, category, content),
            )
            self._conn.commit()

    def get_recent_memories(self, limit: int = 10) -> List[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    # -- Resource operations -------------------------------------------------

    def add_resource(self, uri: str, source_name: str, content: str,
                     abstract: str = "", source_url: str = "") -> str:
        # Auto-create parent directory nodes
        parts = uri.rstrip("/").split("/")
        path_segments: list[str] = []
        for part in parts[:-1]:
            if not part or part == "memory:":
                continue  # Skip empty parts and "memory:" scheme
            path_segments.append(part)
            parent_path = f"memory://{'/'.join(path_segments[:-1])}" if len(path_segments) > 1 else "memory://"
            current_path = f"memory://{'/'.join(path_segments)}"
            with self._lock:
                self._conn.execute(
                    "INSERT OR IGNORE INTO nodes (uri, name, parent_uri, is_dir) VALUES (?, ?, ?, 1)",
                    (current_path, part, parent_path),
                )

        name = parts[-1] if parts else source_name
        parent_uri = f"memory://{'/'.join(path_segments)}" if path_segments else "memory://"
        abstract_text = abstract or _truncate(content, _DEFAULT_MAX_ABSTRACT_CHARS)
        overview_text = abstract_text

        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO nodes
                   (uri, name, parent_uri, is_dir, content, abstract, overview)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (uri, name, parent_uri, content, abstract_text, overview_text),
            )
            self._conn.execute(
                """INSERT OR REPLACE INTO resources
                   (uri, source_name, content, abstract, size, source_url)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (uri, source_name, content, abstract_text, len(content), source_url),
            )
            self._conn.commit()

        return uri


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "memory_enhancer_search",
    "description": (
        "Semantic search over the Memory Enhancer knowledge base. "
        "Returns ranked results with memory:// URIs for deeper reading. "
        "Use mode='deep' for complex queries that need reasoning across "
        "multiple sources, 'fast' for simple lookups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "mode": {
                "type": "string", "enum": ["auto", "fast", "deep"],
                "description": "Search depth (default: auto).",
            },
            "scope": {
                "type": "string",
                "description": "Memory URI prefix to scope search (e.g. 'memory://resources/docs/').",
            },
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
        },
        "required": ["query"],
    },
}

READ_SCHEMA = {
    "name": "memory_enhancer_read",
    "description": (
        "Read content at a memory:// URI. Three detail levels:\n"
        "  abstract — ~100 token summary (L0)\n"
        "  overview — ~2k token key points (L1)\n"
        "  full — complete content (L2)\n"
        "Start with abstract/overview, only use full when you need details."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {"type": "string", "description": "memory:// URI to read."},
            "level": {
                "type": "string", "enum": ["abstract", "overview", "full"],
                "description": "Detail level (default: overview).",
            },
        },
        "required": ["uri"],
    },
}

BROWSE_SCHEMA = {
    "name": "memory_enhancer_browse",
    "description": (
        "Browse the Memory Enhancer knowledge store like a filesystem.\n"
        "  list — show directory contents\n"
        "  tree — show hierarchy\n"
        "  stat — show metadata for a URI"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string", "enum": ["tree", "list", "stat"],
                "description": "Browse action.",
            },
            "path": {
                "type": "string",
                "description": "Memory URI path (default: memory://). Examples: 'memory://resources/', 'memory://user/memories/'.",
            },
        },
        "required": ["action"],
    },
}

REMEMBER_SCHEMA = {
    "name": "memory_enhancer_remember",
    "description": (
        "Explicitly store a fact or memory in the Memory Enhancer knowledge base. "
        "Use for important information the agent should remember long-term. "
        "The system automatically categorizes and indexes the memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to remember."},
            "category": {
                "type": "string",
                "enum": ["preference", "entity", "event", "case", "pattern"],
                "description": "Memory category (default: auto-detected).",
            },
        },
        "required": ["content"],
    },
}

ADD_RESOURCE_SCHEMA = {
    "name": "memory_enhancer_add_resource",
    "description": (
        "Add a local file to the Memory Enhancer knowledge base. "
        "Disabled by default; requires MEMORY_ENHANCER_ENABLE_ADD_RESOURCE=true. "
        "Also requires MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS. "
        "The system automatically parses, indexes, and generates summaries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Local file path to add."},
            "reason": {
                "type": "string",
                "description": "Why this resource is relevant (improves search).",
            },
            "to": {
                "type": "string",
                "description": "Optional target memory:// URI for the resource.",
            },
            "parent": {
                "type": "string",
                "description": "Optional parent memory:// URI. Cannot be used with to.",
            },
        },
        "required": ["url"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class HermesMemoryEnhancerProvider(MemoryProvider):
    """Full bidirectional memory via SQLite-backed knowledge store."""

    def __init__(self):
        self._db: Optional[_MemoryEnhancerSQLite] = None
        self._db_path = ""
        self._session_id = ""
        self._turn_count = 0
        self._sync_thread: Optional[threading.Thread] = None
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_top_k = _DEFAULT_PREFETCH_TOP_K
        self._max_abstract_chars = _DEFAULT_MAX_ABSTRACT_CHARS
        self._sync_max_chars = _DEFAULT_SYNC_MAX_CHARS
        self._redact_secrets = True
        self._enable_add_resource = False

    @property
    def name(self) -> str:
        return "hermes_memory_enhancer"

    def is_available(self) -> bool:
        return bool(os.environ.get("MEMORY_ENHANCER_DB_PATH"))

    def get_config_schema(self):
        return [
            {
                "key": "db_path",
                "description": "Path to SQLite database file",
                "required": True,
                "default": "$HOME/.hermes/memory_enhancer/memory.sqlite3",
                "env_var": "MEMORY_ENHANCER_DB_PATH",
            },
            {
                "key": "account",
                "description": "Memory Enhancer tenant account ID",
                "default": "default",
                "env_var": "MEMORY_ENHANCER_ACCOUNT",
            },
            {
                "key": "user",
                "description": "Memory Enhancer user ID within the account",
                "default": "default",
                "env_var": "MEMORY_ENHANCER_USER",
            },
            {
                "key": "agent",
                "description": "Memory Enhancer agent ID within the account",
                "default": "hermes",
                "env_var": "MEMORY_ENHANCER_AGENT",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._db_path = os.environ.get(
            "MEMORY_ENHANCER_DB_PATH",
            str(Path.home() / ".hermes" / "memory_enhancer" / "memory.sqlite3"),
        )
        self._account = os.environ.get("MEMORY_ENHANCER_ACCOUNT", "default")
        self._user = os.environ.get("MEMORY_ENHANCER_USER", "default")
        self._agent = os.environ.get("MEMORY_ENHANCER_AGENT", "hermes")
        self._session_id = session_id
        self._turn_count = 0
        self._prefetch_top_k = _env_int("MEMORY_ENHANCER_PREFETCH_TOP_K", _DEFAULT_PREFETCH_TOP_K, minimum=0, maximum=10)
        self._max_abstract_chars = _env_int("MEMORY_ENHANCER_MAX_ABSTRACT_CHARS", _DEFAULT_MAX_ABSTRACT_CHARS, minimum=100, maximum=2000)
        self._sync_max_chars = _env_int("MEMORY_ENHANCER_SYNC_MAX_CHARS", _DEFAULT_SYNC_MAX_CHARS, minimum=500, maximum=12000)
        self._redact_secrets = _env_bool("MEMORY_ENHANCER_REDACT_SECRETS", True)
        self._enable_add_resource = _env_bool("MEMORY_ENHANCER_ENABLE_ADD_RESOURCE", False)

        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = _MemoryEnhancerSQLite(self._db_path)
            if not self._db.health():
                logger.warning("Memory Enhancer database at %s is not accessible", self._db_path)
                self._db = None
        except Exception as e:
            logger.warning("Memory Enhancer failed to initialize: %s", e)
            self._db = None

        # Register as the last active provider for atexit safety net
        global _last_active_provider
        _last_active_provider = self

    def system_prompt_block(self) -> str:
        if not self._db:
            return ""
        try:
            result = self._db.ls("memory://")
            entries = result.get("entries", []) if isinstance(result, dict) else []
            children = len(entries)
            if children == 0:
                return ""
            return (
                "# Memory Enhancer Knowledge Base\n"
                f"Active. Database: {self._db_path}\n"
                "Use memory_enhancer_search to find information, memory_enhancer_read for details "
                "(abstract/overview/full), memory_enhancer_browse to explore.\n"
                "Use memory_enhancer_remember to store durable facts. "
                "memory_enhancer_add_resource is disabled unless explicitly enabled."
            )
        except Exception as e:
            logger.warning("Memory Enhancer system_prompt_block failed: %s", e)
            return (
                "# Memory Enhancer Knowledge Base\n"
                f"Active. Database: {self._db_path}\n"
                "Use memory_enhancer_search, memory_enhancer_read, memory_enhancer_browse, "
                "memory_enhancer_remember. memory_enhancer_add_resource is disabled unless explicitly enabled."
            )

    def _format_prefetch_result(self, result: Dict[str, Any]) -> str:
        parts = []
        remaining = self._sync_max_chars
        for ctx_type in ("memories", "resources", "skills"):
            if remaining <= 0:
                break
            items = result.get(ctx_type, []) if isinstance(result, dict) else []
            for item in items[:self._prefetch_top_k]:
                if remaining <= 0:
                    break
                uri = item.get("uri", "")
                abstract = item.get("abstract", "")
                if self._redact_secrets:
                    abstract = _redact_secrets(abstract)
                abstract = _truncate(abstract, self._max_abstract_chars)
                score = item.get("score", 0)
                if abstract:
                    try:
                        score_text = f"{float(score):.2f}"
                    except Exception:
                        score_text = "0.00"
                    line = f"- [{score_text}] {abstract} ({uri})"
                    if len(line) > remaining:
                        line = _truncate(line, remaining)
                    parts.append(line)
                    remaining -= len(line) + 1
        return "\n".join(parts)

    def _sanitize_prefetch_query(self, query: str) -> str:
        if self._redact_secrets:
            query = _redact_secrets(query)
        return _truncate(query, self._sync_max_chars)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result and self._db and query:
            try:
                sanitized_query = self._sanitize_prefetch_query(query)
                resp = self._db.search(sanitized_query, top_k=self._prefetch_top_k)
                result = self._format_prefetch_result(resp)
            except Exception as e:
                logger.debug("Memory Enhancer synchronous prefetch failed: %s", e)
                result = ""
        if not result:
            return ""
        return f"## Memory Enhancer Context\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._db or not query:
            return

        def _run():
            try:
                db = _MemoryEnhancerSQLite(self._db_path)
                sanitized_query = self._sanitize_prefetch_query(query)
                resp = db.search(sanitized_query, top_k=self._prefetch_top_k)
                formatted = self._format_prefetch_result(resp)
                if formatted:
                    with self._prefetch_lock:
                        self._prefetch_result = formatted
            except Exception as e:
                logger.debug("Memory Enhancer prefetch failed: %s", e)
            finally:
                db.close()

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="hermes_memory_enhancer-prefetch"
        )
        self._prefetch_thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not self._db:
            return

        sid = self._session_id
        self._db.ensure_session(sid)
        self._turn_count += 1

        def _sync():
            try:
                db = _MemoryEnhancerSQLite(self._db_path)

                user_text = user_content
                assistant_text = assistant_content
                if self._redact_secrets:
                    user_text = _redact_secrets(user_text)
                    assistant_text = _redact_secrets(assistant_text)
                user_text = _truncate(user_text, self._sync_max_chars)
                assistant_text = _truncate(assistant_text, self._sync_max_chars)

                db.add_message(sid, "user", user_text)
                db.add_message(sid, "assistant", assistant_text)
                db.increment_turn_count(sid)
            except Exception as e:
                logger.debug("Memory Enhancer sync_turn failed: %s", e)
            finally:
                db.close()

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="hermes_memory_enhancer-sync"
        )
        self._sync_thread.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._db:
            return

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)

        if self._turn_count == 0:
            return

        try:
            turn_count = self._db.commit_session(self._session_id)
            logger.info(
                "Memory Enhancer session %s committed (%d turns)",
                self._session_id, turn_count,
            )
        except Exception as e:
            logger.warning("Memory Enhancer session commit failed: %s", e)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        if not self._db or action != "add" or not content:
            return

        def _write():
            try:
                db = _MemoryEnhancerSQLite(self._db_path)
                memory_content = content
                if self._redact_secrets:
                    memory_content = _redact_secrets(memory_content)
                memory_content = _truncate(memory_content, self._sync_max_chars)
                db.ensure_session(self._session_id)
                db.add_message(
                    self._session_id, "user",
                    f"[Memory note — {target}] {memory_content}",
                )
            except Exception as e:
                logger.debug("Memory Enhancer memory mirror failed: %s", e)
            finally:
                db.close()

        t = threading.Thread(target=_write, daemon=True, name="hermes_memory_enhancer-memwrite")
        t.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, READ_SCHEMA, BROWSE_SCHEMA, REMEMBER_SCHEMA, ADD_RESOURCE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._db:
            return tool_error("Memory Enhancer database not connected")

        try:
            if tool_name == "memory_enhancer_search":
                return self._tool_search(args)
            elif tool_name == "memory_enhancer_read":
                return self._tool_read(args)
            elif tool_name == "memory_enhancer_browse":
                return self._tool_browse(args)
            elif tool_name == "memory_enhancer_remember":
                return self._tool_remember(args)
            elif tool_name == "memory_enhancer_add_resource":
                return self._tool_add_resource(args)
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            return tool_error(str(e))

    def shutdown(self) -> None:
        for t in (self._sync_thread, self._prefetch_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
        global _last_active_provider
        if _last_active_provider is self:
            _last_active_provider = None

    # -- Tool implementations ------------------------------------------------

    @staticmethod
    def _normalize_summary_uri(uri: str) -> str:
        if not uri:
            return uri
        for suffix in ("/.abstract.md", "/.overview.md", "/.read.md", "/.full.md"):
            if uri.endswith(suffix):
                return uri[: -len(suffix)] or "memory://"
        return uri

    def _is_directory_uri(self, uri: str) -> bool | None:
        try:
            node = self._db.stat(uri)
            if node is None:
                return None
            return node.get("isDir", False)
        except Exception:
            return None

    def _tool_search(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return tool_error("query is required")
        if self._redact_secrets:
            query = _redact_secrets(query)

        top_k = args.get("limit", 10)
        scope = args.get("scope", "")

        resp = self._db.search(query, top_k=top_k, scope=scope)

        scored_entries = []
        for ctx_type in ("memories", "resources", "skills"):
            items = resp.get(ctx_type, [])
            for item in items:
                raw_score = item.get("score")
                sort_score = raw_score if raw_score is not None else 0.0
                abstract = item.get("abstract", "")
                if self._redact_secrets:
                    abstract = _redact_secrets(abstract)
                abstract = _truncate(abstract, self._max_abstract_chars)
                entry = {
                    "uri": item.get("uri", ""),
                    "type": ctx_type.rstrip("s"),
                    "score": round(raw_score, 3) if raw_score is not None else 0.0,
                    "abstract": abstract,
                }
                scored_entries.append((sort_score, entry))

        scored_entries.sort(key=lambda x: x[0], reverse=True)
        formatted = [entry for _, entry in scored_entries]

        return json.dumps({
            "results": formatted,
            "total": resp.get("total", len(formatted)),
        }, ensure_ascii=False)

    def _tool_read(self, args: dict) -> str:
        uri = args.get("uri", "")
        if not uri:
            return tool_error("uri is required")

        level = args.get("level", "overview")

        summary_level = level in ("abstract", "overview")
        resolved_uri = self._normalize_summary_uri(uri) if summary_level else uri
        used_fallback = False

        if summary_level and resolved_uri == uri:
            is_dir = self._is_directory_uri(uri)
            if is_dir is False:
                resolved_uri = uri
                used_fallback = True

        try:
            if level == "abstract":
                content = self._db.abstract(resolved_uri)
            elif level == "overview":
                content = self._db.overview(resolved_uri)
            else:
                content = self._db.read(resolved_uri)
        except Exception:
            if not summary_level or resolved_uri != uri or used_fallback:
                raise
            content = self._db.read(uri)
            used_fallback = True

        if not content:
            # Fallback: try full read if summary returned empty
            if summary_level and not used_fallback:
                content = self._db.read(resolved_uri)
                used_fallback = True

        max_len = 8000
        if level == "overview":
            max_len = 4000
        elif level == "abstract":
            max_len = 1200

        if self._redact_secrets:
            content = _redact_secrets(content)
        if len(content) > max_len:
            content = content[:max_len] + "\n\n[... truncated, use a more specific URI or full level]"

        payload = {
            "uri": uri,
            "resolved_uri": resolved_uri,
            "level": level,
            "content": content,
        }
        if used_fallback:
            payload["fallback"] = "content/read"

        return json.dumps(payload, ensure_ascii=False)

    def _tool_browse(self, args: dict) -> str:
        action = args.get("action", "list")
        path = args.get("path", "memory://")

        try:
            if action == "tree":
                result = self._db.tree(path)
                # Format for readability
                entries = []
                for node in result[:50]:
                    entries.append({
                        "name": node.get("name", ""),
                        "uri": node.get("uri", ""),
                        "type": "dir" if node.get("isDir") else "file",
                        "depth": node.get("depth", 0),
                        "abstract": _truncate(
                            _redact_secrets(node.get("abstract", ""))
                            if self._redact_secrets else node.get("abstract", ""),
                            self._max_abstract_chars,
                        ),
                    })
                return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)

            elif action == "stat":
                node = self._db.stat(path)
                if node is None:
                    return json.dumps({"error": f"URI not found: {path}"}, ensure_ascii=False)
                return json.dumps({
                    "uri": node["uri"],
                    "name": node["name"],
                    "type": "dir" if node["isDir"] else "file",
                    "abstract": node.get("abstract", ""),
                    "created_at": node.get("created_at", ""),
                    "updated_at": node.get("updated_at", ""),
                }, ensure_ascii=False)

            else:  # list
                result = self._db.ls(path)
                raw_entries = result.get("entries", [])
                entries = []
                for e in raw_entries[:50]:
                    abstract = e.get("abstract", "")
                    if self._redact_secrets:
                        abstract = _redact_secrets(abstract)
                    entries.append({
                        "name": e.get("name", ""),
                        "uri": e.get("uri", ""),
                        "type": "dir" if e.get("isDir") else "file",
                        "abstract": _truncate(abstract, self._max_abstract_chars),
                    })
                return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)

        except Exception as e:
            return tool_error(str(e))

    def _tool_remember(self, args: dict) -> str:
        content = args.get("content", "")
        if not content:
            return tool_error("content is required")
        if self._redact_secrets:
            content = _redact_secrets(content)
        content = _truncate(content, self._sync_max_chars)

        category = args.get("category", "general")

        self._db.ensure_session(self._session_id)
        # Store as message for commit extraction
        text = f"[Remember] {content}"
        if category:
            text = f"[Remember — {category}] {content}"
        self._db.add_message(self._session_id, "user", text)
        # Also store directly as a memory
        self._db.store_memory(content, category, self._session_id)

        return json.dumps({
            "status": "stored",
            "message": "Memory recorded. Will be extracted and indexed on session commit.",
        })

    def _tool_add_resource(self, args: dict) -> str:
        if not self._enable_add_resource:
            return tool_error(
                "memory_enhancer_add_resource is disabled by default. Set "
                "MEMORY_ENHANCER_ENABLE_ADD_RESOURCE=true and "
                "MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS for local uploads."
            )
        url = args.get("url", "")
        if not url:
            return tool_error("url is required")

        if args.get("to") and args.get("parent"):
            return tool_error("Cannot specify both 'to' and 'parent'")

        parsed_url = urlparse(url)

        if _is_remote_resource_source(url):
            return tool_error(
                "Remote URL resources are not supported in direct SQLite mode. "
                "Use a local file path instead."
            )

        source_path = Path(url).expanduser()

        local_security_error = _local_upload_security_error(source_path)
        if local_security_error:
            return tool_error(local_security_error)

        if not source_path.exists():
            return tool_error(f"Local resource path does not exist: {url}")

        try:
            content = source_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return tool_error(f"Failed to read file: {e}")

        target_uri = args.get("to", "")
        parent_uri = args.get("parent", "")

        if not target_uri:
            name = source_path.name
            if parent_uri:
                target_uri = parent_uri.rstrip("/") + "/" + name
            else:
                target_uri = f"memory://resources/{name}"

        source_url = str(source_path)
        reason = args.get("reason", "")

        root_uri = self._db.add_resource(
            uri=target_uri,
            source_name=source_path.name,
            content=content,
            abstract=reason or _truncate(content, _DEFAULT_MAX_ABSTRACT_CHARS),
            source_url=source_url,
        )

        return json.dumps({
            "status": "added",
            "root_uri": root_uri,
            "message": "Resource added to knowledge base. Use memory_enhancer_search to find it.",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register Memory Enhancer as a memory provider plugin."""
    ctx.register_memory_provider(HermesMemoryEnhancerProvider())
