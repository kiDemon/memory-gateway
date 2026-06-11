"""
Test memory CRUD operations via the FastAPI TestClient.
"""

import json

import pytest


class TestSaveMemory:
    """Test /mcp/save endpoint."""

    def test_save_basic(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Hello from test", "source": "system"},
        )
        data = resp.json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["action"] == "saved"
        assert "id" in data

    def test_save_with_all_fields(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={
                "content": "Important project decision: use FastAPI",
                "type": "decision",
                "scope": "project",
                "source": "hermes",
                "priority": "P0",
                "tags": ["project", "architecture"],
                "category_id": "work",
                "session_id": "session-123",
            },
        )
        data = resp.json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["action"] == "saved"
        assert data["type"] == "decision"

    def test_save_duplicate_content(self, test_client):
        content = "This is a unique test memory"
        resp1 = test_client.post(
            "/mcp/save", json={"content": content}
        )
        assert resp1.json()["action"] == "saved"

        resp2 = test_client.post(
            "/mcp/save", json={"content": content}
        )
        data2 = resp2.json()
        assert data2["action"] == "skipped"
        assert data2["reason"] == "duplicate"

    def test_save_empty_content_rejected(self, test_client):
        resp = test_client.post(
            "/mcp/save", json={"content": ""}
        )
        assert resp.status_code == 422  # Pydantic validation

    def test_save_with_insights(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={
                "content": "Learned that pytest fixtures are powerful",
                "type": "learning",
                "insights": "Fixtures allow clean test isolation",
            },
        )
        data = resp.json()
        assert data["success"] is True

    def test_save_procedural_auto_hot(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={
                "content": "工作流程：每天早晨检查系统状态，确认服务正常运行",
                "source": "system",
            },
        )
        data = resp.json()
        assert data["success"] is True
        # Procedural content should auto-detect and save
        assert "id" in data

    def test_save_privacy_filter(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "API key is sk-abcdefghijklmnopqrstuvwxyz123456"},
        )
        data = resp.json()
        assert data["success"] is True
        # Verify it was saved with redacted content
        get_resp = test_client.get(f"/mcp/get/{data['id']}")
        get_data = get_resp.json()
        saved_content = get_data.get("memory", {}).get("content", "")
        assert "[API-KEY-REDACTED]" in saved_content
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in saved_content


class TestGetMemory:
    """Test /mcp/get/{memory_id} endpoint."""

    def test_get_existing(self, test_client):
        save_resp = test_client.post(
            "/mcp/save", json={"content": "Memory to retrieve"}
        )
        mem_id = save_resp.json()["id"]

        resp = test_client.get(f"/mcp/get/{mem_id}")
        data = resp.json()
        assert resp.status_code == 200
        assert data["success"] is True
        mem = data["memory"]
        assert mem["id"] == mem_id
        assert mem["content"] == "Memory to retrieve"

    def test_get_nonexistent(self, test_client):
        resp = test_client.get("/mcp/get/nonexistent-id")
        assert resp.status_code == 404

    def test_get_includes_fields(self, test_client):
        save_resp = test_client.post(
            "/mcp/save",
            json={
                "content": "Memory with all metadata",
                "type": "decision",
                "source": "claude",
                "priority": "P1",
                "tags": ["test"],
                "category_id": "work",
            },
        )
        mem_id = save_resp.json()["id"]

        resp = test_client.get(f"/mcp/get/{mem_id}")
        mem = resp.json()["memory"]
        assert mem["type"] == "decision"
        assert mem["source"] == "claude"
        assert mem["priority"] == "P1"
        assert mem["category_id"] == "work"


class TestSearchMemory:
    """Test /mcp/search endpoint."""

    def test_search_by_content(self, test_client):
        test_client.post(
            "/mcp/save",
            json={"content": "The capital of France is Paris"},
        )
        resp = test_client.post(
            "/mcp/search", json={"q": "Paris", "limit": 10}
        )
        data = resp.json()
        assert data["success"] is True
        assert data["count"] >= 1
        assert any("Paris" in r["content"] for r in data["results"])

    def test_search_empty_query(self, test_client):
        resp = test_client.post(
            "/mcp/search", json={"q": "xxyyzz_nonexistent", "limit": 10}
        )
        data = resp.json()
        assert data["success"] is True
        assert data["count"] == 0

    def test_search_with_category_filter(self, test_client):
        test_client.post(
            "/mcp/save",
            json={
                "content": "Work-related memory",
                "category_id": "work",
            },
        )
        test_client.post(
            "/mcp/save",
            json={
                "content": "Learning-related memory",
                "category_id": "learning",
            },
        )
        resp = test_client.post(
            "/mcp/search",
            json={"q": "memory", "category_filter": "learning", "limit": 10},
        )
        data = resp.json()
        assert all(r["category_id"] == "learning" for r in data["results"])

    def test_search_with_source_filter(self, test_client):
        test_client.post(
            "/mcp/save",
            json={"content": "From hermes", "source": "hermes"},
        )
        resp = test_client.post(
            "/mcp/search",
            json={"q": "From", "source_filter": "hermes", "limit": 10},
        )
        data = resp.json()
        assert all(r["source"] == "hermes" for r in data["results"])

    def test_search_short_query(self, test_client):
        """1-2 char queries should still work."""
        test_client.post(
            "/mcp/save", json={"content": "Go to the store"}
        )
        resp = test_client.post(
            "/mcp/search", json={"q": "Go", "limit": 10}
        )
        data = resp.json()
        assert data["success"] is True


