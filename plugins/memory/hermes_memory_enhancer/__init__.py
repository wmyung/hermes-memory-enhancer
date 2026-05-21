"""Hermes Memory Enhancer plugin — full bidirectional MemoryProvider interface.

Self-hosted memory layer that organizes agent knowledge into a filesystem-like
hierarchy (memory:// URIs) with tiered context loading, automatic memory
extraction, and session management.

Config via environment variables (profile-scoped via each profile's .env):
  MEMORY_ENHANCER_ENDPOINT  — Server URL (default: http://127.0.0.1:1933)
  MEMORY_ENHANCER_API_KEY   — API key (required for authenticated servers)
  MEMORY_ENHANCER_ACCOUNT   — Tenant account (default: default)
  MEMORY_ENHANCER_USER      — Tenant user (default: default)
  MEMORY_ENHANCER_AGENT   — Tenant agent (default: hermes)

Capabilities:
  - Automatic memory extraction on session commit (6 categories)
  - Tiered context: L0 (~100 tokens), L1 (~2k), L2 (full)
  - Semantic search with hierarchical directory retrieval
  - Filesystem-style browsing via memory:// URIs
  - Resource ingestion (URLs, docs, code)
"""

from __future__ import annotations

import atexit
import json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "http://127.0.0.1:1933"
_TIMEOUT = 30.0
_REMOTE_RESOURCE_PREFIXES = ("http://", "https://", "git@", "ssh://", "git://")
_DEFAULT_PREFETCH_TOP_K = 3
_DEFAULT_MAX_ABSTRACT_CHARS = 500
_DEFAULT_SYNC_MAX_CHARS = 4000
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
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
# Process-level atexit safety net — ensures pending sessions are committed
# even if shutdown_memory_provider is never called (e.g. gateway crash,
# SIGKILL, or exception in the session expiry watcher preventing shutdown).
# ---------------------------------------------------------------------------
_last_active_provider: Optional["HermesMemoryEnhancerProvider"] = None


def _atexit_commit_sessions():
    """Fire on_session_end for the last active provider on process exit."""
    global _last_active_provider
    provider = _last_active_provider
    if provider is None:
        return
    _last_active_provider = None
    try:
        provider.on_session_end([])
    except Exception:
        pass  # best-effort at shutdown time


atexit.register(_atexit_commit_sessions)


# ---------------------------------------------------------------------------
# HTTP helper — uses httpx to avoid requiring the hermes_memory_enhancer SDK
# ---------------------------------------------------------------------------

def _get_httpx():
    """Lazy import httpx."""
    try:
        import httpx
        return httpx
    except ImportError:
        return None


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


def _endpoint_is_loopback(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def _endpoint_security_error(endpoint: str, api_key: str) -> str:
    parsed = urlparse(endpoint)
    scheme = (parsed.scheme or "http").lower()
    if _endpoint_is_loopback(endpoint):
        return ""
    if scheme != "https" and not _env_bool("MEMORY_ENHANCER_ALLOW_INSECURE_REMOTE", False):
        return (
            "Refusing Memory Enhancer remote non-HTTPS endpoint. Use https://, "
            "127.0.0.1/localhost, or set MEMORY_ENHANCER_ALLOW_INSECURE_REMOTE=true."
        )
    if not api_key and not _env_bool("MEMORY_ENHANCER_ALLOW_UNAUTHENTICATED_REMOTE", False):
        return (
            "Refusing Memory Enhancer remote endpoint without API key. Set "
            "MEMORY_ENHANCER_API_KEY or explicitly allow unauthenticated remote mode."
        )
    return ""


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: m.group(1) + " [REDACTED]" if m.groups() else "[REDACTED]", redacted)
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

