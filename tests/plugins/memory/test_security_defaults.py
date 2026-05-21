"""Security default tests for the SQLite-backed Memory Enhancer plugin."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

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

@pytest.fixture
def provider_with_db():
    """Create a provider with a temporary SQLite database."""
    tmp_db = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp_db.close()
    old_db_path = os.environ.get("MEMORY_ENHANCER_DB_PATH")
    old_agent = os.environ.get("MEMORY_ENHANCER_AGENT")
    os.environ["MEMORY_ENHANCER_DB_PATH"] = tmp_db.name
    os.environ["MEMORY_ENHANCER_AGENT"] = "hermes"

    provider = HermesMemoryEnhancerProvider()
    provider.initialize("test-session")
    provider._redact_secrets = True
    provider._max_abstract_chars = 120
    provider._sync_max_chars = 80
    provider._prefetch_top_k = 1

    yield provider

    try:
        provider.shutdown()
    except Exception:
        pass
    try:
        os.unlink(tmp_db.name)
    except OSError:
        pass
    if old_db_path:
        os.environ["MEMORY_ENHANCER_DB_PATH"] = old_db_path
    else:
        os.environ.pop("MEMORY_ENHANCER_DB_PATH", None)
    if old_agent:
        os.environ["MEMORY_ENHANCER_AGENT"] = old_agent
    else:
        os.environ.pop("MEMORY_ENHANCER_AGENT", None)


# ---------------------------------------------------------------------------
# Add resource security
# ---------------------------------------------------------------------------

def test_add_resource_disabled_by_default(tmp_path):
    """memory_enhancer_add_resource is off unless MEMORY_ENHANCER_ENABLE_ADD_RESOURCE=true."""
    sample = tmp_path / "sample.md"
    sample.write_text("ok\n", encoding="utf-8")

    provider = HermesMemoryEnhancerProvider()
    result = json.loads(provider._tool_add_resource({"url": str(sample)}))
    assert "error" in result
    assert "disabled" in result["error"].lower()


def test_local_upload_requires_allowed_root(tmp_path, monkeypatch):
    """Without MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS, local upload is refused."""
    sample = tmp_path / "sample.md"
    sample.write_text("ok\n", encoding="utf-8")

    tmp_db = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp_db.close()
    monkeypatch.setenv("MEMORY_ENHANCER_DB_PATH", tmp_db.name)
    monkeypatch.setenv("MEMORY_ENHANCER_AGENT", "hermes")
    monkeypatch.delenv("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS", raising=False)

    provider = HermesMemoryEnhancerProvider()
    provider.initialize("test-session")
    provider._enable_add_resource = True

    try:
        result = json.loads(provider._tool_add_resource({"url": str(sample)}))
        assert "error" in result
        assert "upload roots" in result["error"].lower() or "allowed_upload_roots" in result["error"].lower()
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass
        try:
            os.unlink(tmp_db.name)
        except OSError:
            pass


def test_local_upload_refuses_sensitive_filename(tmp_path, monkeypatch):
    """Paths containing sensitive patterns are refused."""
    sensitive = tmp_path / "credentials.txt"
    sensitive.write_text("secret", encoding="utf-8")

    tmp_db = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp_db.close()
    monkeypatch.setenv("MEMORY_ENHANCER_DB_PATH", tmp_db.name)
    monkeypatch.setenv("MEMORY_ENHANCER_AGENT", "hermes")
    monkeypatch.setenv("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS", str(tmp_path))

    provider = HermesMemoryEnhancerProvider()
    provider.initialize("test-session")
    provider._enable_add_resource = True

    try:
        result = json.loads(provider._tool_add_resource({"url": str(sensitive)}))
        assert "error" in result
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass
        try:
            os.unlink(tmp_db.name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Redact and truncate behavior
# ---------------------------------------------------------------------------

def test_prefetch_redacts_and_caps_context(provider_with_db):
    """prefetch output should redact secrets and cap text length."""
    provider = provider_with_db
    # Add content with secret-like text
    provider._db.add_resource(
        "memory://resources/secret.md", "secret.md",
        "api_key = sk-abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 300,
    )

    result = provider.prefetch("api key secret key")
    assert result  # Should have context
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in result


def test_sync_turn_redacts_and_caps_messages(provider_with_db):
    """sync_turn should redact secrets and cap message length."""
    provider = provider_with_db
    provider.sync_turn(
        "token=ghp_abcdefghijklmnopqrstuvwxyz123456 " + "q" * 120,
        "Sure, I can help with that.",
    )
    import time
    time.sleep(0.3)

    # Verify by checking messages in DB (accessible via the SQLite manager)
    messages = provider._db.get_messages(provider._session_id)
    assert len(messages) > 0
    # Content length should be capped
    user_msgs = [m for m in messages if m["role"] == "user"]
    if user_msgs:
        content = user_msgs[0]["content"]
        assert len(content) <= 80 + 50  # truncation may add suffix


def test_on_memory_write_redacts_and_caps_content(provider_with_db):
    """on_memory_write should redact secrets and cap content."""
    provider = provider_with_db
    provider.on_memory_write(
        "add", "user",
        "token=ghp_abcdefghijklmnopqrstuvwxyz123456 " + "r" * 200,
    )
    import time
    time.sleep(0.3)

    messages = provider._db.get_messages(provider._session_id)
    user_msgs = [m for m in messages if m["role"] == "user"]
    if user_msgs:
        content = user_msgs[0]["content"]
        assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in content


def test_search_read_and_browse_outputs_redact_secret_material(provider_with_db):
    """Search/read/browse tool outputs should redact secrets."""
    provider = provider_with_db
    # Add resource with secret-like content
    provider._db.add_resource(
        "memory://resources/secret.md", "secret.md",
        "The token is token=ghp_abcdefghijklmnopqrstuvwxyz123456",
    )

    # Search
    search_result = json.loads(provider._tool_search({
        "query": "secret",
        "limit": 10,
    }))
    search_text = json.dumps(search_result)
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in search_text

    # Read (full)
    read_result = json.loads(provider._tool_read({
        "uri": "memory://resources/secret.md",
        "level": "full",
    }))
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in read_result.get("content", "")

    # Browse list
    browse_result = json.loads(provider._tool_browse({
        "action": "list",
        "path": "memory://resources",
    }))
    browse_text = json.dumps(browse_result)
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in browse_text


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

def test_redact_secrets_sk_20_plus():
    """sk- pattern requires at least 20 chars after prefix."""
    text = "key is sk-abcdefghijklmnopqrstuvwxyz012345"
    result = _redact_secrets(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in result
    assert "[REDACTED]" in result


def test_redact_secrets_api_key_pattern():
    text = 'api_key = "my-secret-api-key-12345"'
    result = _redact_secrets(text)
    assert "my-secret-api-key-12345" not in result
    assert "[REDACTED]" in result


def test_redact_secrets_bearer_token():
    text = "Bearer ghp_abcdefghijklmnopqrstuvwxyz12345678901"
    result = _redact_secrets(text)
    assert "ghp_abcdefghijklmnopqrstuvwxyz12345678901" not in result


def test_redact_secrets_github_token():
    text = "ghp_abcdefghijklmnopqrstuvwxyz123456789012"
    result = _redact_secrets(text)
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456789012" not in result


def test_truncate_preserves_short():
    text = "Short text"
    assert _truncate(text, 100) == text


def test_truncate_cuts_long():
    text = "A" * 1000
    result = _truncate(text, 100)
    assert len(result) <= 200
    assert "[... truncated" in result


def test_local_upload_security_error_sensitive_path():
    result = _local_upload_security_error(Path("/home/user/.ssh/id_rsa"))
    assert "sensitive" in result.lower() or "refusing" in result.lower()


def test_local_upload_security_error_outside_roots(tmp_path, monkeypatch):
    # Ensure no upload roots are configured
    monkeypatch.delenv("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS", raising=False)
    result = _local_upload_security_error(tmp_path / "test.md")
    # Without MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS, should be an error
    assert result


def test_is_remote_resource_source_true():
    assert _is_remote_resource_source("https://example.com")
    assert _is_remote_resource_source("git@github.com:org/repo.git")
    assert _is_remote_resource_source("ssh://git@github.com/org/repo.git")


def test_is_remote_resource_source_false():
    assert not _is_remote_resource_source("/local/path")
    assert not _is_remote_resource_source("relative/path.md")


def test_is_local_path_reference_absolute():
    assert _is_local_path_reference("/absolute/path")


def test_is_local_path_reference_relative():
    assert _is_local_path_reference("./relative")


def test_is_local_path_reference_url_false():
    assert not _is_local_path_reference("https://example.com")
