import json
import os
import tempfile

import pytest

from plugins.memory.hermes_memory_enhancer import (
    HermesMemoryEnhancerProvider,
    _redact_secrets,
    _truncate,
    _local_upload_security_error,
    _is_remote_resource_source,
    _is_local_path_reference,
    _SECRET_PATTERNS,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

class ProviderWithDB:
    """Context manager that creates a provider with temporary SQLite database."""

    def __init__(self, agent="hermes"):
        self.agent = agent
        self._tmp_db = None
        self.provider = None

    def __enter__(self):
        self._tmp_db = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp_db.close()
        os.environ["MEMORY_ENHANCER_DB_PATH"] = self._tmp_db.name
        os.environ["MEMORY_ENHANCER_AGENT"] = self.agent
        self.provider = HermesMemoryEnhancerProvider()
        self.provider.initialize("test-session")
        return self.provider

    def __exit__(self, *args):
        if self.provider:
            try:
                self.provider.shutdown()
            except Exception:
                pass
        if self._tmp_db:
            try:
                os.unlink(self._tmp_db.name)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Search tests
# ---------------------------------------------------------------------------

def test_tool_search_returns_results():
    with ProviderWithDB() as provider:
        # Seed some content via resource
        provider._db.add_resource(
            "memory://resources/paper1.md", "paper1.md",
            "This paper discusses the genetic basis of schizophrenia.",
        )
        provider._db.add_resource(
            "memory://resources/paper2.md", "paper2.md",
            "Another paper about treatment outcomes in depression.",
        )

        result = json.loads(provider._tool_search({"query": "schizophrenia", "limit": 10}))

        assert result["total"] >= 1
        uris = [r["uri"] for r in result["results"]]
        assert any("paper1" in u for u in uris)


def test_tool_search_sorts_by_score():
    with ProviderWithDB() as provider:
        provider._db.add_resource(
            "memory://resources/a.md", "a.md",
            "Machine learning methods for genomic analysis.",
        )
        provider._db.add_resource(
            "memory://resources/b.md", "b.md",
            "Statistical power analysis for genetic studies.",
        )

        result = json.loads(provider._tool_search({"query": "genetic analysis", "limit": 10}))
        assert result["total"] >= 1
        # Results should be sorted by score descending
        scores = [r["score"] for r in result["results"]]
        assert scores == sorted(scores, reverse=True)


def test_tool_search_with_scope():
    with ProviderWithDB() as provider:
        provider._db.add_resource(
            "memory://resources/doc.md", "doc.md",
            "GWAS analysis results for depression.",
        )
        provider._db.add_resource(
            "memory://user/hermes/memories/note.md", "note.md",
            "Personal note about GWAS methodology.",
        )

        # Scope to resources only
        result = json.loads(provider._tool_search({
            "query": "GWAS",
            "scope": "memory://resources",
            "limit": 10,
        }))
        uris = [r["uri"] for r in result["results"]]
        # Only resource-scoped results should appear
        assert len(uris) > 0
        assert all("resources" in u for u in uris)


# ---------------------------------------------------------------------------
# Read tests
# ---------------------------------------------------------------------------

def test_tool_read_nonexistent_uri():
    with ProviderWithDB() as provider:
        result = json.loads(provider._tool_read({
            "uri": "memory://resources/nonexistent.md",
            "level": "full",
        }))
        assert result["content"] == ""


def test_tool_read_overview_file():
    with ProviderWithDB() as provider:
        provider._db.add_resource(
            "memory://resources/doc.md", "doc.md",
            "Full content here.",
            abstract="Abstract text.",
            source_url="/tmp/doc.md",
        )
        # Force overview = abstract for non-dir resources
        result = json.loads(provider._tool_read({
            "uri": "memory://resources/doc.md",
            "level": "overview",
        }))
        assert result["level"] == "overview"
        assert result["content"]  # Should have content (falls back to full)


# ---------------------------------------------------------------------------
# Browse tests
# ---------------------------------------------------------------------------

def test_browse_list():
    with ProviderWithDB() as provider:
        result = json.loads(provider._tool_browse({"action": "list", "path": "memory://"}))
        assert result["path"] == "memory://"
        entries = result["entries"]
        names = {e["name"] for e in entries}
        assert "user" in names
        assert "resources" in names


def test_browse_tree():
    with ProviderWithDB() as provider:
        result = json.loads(provider._tool_browse({"action": "tree", "path": "memory://"}))
        assert "entries" in result
        entries = result["entries"]
        assert len(entries) >= 2
        # Each entry should have depth
        assert all("depth" in e for e in entries)


def test_browse_stat():
    with ProviderWithDB() as provider:
        result = json.loads(provider._tool_browse({"action": "stat", "path": "memory://user"}))
        assert result["uri"] == "memory://user"
        assert result["name"] == "user"
        assert result["type"] == "dir"


# ---------------------------------------------------------------------------
# Remember tests
# ---------------------------------------------------------------------------

def test_remember_explicit_memory():
    with ProviderWithDB() as provider:
        result = json.loads(provider._tool_remember({
            "content": "The user's favorite color is blue",
            "category": "preference",
        }))
        assert result["status"] == "stored"

        # Verify it's searchable
        search_result = json.loads(provider._tool_search({
            "query": "favorite color",
            "limit": 10,
        }))
        assert search_result["total"] >= 1


# ---------------------------------------------------------------------------
# Add resource tests
# ---------------------------------------------------------------------------

def test_add_resource_local_file(tmp_path, monkeypatch):
    sample = tmp_path / "sample.md"
    sample.write_text("# Local resource\n", encoding="utf-8")

    with ProviderWithDB() as provider:
        provider._enable_add_resource = True
        monkeypatch.setenv("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS", str(tmp_path))

        result = json.loads(provider._tool_add_resource({
            "url": str(sample),
            "reason": "local test",
        }))

        assert result["status"] == "added"
        assert "resources" in result["root_uri"]


def test_add_resource_missing_file(monkeypatch):
    with ProviderWithDB() as provider:
        provider._enable_add_resource = True
        monkeypatch.setenv("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS", "/tmp")

        result = json.loads(provider._tool_add_resource({
            "url": "/tmp/nonexistent-12345.md",
        }))

        assert "error" in result


def test_add_resource_rejects_remote_url():
    with ProviderWithDB() as provider:
        provider._enable_add_resource = True

        result = json.loads(provider._tool_add_resource({
            "url": "https://example.com/doc.md",
        }))

        assert "error" in result


def test_add_resource_rejects_when_disabled():
    with ProviderWithDB() as provider:
        result = json.loads(provider._tool_add_resource({
            "url": "/tmp/test.md",
        }))

        assert "error" in result


# ---------------------------------------------------------------------------
# Security tests
# ---------------------------------------------------------------------------

def test_redact_secrets():
    text = "My API key is sk-1234567890abcdef123456 and token=secret123"
    result = _redact_secrets(text)
    assert "sk-1234567890abcdef123456" not in result
    assert "secret123" not in result
    assert "[REDACTED]" in result


def test_truncate_short_text():
    text = "Short text"
    assert _truncate(text, 100) == text


def test_truncate_long_text():
    text = "A" * 1000
    result = _truncate(text, 100)
    assert len(result) <= 100 + 50  # truncation suffix adds some chars
    assert "[... truncated" in result


def test_local_upload_security_error_sensitive_paths():
    from pathlib import Path
    result = _local_upload_security_error(Path("/home/user/.ssh/id_rsa"))
    assert "sensitive" in result.lower() or "refusing" in result.lower()


def test_local_upload_security_error_outside_roots(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS", raising=False)
    result = _local_upload_security_error(tmp_path / "test.md")
    assert result  # Should be an error since MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS is not set


def test_is_remote_resource_source():
    assert _is_remote_resource_source("https://example.com")
    assert _is_remote_resource_source("git@github.com:org/repo.git")
    assert not _is_remote_resource_source("/local/path")
    assert not _is_remote_resource_source("relative/path.md")


def test_is_local_path_reference():
    assert _is_local_path_reference("/absolute/path")
    assert _is_local_path_reference("./relative")
    assert _is_local_path_reference("../parent")
    assert not _is_local_path_reference("https://example.com")


# ---------------------------------------------------------------------------
# Session lifecycle tests
# ---------------------------------------------------------------------------

def test_full_session_flow():
    with ProviderWithDB() as provider:
        # Simulate a conversation
        provider.sync_turn("Hello", "Hi! How can I help?")
        import time
        time.sleep(0.2)

        provider.sync_turn("My project is about PTSD genetics", "I'll remember that.")
        time.sleep(0.2)

        provider._tool_remember({"content": "Project focus: PTSD genetics GWAS", "category": "project"})

        # Commit the session
        provider._turn_count = 2
        provider.on_session_end([])

        # Search should find the remembered content
        result = json.loads(provider._tool_search({
            "query": "PTSD genetics",
            "limit": 10,
        }))
        assert result["total"] >= 1


def test_provider_name_and_availability():
    # Test without env var set
    if "MEMORY_ENHANCER_DB_PATH" in os.environ:
        del os.environ["MEMORY_ENHANCER_DB_PATH"]
    provider = HermesMemoryEnhancerProvider()
    assert provider.name == "hermes_memory_enhancer"
    assert not provider.is_available()

    # Test with env var set
    with ProviderWithDB() as provider2:
        assert provider2.is_available()


def test_initialize_creates_db():
    """Test that initialize creates the database file."""
    tmp_db = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp_db.close()
    try:
        os.environ["MEMORY_ENHANCER_DB_PATH"] = tmp_db.name
        provider = HermesMemoryEnhancerProvider()
        provider.initialize("test-session")
        assert os.path.exists(tmp_db.name)
        provider.shutdown()
    finally:
        try:
            os.unlink(tmp_db.name)
        except OSError:
            pass


def test_system_prompt_block():
    with ProviderWithDB() as provider:
        block = provider.system_prompt_block()
        assert "Memory Enhancer Knowledge Base" in block
        assert "memory_enhancer_search" in block


def test_prefetch_with_content():
    with ProviderWithDB() as provider:
        provider._db.add_resource(
            "memory://resources/research.md", "research.md",
            "Important research about machine learning in psychiatry.",
        )
        result = provider.prefetch("machine learning psychiatry")
        assert result  # Should return non-empty context
        assert "Memory Enhancer Context" in result