class TestUpdateMemory:
    """Test /mcp/update/{memory_id} endpoint."""

    def test_update_content(self, test_client):
        save_resp = test_client.post(
            "/mcp/save", json={"content": "Original content"}
        )
        mem_id = save_resp.json()["id"]

        resp = test_client.put(
            f"/mcp/update/{mem_id}",
            json={"content": "Updated content"},
        )
        data = resp.json()
        assert data["success"] is True
        assert data["action"] == "updated"

        # Verify content changed
        get_resp = test_client.get(f"/mcp/get/{mem_id}")
        assert get_resp.json()["memory"]["content"] == "Updated content"

    def test_update_type(self, test_client):
        save_resp = test_client.post(
            "/mcp/save",
            json={"content": "Some decision content", "type": "general"},
        )
        mem_id = save_resp.json()["id"]

        resp = test_client.put(
            f"/mcp/update/{mem_id}",
            json={"type": "decision"},
        )
        assert resp.json()["success"] is True

        get_resp = test_client.get(f"/mcp/get/{mem_id}")
        assert get_resp.json()["memory"]["type"] == "decision"

    def test_update_priority_and_hot_tier(self, test_client):
        save_resp = test_client.post(
            "/mcp/save", json={"content": "Priority test", "priority": "P2"}
        )
        mem_id = save_resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"priority": "P0"},
        )
        get_resp = test_client.get(f"/mcp/get/{mem_id}")
        mem = get_resp.json()["memory"]
        assert mem["priority"] == "P0"
        # P0 should be hot tier
        assert mem["hot_tier"] == 1

    def test_update_category(self, test_client):
        save_resp = test_client.post(
            "/mcp/save", json={"content": "Category test"}
        )
        mem_id = save_resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"category_id": "innovation"},
        )
        get_resp = test_client.get(f"/mcp/get/{mem_id}")
        assert get_resp.json()["memory"]["category_id"] == "innovation"

    def test_update_archive(self, test_client):
        save_resp = test_client.post(
            "/mcp/save", json={"content": "Will be archived"}
        )
        mem_id = save_resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"archived": True},
        )
        get_resp = test_client.get(f"/mcp/get/{mem_id}")
        assert get_resp.json()["memory"]["archived"] == 1

    def test_update_nonexistent(self, test_client):
        resp = test_client.put(
            "/mcp/update/nonexistent-id",
            json={"content": "New content"},
        )
        assert resp.status_code == 404

    def test_update_no_changes(self, test_client):
        save_resp = test_client.post(
            "/mcp/save", json={"content": "No changes here"}
        )
        mem_id = save_resp.json()["id"]

        resp = test_client.put(
            f"/mcp/update/{mem_id}",
            json={},  # empty update
        )
        data = resp.json()
        assert data["success"] is True
        assert data["action"] == "no_changes"


class TestDeleteMemory:
    """Test /mcp/delete/{memory_id} endpoint."""

    def test_delete_existing(self, test_client):
        save_resp = test_client.post(
            "/mcp/save", json={"content": "To be deleted"}
        )
        mem_id = save_resp.json()["id"]

        resp = test_client.delete(f"/mcp/delete/{mem_id}")
        data = resp.json()
        assert data["success"] is True
        assert data["action"] == "deleted"

        # Verify it's gone
        get_resp = test_client.get(f"/mcp/get/{mem_id}")
        assert get_resp.status_code == 404

    def test_delete_nonexistent(self, test_client):
        resp = test_client.delete("/mcp/delete/nonexistent-id")
        assert resp.status_code == 404

    def test_delete_cleans_related_tables(self, test_client):
        """Deleting should also clean session_memories and change_log."""
        save_resp = test_client.post(
            "/mcp/save",
            json={
                "content": "With session",
                "session_id": "test-session",
            },
        )
        mem_id = save_resp.json()["id"]

        test_client.delete(f"/mcp/delete/{mem_id}")

        # Verify related data is cleaned by checking the session doesn't reference it
        list_resp = test_client.post(
            "/mcp/list", json={"limit": 100}
        )
        ids = [m["id"] for m in list_resp.json().get("memories", [])]
        assert mem_id not in ids


class TestListMemory:
    """Test /mcp/list endpoint."""

    def test_list_all(self, test_client):
        test_client.post("/mcp/save", json={"content": "Memory Alpha for testing list functionality"})
        test_client.post("/mcp/save", json={"content": "Memory Beta completely different content here"})

        resp = test_client.post("/mcp/list", json={"limit": 50})
        data = resp.json()
        assert data["success"] is True
        assert data["count"] >= 2

    def test_list_with_category_filter(self, test_client):
        test_client.post(
            "/mcp/save",
            json={"content": "Work item for category filter test with unique content", "category_id": "work"},
        )
        test_client.post(
            "/mcp/save",
            json={"content": "Learning item for category filter test with different content", "category_id": "learning"},
        )
        resp = test_client.post(
            "/mcp/list",
            json={"category_filter": "work", "limit": 50},
        )
        data = resp.json()
        for m in data["results"]:
            assert m["category_id"] == "work"