class _MemoryEnhancerClient:

    """Thin HTTP client for the Memory Enhancer REST API."""

    def __init__(self, endpoint: str, api_key: str = "",
                 account: str = "", user: str = "", agent: str = ""):
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._account = account or os.environ.get("MEMORY_ENHANCER_ACCOUNT", "default")
        self._user = user or os.environ.get("MEMORY_ENHANCER_USER", "default")
        self._agent = agent or os.environ.get("MEMORY_ENHANCER_AGENT", "hermes")
        self._httpx = _get_httpx()
        if self._httpx is None:
            raise ImportError("httpx is required for Memory Enhancer: pip install httpx")

    def _headers(self) -> dict:
        # Always send tenant headers when account/user are configured.
        # Memory Enhancer uses tenant headers to isolate account/user/agent data.
        h = {
            "Content-Type": "application/json",
            "X-Memory-Enhancer-Agent": self._agent,
        }
        if self._account:
            h["X-Memory-Enhancer-Account"] = self._account
        if self._user:
            h["X-Memory-Enhancer-User"] = self._user
        if self._api_key:
            h["X-API-Key"] = self._api_key
            h["Authorization"] = "Bearer " + self._api_key
        return h

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"

    def _multipart_headers(self) -> dict:
        headers = self._headers()
        headers.pop("Content-Type", None)
        return headers

    def _parse_response(self, resp) -> dict:
        try:
            data = resp.json()
        except Exception:
            data = None

        if resp.status_code >= 400:
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    code = error.get("code", "HTTP_ERROR")
                    message = error.get("message", resp.text)
                    raise RuntimeError(f"{code}: {message}")
                if data.get("status") == "error":
                    raise RuntimeError(str(data))
            resp.raise_for_status()

        if isinstance(data, dict) and data.get("status") == "error":
            error = data.get("error")
            if isinstance(error, dict):
                code = error.get("code", "MEMORY_ENHANCER_ERROR")
                message = error.get("message", "")
                raise RuntimeError(f"{code}: {message}")
            raise RuntimeError(str(data))

        if data is None:
            return {}
        return data

    def get(self, path: str, **kwargs) -> dict:
        resp = self._httpx.get(
            self._url(path), headers=self._headers(), timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def post(self, path: str, payload: dict = None, **kwargs) -> dict:
        resp = self._httpx.post(
            self._url(path), json=payload or {}, headers=self._headers(),
            timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def upload_temp_file(self, file_path: Path) -> str:
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as f:
            resp = self._httpx.post(
                self._url("/api/v1/resources/temp_upload"),
                files={"file": (file_path.name, f, mime_type)},
                headers=self._multipart_headers(),
                timeout=_TIMEOUT,
            )
        data = self._parse_response(resp)
        result = data.get("result", {})
        temp_file_id = result.get("temp_file_id", "")
        if not temp_file_id:
            raise RuntimeError("Memory Enhancer temp upload did not return temp_file_id")
        return temp_file_id

    def health(self) -> bool:
        try:
            resp = self._httpx.get(
                self._url("/health"), headers=self._headers(), timeout=3.0
            )
            return resp.status_code == 200
        except Exception:
            return False


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
        "Add a remote URL or explicitly allowed local file/directory to the Memory Enhancer knowledge base. "
        "Disabled by default; requires MEMORY_ENHANCER_ENABLE_ADD_RESOURCE=true. "
        "Local uploads also require MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS and sensitive paths are refused. "
        "The system automatically parses, indexes, and generates summaries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Remote URL or local file/directory path to add."},
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
            "instruction": {
                "type": "string",
                "description": "Optional processing instruction for semantic extraction.",
            },
            "wait": {
                "type": "boolean",
                "description": "Whether to wait for processing to complete.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds when wait is true.",
            },
        },
        "required": ["url"],
    },
}


def _zip_directory(dir_path: Path) -> Path:
    """Create a temporary zip file containing a directory tree."""
    root = dir_path.resolve()
    zip_path = Path(tempfile.gettempdir()) / f"hermes_memory_enhancer_upload_{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in dir_path.rglob("*"):
            if file_path.is_symlink():
                continue
            if file_path.is_file():
                try:
                    file_path.resolve().relative_to(root)
                except ValueError:
                    continue
                if _local_upload_security_error(file_path):
                    continue
                arcname = str(file_path.relative_to(dir_path)).replace("\\", "/")
                zipf.write(file_path, arcname=arcname)
    return zip_path


def _is_windows_absolute_path(value: str) -> bool:
    return (
        len(value) >= 3
        and value[0].isalpha()
        and value[1] == ":"
        and value[2] in ("/", "\\")
    )


def _is_remote_resource_source(value: str) -> bool:
    return value.startswith(_REMOTE_RESOURCE_PREFIXES)


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


