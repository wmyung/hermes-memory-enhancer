import json
from unittest.mock import MagicMock

from plugins.memory import hermes_memory_enhancer as hme
from plugins.memory.hermes_memory_enhancer import HermesMemoryEnhancerProvider


def test_add_resource_disabled_by_default():
    provider = HermesMemoryEnhancerProvider()
    provider._client = MagicMock()

    result = json.loads(provider._tool_add_resource({"url": "https://example.com/doc.md"}))

    assert "disabled by default" in result["error"]
    provider._client.post.assert_not_called()


def test_local_upload_requires_allowed_root(tmp_path):
    sample = tmp_path / "sample.md"
    sample.write_text("ok\n", encoding="utf-8")
    provider = HermesMemoryEnhancerProvider()
    provider._enable_add_resource = True
    provider._client = MagicMock()

    result = json.loads(provider._tool_add_resource({"url": str(sample)}))

    assert "MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS" in result["error"]
    provider._client.upload_temp_file.assert_not_called()


def test_local_upload_refuses_sensitive_filename(tmp_path, monkeypatch):
    secret = tmp_path / ".env"
    secret.write_text("API_KEY=abc\n", encoding="utf-8")
    monkeypatch.setenv("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS", str(tmp_path))
    provider = HermesMemoryEnhancerProvider()
    provider._enable_add_resource = True
    provider._client = MagicMock()

    result = json.loads(provider._tool_add_resource({"url": str(secret)}))

    assert "sensitive local path" in result["error"]
    provider._client.upload_temp_file.assert_not_called()


def test_initialize_refuses_remote_http_without_explicit_override(monkeypatch):
    monkeypatch.setenv("MEMORY_ENHANCER_ENDPOINT", "http://memory.example.com")
    monkeypatch.setenv("MEMORY_ENHANCER_API_KEY", "test-key")
    provider = HermesMemoryEnhancerProvider()

    provider.initialize("session-1")

    assert provider._client is None


def test_initialize_refuses_remote_endpoint_without_api_key(monkeypatch):
    monkeypatch.setenv("MEMORY_ENHANCER_ENDPOINT", "https://memory.example.com")
    monkeypatch.delenv("MEMORY_ENHANCER_API_KEY", raising=False)
    provider = HermesMemoryEnhancerProvider()

    provider.initialize("session-1")

    assert provider._client is None


def test_prefetch_redacts_and_caps_context():
    provider = HermesMemoryEnhancerProvider()
    provider._client = MagicMock()
    provider._prefetch_top_k = 1
    provider._max_abstract_chars = 120
    provider._sync_max_chars = 80
    provider._client.post.return_value = {
        "result": {
            "memories": [
                {
                    "uri": "memory://memories/secret",
                    "score": 1.0,
                    "abstract": "api_key = sk-abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 300,
                }
            ]
        }
    }

    result = provider.prefetch("token=ghp_abcdefghijklmnopqrstuvwxyz123456 " + "q" * 120)

    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result
    assert "api_key [REDACTED]" in result
    assert len(result) < 300
    payload = provider._client.post.call_args.args[1]
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in payload["query"]
    assert "token [REDACTED]" in payload["query"]
    assert len(payload["query"]) <= 80


def test_queue_prefetch_redacts_and_caps_query(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, endpoint, payload):
            calls.append((endpoint, payload))
            return {"result": {}}

    monkeypatch.setattr(hme, "_MemoryEnhancerClient", FakeClient)
    provider = HermesMemoryEnhancerProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://127.0.0.1:1933"
    provider._api_key = ""
    provider._account = "default"
    provider._user = "default"
    provider._agent = "hermes"
    provider._sync_max_chars = 80

    provider.queue_prefetch("secret: sk-abcdefghijklmnopqrstuvwxyz0123456789 " + "q" * 120)
    assert provider._prefetch_thread is not None
    provider._prefetch_thread.join(timeout=5)

    assert len(calls) == 1
    payload = calls[0][1]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in payload["query"]
    assert "secret [REDACTED]" in payload["query"]
    assert len(payload["query"]) <= 80


def test_sync_turn_redacts_and_caps_messages(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, endpoint, payload):
            calls.append((endpoint, payload))
            return {"status": "ok"}

    monkeypatch.setattr(hme, "_MemoryEnhancerClient", FakeClient)
    provider = HermesMemoryEnhancerProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://127.0.0.1:1933"
    provider._api_key = ""
    provider._account = "default"
    provider._user = "default"
    provider._agent = "hermes"
    provider._session_id = "session-1"
    provider._sync_max_chars = 80

    provider.sync_turn(
        "api_key = sk-abcdefghijklmnopqrstuvwxyz0123456789 " + "u" * 200,
        "authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789 " + "a" * 200,
    )
    assert provider._sync_thread is not None
    provider._sync_thread.join(timeout=5)

    assert len(calls) == 2
    sent = json.dumps([payload for _, payload in calls])
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in sent
    assert "Bearer abcdefghijklmnopqrstuvwxyz" not in sent
    assert "[REDACTED]" in sent
    assert all(len(payload["content"]) <= 125 for _, payload in calls)


def test_on_memory_write_redacts_and_caps_content(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, endpoint, payload):
            calls.append((endpoint, payload))
            return {"status": "ok"}

    monkeypatch.setattr(hme, "_MemoryEnhancerClient", FakeClient)
    provider = HermesMemoryEnhancerProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://127.0.0.1:1933"
    provider._api_key = ""
    provider._account = "default"
    provider._user = "default"
    provider._agent = "hermes"
    provider._session_id = "session-1"
    provider._sync_max_chars = 80

    provider.on_memory_write(
        "add",
        "memory",
        "password: supersecretvalue " + "x" * 200,
    )
    # on_memory_write starts a daemon thread without storing it; wait briefly by polling calls.
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)

    assert len(calls) == 1
    text = calls[0][1]["parts"][0]["text"]
    assert "supersecretvalue" not in text
    assert "password [REDACTED]" in text
    assert len(text) <= 130


def test_search_read_and_browse_outputs_redact_secret_material():
    provider = HermesMemoryEnhancerProvider()
    provider._client = MagicMock()
    provider._max_abstract_chars = 120
    provider._client.post.return_value = {
        "result": {
            "memories": [
                {"uri": "memory://m/1", "score": 1.0, "abstract": "token=ghp_abcdefghijklmnopqrstuvwxyz123456"}
            ]
        }
    }

    search = json.loads(provider._tool_search({"query": "token=ghp_abcdefghijklmnopqrstuvwxyz123456"}))
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in json.dumps(search)
    assert "token [REDACTED]" in json.dumps(search)
    provider._client.post.assert_called_with("/api/v1/search/find", {"query": "token [REDACTED]"})

    provider._client.get.return_value = {"result": {"content": "secret: sk-abcdefghijklmnopqrstuvwxyz0123456789"}}
    read = json.loads(provider._tool_read({"uri": "memory://m/1", "level": "full"}))
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(read)
    assert "secret [REDACTED]" in read["content"]

    provider._client.get.return_value = {"result": [{"uri": "memory://m/1", "name": "m1", "abstract": "api_key=sk-abcdefghijklmnopqrstuvwxyz0123456789"}]}
    browse = json.loads(provider._tool_browse({"path": "memory://", "action": "list"}))
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(browse)
    assert "api_key [REDACTED]" in browse["entries"][0]["abstract"]
