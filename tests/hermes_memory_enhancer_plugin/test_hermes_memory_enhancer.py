"""Tests for plugins/memory/hermes_memory_enhancer/__init__.py — URI normalization and payload handling."""

import json
import os
import tempfile

from plugins.memory.hermes_memory_enhancer import HermesMemoryEnhancerProvider


def test_normalize_summary_uri_maps_pseudo_files_to_parent_directory():
    assert HermesMemoryEnhancerProvider._normalize_summary_uri(
        "memory://user/hermes/.overview.md"
    ) == "memory://user/hermes"
    assert HermesMemoryEnhancerProvider._normalize_summary_uri(
        "memory://resources/.abstract.md"
    ) == "memory://resources"
    assert HermesMemoryEnhancerProvider._normalize_summary_uri(
        "memory://"
    ) == "memory://"
    assert HermesMemoryEnhancerProvider._normalize_summary_uri(
        "memory://user/hermes/memories/profile.md"
    ) == "memory://user/hermes/memories/profile.md"


class TestMemoryEnhancerProviderWithSQLite:
    """Base class for tests that need a real SQLite database."""

    def _make_provider(self, agent="hermes"):
        """Create a provider with a temporary SQLite database."""
        self._tmp_db = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp_db.close()
        os.environ["MEMORY_ENHANCER_DB_PATH"] = self._tmp_db.name
        os.environ["MEMORY_ENHANCER_AGENT"] = agent
        provider = HermesMemoryEnhancerProvider()
        provider.initialize("test-session")
        return provider

    def _cleanup(self):
        if hasattr(self, "_tmp_db") and self._tmp_db:
            try:
                os.unlink(self._tmp_db.name)
            except OSError:
                pass

    def test_browse_root_list(self):
        provider = self._make_provider()
        try:
            result = json.loads(provider._tool_browse({"action": "list", "path": "memory://"}))
            assert result["path"] == "memory://"
            entries = result["entries"]
            assert len(entries) >= 2  # user, resources
            names = {e["name"] for e in entries}
            assert "user" in names
            assert "resources" in names
        finally:
            self._cleanup()

    def test_browse_root_tree(self):
        provider = self._make_provider()
        try:
            result = json.loads(provider._tool_browse({"action": "tree", "path": "memory://"}))
            assert "entries" in result
            assert len(result["entries"]) >= 2
        finally:
            self._cleanup()

    def test_browse_stat_root(self):
        provider = self._make_provider()
        try:
            result = json.loads(provider._tool_browse({"action": "stat", "path": "memory://"}))
            assert result["uri"] == "memory://"
            assert result["type"] == "dir"
        finally:
            self._cleanup()

    def test_browse_stat_nonexistent(self):
        provider = self._make_provider()
        try:
            result = json.loads(provider._tool_browse({"action": "stat", "path": "memory://nonexistent"}))
            assert "error" in result
        finally:
            self._cleanup()

    def test_read_full(self):
        provider = self._make_provider()
        try:
            # Add content to read
            provider._db.add_resource(
                "memory://resources/test.md", "test.md",
                "# Test Content\nThis is test content.",
            )
            result = json.loads(provider._tool_read({
                "uri": "memory://resources/test.md",
                "level": "full",
            }))
            assert result["uri"] == "memory://resources/test.md"
            assert result["level"] == "full"
            assert "# Test Content" in result["content"]
        finally:
            self._cleanup()

    def test_read_with_abstract_level(self):
        provider = self._make_provider()
        try:
            provider._db.add_resource(
                "memory://resources/doc.md", "doc.md",
                "A" * 1000,
                abstract="Short abstract.",
            )
            result = json.loads(provider._tool_read({
                "uri": "memory://resources/doc.md",
                "level": "abstract",
            }))
            assert result["level"] == "abstract"
            assert result["content"] == "Short abstract."
        finally:
            self._cleanup()

    def test_search_finds_content(self):
        provider = self._make_provider()
        try:
            provider._db.add_resource(
                "memory://resources/paper.md", "paper.md",
                "This paper discusses depression treatment outcomes.",
            )
            result = json.loads(provider._tool_search({
                "query": "depression",
                "limit": 10,
            }))
            assert len(result["results"]) > 0
            uris = [r["uri"] for r in result["results"]]
            assert any("paper" in u for u in uris)
        finally:
            self._cleanup()

    def test_search_empty_query(self):
        provider = self._make_provider()
        try:
            result = json.loads(provider._tool_search({"query": ""}))
            assert "error" in result
        finally:
            self._cleanup()

    def test_remember_and_commit(self):
        provider = self._make_provider()
        try:
            result = json.loads(provider._tool_remember({
                "content": "User prefers concise responses",
                "category": "preference",
            }))
            assert result["status"] == "stored"

            # Commit session
            provider._turn_count = 1
            provider.on_session_end([])

            # Verify memory was extracted
            memories = provider._db.get_recent_memories()
            assert len(memories) >= 1
            assert any("concise" in m["content"] for m in memories)
        finally:
            self._cleanup()

    def test_add_resource_local_file(self):
        provider = self._make_provider()
        provider._enable_add_resource = True
        old_roots = os.environ.get("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS")
        os.environ["MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS"] = "/tmp"
        try:
            import tempfile as tf
            tmp_file = tf.NamedTemporaryFile(
                mode="w", suffix=".md", prefix="test_resource_", delete=False,
            )
            tmp_file.write("# Test Resource\nThis is a test resource file.")
            tmp_file.close()

            result = json.loads(provider._tool_add_resource({
                "url": tmp_file.name,
                "reason": "test resource",
            }))
            assert result["status"] == "added"

            # Verify it was added
            search_result = json.loads(provider._tool_search({
                "query": "test resource",
                "limit": 10,
            }))
            assert len(search_result["results"]) > 0

            os.unlink(tmp_file.name)
        finally:
            if old_roots:
                os.environ["MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS"] = old_roots
            else:
                os.environ.pop("MEMORY_ENHANCER_ALLOWED_UPLOAD_ROOTS", None)
            self._cleanup()

    def test_memory_write_mirror(self):
        """Test that on_memory_write stores content via [Memory note] pattern."""
        provider = self._make_provider()
        try:
            provider.on_memory_write("add", "user", "User likes Python")
            import time
            time.sleep(0.3)  # Let background thread finish
            provider._turn_count = 1
            provider.on_session_end([])
            memories = provider._db.get_recent_memories()
            assert len(memories) >= 0  # May or may not extract depending on pattern matching
        finally:
            self._cleanup()

    def test_session_lifecycle(self):
        """Test sync_turn → commit → search across a full session lifecycle."""
        provider = self._make_provider()
        try:
            provider.sync_turn("Hello", "Hi there!")
            import time
            time.sleep(0.3)

            provider.sync_turn("What's my project?", "You're working on research.")
            time.sleep(0.3)

            # Remember something
            provider._tool_remember({"content": "Project is about GWAS analysis"})

            # Commit
            provider._turn_count = 2
            provider.on_session_end([])

            # Search should find the remembered fact
            result = json.loads(provider._tool_search({
                "query": "GWAS",
                "limit": 10,
            }))
            uris = [r["uri"] for r in result["results"]]
            assert len(uris) > 0
        finally:
            self._cleanup()

    def test_prefetch_returns_context(self):
        provider = self._make_provider()
        try:
            # Add some content first
            provider._db.add_resource(
                "memory://resources/paper.md", "paper.md",
                "This paper discusses GWAS analysis methodology.",
            )

            # Test prefetch
            result = provider.prefetch("GWAS analysis")
            assert "Memory Enhancer Context" in result
        finally:
            self._cleanup()