def _path_from_file_uri(uri: str) -> Path | str:
    parsed = urlparse(uri)
    if parsed.netloc not in ("", "localhost"):
        return f"Unsupported non-local file URI: {uri}"
    return Path(url2pathname(parsed.path)).expanduser()


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class HermesMemoryEnhancerProvider(MemoryProvider):
    """Full bidirectional memory via Memory Enhancer context database."""

    def __init__(self):
        self._client: Optional[_MemoryEnhancerClient] = None
        self._endpoint = ""
        self._api_key = ""
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
        """Check if Memory Enhancer endpoint is configured. No network calls."""
        return bool(os.environ.get("MEMORY_ENHANCER_ENDPOINT"))

    def get_config_schema(self):
        return [
            {
                "key": "endpoint",
                "description": "Memory Enhancer server URL",
                "required": True,
                "default": _DEFAULT_ENDPOINT,
                "env_var": "MEMORY_ENHANCER_ENDPOINT",
            },
            {
                "key": "api_key",
                "description": "Memory Enhancer API key (leave blank for local dev mode)",
                "secret": True,
                "env_var": "MEMORY_ENHANCER_API_KEY",
            },
            {
                "key": "account",
                "description": "Memory Enhancer tenant account ID ([default], used when local mode, MEMORY_ENHANCER_API_KEY is empty)",
                "default": "default",
                "env_var": "MEMORY_ENHANCER_ACCOUNT",
            },
            {
                "key": "user",
                "description": "Memory Enhancer user ID within the account ([default], used when local mode, MEMORY_ENHANCER_API_KEY is empty)",
                "default": "default",
                "env_var": "MEMORY_ENHANCER_USER",
            },
            {
                "key": "agent",
                "description": "Memory Enhancer agent ID within the account ([hermes], useful in multi-agent mode)",
                "default": "hermes",
                "env_var": "MEMORY_ENHANCER_AGENT",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._endpoint = os.environ.get("MEMORY_ENHANCER_ENDPOINT", _DEFAULT_ENDPOINT)
        self._api_key = os.environ.get("MEMORY_ENHANCER_API_KEY", "")
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

        security_error = _endpoint_security_error(self._endpoint, self._api_key)
        if security_error:
            logger.warning("%s", security_error)
            self._client = None
            return

        try:
            self._client = _MemoryEnhancerClient(
                self._endpoint, self._api_key,
                account=self._account, user=self._user, agent=self._agent,
            )
            if not self._client.health():
                logger.warning("Memory Enhancer server at %s is not reachable", self._endpoint)
                self._client = None
        except ImportError:
            logger.warning("httpx not installed — Memory Enhancer plugin disabled")
            self._client = None

        # Register as the last active provider for atexit safety net
        global _last_active_provider
        _last_active_provider = self

    def system_prompt_block(self) -> str:
        if not self._client:
            return ""
        # Provide brief info about the knowledge base
        try:
            # Check what's in the knowledge base via a root listing
            resp = self._client.get("/api/v1/fs/ls", params={"uri": "memory://"})
            result = resp.get("result", [])
            children = len(result) if isinstance(result, list) else 0
            if children == 0:
                return ""
            return (
                "# Memory Enhancer Knowledge Base\n"
                f"Active. Endpoint: {self._endpoint}\n"
                "Use memory_enhancer_search to find information, memory_enhancer_read for details "
                "(abstract/overview/full), memory_enhancer_browse to explore.\n"
                "Use memory_enhancer_remember to store durable facts. "
                "memory_enhancer_add_resource is disabled unless explicitly enabled."
            )
        except Exception as e:
            logger.warning("Memory Enhancer system_prompt_block failed: %s", e)
            return (
                "# Memory Enhancer Knowledge Base\n"
                f"Active. Endpoint: {self._endpoint}\n"
                "Use memory_enhancer_search, memory_enhancer_read, memory_enhancer_browse, "
                "memory_enhancer_remember. memory_enhancer_add_resource is disabled unless explicitly enabled."
            )

    def _format_prefetch_result(self, result: Dict[str, Any]) -> str:
        """Format search results for automatic prompt injection."""
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
        """Return relevant Memory Enhancer context for the current turn.

        Prefer the background result queued after the previous turn, but fall
        back to a bounded synchronous search so the first turn of a new session
        also gets relevant context instead of waiting until turn two.
        """
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result and self._client and query:
            try:
                sanitized_query = self._sanitize_prefetch_query(query)
                resp = self._client.post("/api/v1/search/find", {
                    "query": sanitized_query,
                    "top_k": self._prefetch_top_k,
                })
                result = self._format_prefetch_result(resp.get("result", {}))
            except Exception as e:
                logger.debug("Memory Enhancer synchronous prefetch failed: %s", e)
                result = ""
        if not result:
            return ""
        return f"## Memory Enhancer Context\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire a background search to pre-load relevant context."""
        if not self._client or not query:
            return

        def _run():
            try:
                client = _MemoryEnhancerClient(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                sanitized_query = self._sanitize_prefetch_query(query)
                resp = client.post("/api/v1/search/find", {
                    "query": sanitized_query,
                    "top_k": self._prefetch_top_k,
                })
                formatted = self._format_prefetch_result(resp.get("result", {}))
                if formatted:
                    with self._prefetch_lock:
                        self._prefetch_result = formatted
            except Exception as e:
                logger.debug("Memory Enhancer prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="hermes_memory_enhancer-prefetch"
        )
        self._prefetch_thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Record the conversation turn in Memory Enhancer's session (non-blocking)."""
        if not self._client:
            return

        self._turn_count += 1

        def _sync():
            try:
                client = _MemoryEnhancerClient(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                sid = self._session_id

                user_text = user_content
                assistant_text = assistant_content
                if self._redact_secrets:
                    user_text = _redact_secrets(user_text)
                    assistant_text = _redact_secrets(assistant_text)
                user_text = _truncate(user_text, self._sync_max_chars)
                assistant_text = _truncate(assistant_text, self._sync_max_chars)

                # Add user message
                client.post(f"/api/v1/sessions/{sid}/messages", {
                    "role": "user",
                    "content": user_text,
                })
                # Add assistant message
                client.post(f"/api/v1/sessions/{sid}/messages", {
                    "role": "assistant",
                    "content": assistant_text,
                })
            except Exception as e:
                logger.debug("Memory Enhancer sync_turn failed: %s", e)

        # Wait for any previous sync to finish before starting a new one
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="hermes_memory_enhancer-sync"
        )
        self._sync_thread.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Commit the session to trigger memory extraction.

        Memory Enhancer automatically extracts 6 categories of memories:
        profile, preferences, entities, events, cases, and patterns.
        """
        if not self._client:
            return

        # Wait for any pending sync to finish first — do this before the
        # turn_count check so the last turn's messages are flushed even if
        # the count hasn't been incremented yet.
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)

        if self._turn_count == 0:
            return

        try:
            self._client.post(f"/api/v1/sessions/{self._session_id}/commit")
            logger.info("Memory Enhancer session %s committed (%d turns)", self._session_id, self._turn_count)
        except Exception as e:
            logger.warning("Memory Enhancer session commit failed: %s", e)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes to Memory Enhancer as explicit memories."""
        if not self._client or action != "add" or not content:
            return

        def _write():
            try:
                client = _MemoryEnhancerClient(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                memory_content = content
                if self._redact_secrets:
                    memory_content = _redact_secrets(memory_content)
                memory_content = _truncate(memory_content, self._sync_max_chars)
                # Add as a user message with memory context so the commit
                # picks it up as an explicit memory during extraction
                client.post(f"/api/v1/sessions/{self._session_id}/messages", {
                    "role": "user",
                    "parts": [
                        {"type": "text", "text": f"[Memory note — {target}] {memory_content}"},
                    ],
                })
            except Exception as e:
                logger.debug("Memory Enhancer memory mirror failed: %s", e)

        t = threading.Thread(target=_write, daemon=True, name="hermes_memory_enhancer-memwrite")
        t.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, READ_SCHEMA, BROWSE_SCHEMA, REMEMBER_SCHEMA, ADD_RESOURCE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._client:
            return tool_error("Memory Enhancer server not connected")

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
        # Wait for background threads to finish
        for t in (self._sync_thread, self._prefetch_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        # Clear atexit reference so it doesn't double-commit
        global _last_active_provider
        if _last_active_provider is self:
            _last_active_provider = None

    # -- Tool implementations ------------------------------------------------

    @staticmethod
    def _unwrap_result(resp: Any) -> Any:
        """Return Memory Enhancer payload body regardless of wrapped/unwrapped shape."""
        if isinstance(resp, dict) and "result" in resp:
            return resp.get("result")
        return resp

    @staticmethod
    def _normalize_summary_uri(uri: str) -> str:
        """Map pseudo summary files to their parent directory URI for L0/L1 reads."""
        if not uri:
            return uri
        for suffix in ("/.abstract.md", "/.overview.md", "/.read.md", "/.full.md"):
            if uri.endswith(suffix):
                return uri[: -len(suffix)] or "memory://"
        return uri

    def _is_directory_uri(self, uri: str) -> bool | None:
        """Probe fs/stat to decide if a URI is a directory.

        Returns True/False when the server answers cleanly, and None when the
        probe itself fails (network error, unexpected shape). Callers should
        treat None as "unknown" and fall back to the exception-based path.
        """
        try:
            resp = self._client.get("/api/v1/fs/stat", params={"uri": uri})
        except Exception:
            return None
        result = self._unwrap_result(resp)
        if isinstance(result, dict):
            if "isDir" in result:
                return bool(result.get("isDir"))
            if "is_dir" in result:
                return bool(result.get("is_dir"))
            if result.get("type") == "dir":
                return True
            if result.get("type") == "file":
                return False
        return None

    def _tool_search(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return tool_error("query is required")
        if self._redact_secrets:
            query = _redact_secrets(query)

        payload: Dict[str, Any] = {"query": query}
        mode = args.get("mode", "auto")
        if mode != "auto":
            payload["mode"] = mode
        if args.get("scope"):
            payload["target_uri"] = args["scope"]
        if args.get("limit"):
            payload["top_k"] = args["limit"]

        resp = self._client.post("/api/v1/search/find", payload)
        result = resp.get("result", {})

        # Format results for the model — keep it concise
        scored_entries = []
        for ctx_type in ("memories", "resources", "skills"):
            items = result.get(ctx_type, [])
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
                if item.get("relations"):
                    entry["related"] = [r.get("uri") for r in item["relations"][:3]]
                scored_entries.append((sort_score, entry))

        scored_entries.sort(key=lambda x: x[0], reverse=True)
        formatted = [entry for _, entry in scored_entries]

        return json.dumps({
            "results": formatted,
            "total": result.get("total", len(formatted)),
        }, ensure_ascii=False)

    def _tool_read(self, args: dict) -> str:
        uri = args.get("uri", "")
        if not uri:
            return tool_error("uri is required")

        level = args.get("level", "overview")

        summary_level = level in ("abstract", "overview")
        # Memory Enhancer expects directory URIs for pseudo summary files
        # (e.g. memory://user/hermes/.overview.md).
        resolved_uri = self._normalize_summary_uri(uri) if summary_level else uri
        used_fallback = False

        # abstract/overview endpoints are directory-only on Memory Enhancer
        # (v0.3.x returns 500/412 for file URIs). When the caller asks for a
        # summary level on a non-pseudo URI, probe fs/stat first and route
        # file URIs straight to /content/read instead of eating a failing
        # round-trip. The pseudo-URI path already points at a directory, so
        # skip the probe there.
        if summary_level and resolved_uri == uri:
            is_dir = self._is_directory_uri(uri)
            if is_dir is False:
                resolved_uri = uri
                used_fallback = True

        # Map our level names to Memory Enhancer GET endpoints.
        endpoint = "/api/v1/content/read"
        if not used_fallback:
            if level == "abstract":
                endpoint = "/api/v1/content/abstract"
            elif level == "overview":
                endpoint = "/api/v1/content/overview"

        try:
            resp = self._client.get(endpoint, params={"uri": resolved_uri})
        except Exception:
            # Memory Enhancer may return HTTP 500 for abstract/overview reads on normal
            # file URIs (mem_*.md). For those, gracefully fallback to full read.
            if not summary_level or resolved_uri != uri or used_fallback:
                raise
            resp = self._client.get("/api/v1/content/read", params={"uri": uri})
            used_fallback = True

        result = self._unwrap_result(resp)
        # Content endpoints may return either plain strings or objects.
        if isinstance(result, str):
            content = result
        elif isinstance(result, dict):
            content = result.get("content", "") or result.get("text", "")
        else:
            content = ""

        # Truncate long content to avoid flooding context.
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

        # Map action to the correct fs endpoint (all GET with uri= param)
        endpoint_map = {"tree": "/api/v1/fs/tree", "list": "/api/v1/fs/ls", "stat": "/api/v1/fs/stat"}
        endpoint = endpoint_map.get(action, "/api/v1/fs/ls")
        resp = self._client.get(endpoint, params={"uri": path})
        result = self._unwrap_result(resp)

        # Format list/tree results for readability
        if action in ("list", "tree"):
            raw_entries = result
            if isinstance(result, dict):
                raw_entries = result.get("entries") or result.get("items") or result.get("children") or []

            if isinstance(raw_entries, list):
                entries = []
                for e in raw_entries[:50]:  # cap at 50 entries
                    uri = e.get("uri", "")
                    name = e.get("rel_path") or e.get("name") or (uri.rsplit("/", 1)[-1] if uri else "")
                    is_dir = bool(e.get("isDir") or e.get("is_dir") or e.get("type") == "dir")
                    abstract = e.get("abstract", "")
                    if self._redact_secrets:
                        abstract = _redact_secrets(abstract)
                    abstract = _truncate(abstract, self._max_abstract_chars)
                    entries.append({
                        "name": name,
                        "uri": uri,
                        "type": "dir" if is_dir else "file",
                        "abstract": abstract,
                    })
                return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)

        return json.dumps(result, ensure_ascii=False)

    def _tool_remember(self, args: dict) -> str:
        content = args.get("content", "")
        if not content:
            return tool_error("content is required")
        if self._redact_secrets:
            content = _redact_secrets(content)
        content = _truncate(content, self._sync_max_chars)

        # Store as a session message that will be extracted during commit.
        # The category hint helps Memory Enhancer's extraction classify correctly.
        category = args.get("category", "")
        text = f"[Remember] {content}"
        if category:
            text = f"[Remember — {category}] {content}"

        self._client.post(f"/api/v1/sessions/{self._session_id}/messages", {
            "role": "user",
            "parts": [
                {"type": "text", "text": text},
            ],
        })

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

        payload: Dict[str, Any] = {}
        for key in ("reason", "to", "parent", "instruction", "wait", "timeout"):
            if key in args and args[key] not in (None, ""):
                payload[key] = args[key]

        parsed_url = urlparse(url)
        if _is_remote_resource_source(url):
            source_path = None
        elif parsed_url.scheme == "file":
            source_path = _path_from_file_uri(url)
            if isinstance(source_path, str):
                return tool_error(source_path)
        elif parsed_url.scheme and not _is_windows_absolute_path(url):
            source_path = None
        else:
            source_path = Path(url).expanduser()

        cleanup_path: Optional[Path] = None
        try:
            if source_path is not None:
                local_security_error = _local_upload_security_error(source_path)
                if local_security_error:
                    return tool_error(local_security_error)
                if source_path.exists():
                    if source_path.is_dir():
                        payload["source_name"] = source_path.name
                        cleanup_path = _zip_directory(source_path)
                        upload_path = cleanup_path
                    elif source_path.is_file():
                        payload["source_name"] = source_path.name
                        upload_path = source_path
                    else:
                        return tool_error(f"Unsupported local resource path: {url}")
                    payload["temp_file_id"] = self._client.upload_temp_file(upload_path)
                elif _is_local_path_reference(url):
                    return tool_error(f"Local resource path does not exist: {url}")
                else:
                    payload["path"] = url
            else:
                payload["path"] = url

            resp = self._client.post("/api/v1/resources", payload)
            result = resp.get("result", {})
        finally:
            if cleanup_path:
                cleanup_path.unlink(missing_ok=True)

        return json.dumps({
            "status": "added",
            "root_uri": result.get("root_uri", ""),
            "message": "Resource queued for processing. Use memory_enhancer_search after a moment to find it.",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register Memory Enhancer as a memory provider plugin."""
    ctx.register_memory_provider(HermesMemoryEnhancerProvider())
