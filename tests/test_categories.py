"""
Test category management endpoints.
"""

import pytest


class TestListCategories:
    """Test GET /mcp/categories."""

    def test_list_all_categories(self, test_client):
        resp = test_client.get("/mcp/categories")
        data = resp.json()
        assert data["success"] is True
        categories = data["categories"]
        # Should have default categories
        ids = [c["id"] for c in categories]
        assert "general" in ids
        assert "work" in ids
        assert "learning" in ids
        assert "life" in ids
        assert "innovation" in ids

    def test_list_with_parent_filter(self, test_client):
        resp = test_client.get("/mcp/categories?parent_id=work")
        data = resp.json()
        assert data["success"] is True
        for cat in data["categories"]:
            assert cat["parent_id"] == "work"

    def test_list_returns_ordered(self, test_client):
        resp = test_client.get("/mcp/categories")
        categories = resp.json()["categories"]
        # 'general' has sort_order 0, should come first
        assert categories[0]["id"] == "general"

    def test_list_nonexistent_parent(self, test_client):
        resp = test_client.get("/mcp/categories?parent_id=nonexistent")
        data = resp.json()
        assert data["success"] is True
        assert data["categories"] == []


class TestGetCategory:
    """Test GET /mcp/categories/{category_id}."""

    def test_get_existing(self, test_client):
        resp = test_client.get("/mcp/categories/work")
        data = resp.json()
        assert data["success"] is True
        assert data["category"]["id"] == "work"
        assert data["category"]["name"] == "工作"

    def test_get_nonexistent(self, test_client):
        resp = test_client.get("/mcp/categories/does_not_exist")
        assert resp.status_code == 404


class TestCreateCategory:
    """Test POST /mcp/categories."""

    def test_create_valid(self, test_client):
        resp = test_client.post(
            "/mcp/categories",
            json={
                "id": "test_custom",
                "name": "Test Custom",
                "parent_id": None,
                "icon": "🧪",
                "sort_order": 99,
            },
        )
        data = resp.json()
        assert data["success"] is True
        assert data["category"]["id"] == "test_custom"

        # Verify it exists
        get_resp = test_client.get("/mcp/categories/test_custom")
        assert get_resp.json()["category"]["name"] == "Test Custom"

    def test_create_child_category(self, test_client):
        resp = test_client.post(
            "/mcp/categories",
            json={
                "id": "work_testing",
                "name": "测试子分类",
                "parent_id": "work",
            },
        )
        data = resp.json()
        assert data["success"] is True

        # Verify parent relationship
        get_resp = test_client.get("/mcp/categories/work_testing")
        assert get_resp.json()["category"]["parent_id"] == "work"

    def test_create_duplicate_rejected(self, test_client):
        # First create
        test_client.post(
            "/mcp/categories",
            json={"id": "dup_cat", "name": "Duplicate Test"},
        )
        # Second create with same ID should fail
        resp = test_client.post(
            "/mcp/categories",
            json={"id": "dup_cat", "name": "Duplicate Again"},
        )
        assert resp.status_code == 409

    def test_create_with_minimal_fields(self, test_client):
        resp = test_client.post(
            "/mcp/categories",
            json={"id": "minimal", "name": "Minimal Category"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_create_category_with_nonexistent_parent(self, test_client):
        """This should still succeed (no FK enforcement at app level for parent)."""
        resp = test_client.post(
            "/mcp/categories",
            json={
                "id": "orphan_cat",
                "name": "Orphan",
                "parent_id": "nonexistent_parent",
            },
        )
        assert resp.status_code == 200


class TestUpdateCategory:
    """Test PUT /mcp/categories/{category_id}."""

    @pytest.fixture
    def _create_test_cat(self, test_client):
        test_client.post(
            "/mcp/categories",
            json={"id": "updatable", "name": "Original Name"},
        )

    def test_update_name(self, test_client):
        test_client.post(
            "/mcp/categories",
            json={"id": "upd_name", "name": "Original"},
        )
        resp = test_client.put(
            "/mcp/categories/upd_name",
            json={"name": "Updated Name"},
        )
        assert resp.json()["success"] is True

        get_resp = test_client.get("/mcp/categories/upd_name")
        assert get_resp.json()["category"]["name"] == "Updated Name"

    def test_update_icon(self, test_client):
        test_client.post(
            "/mcp/categories",
            json={"id": "upd_icon", "name": "Icon Test"},
        )
        test_client.put(
            "/mcp/categories/upd_icon",
            json={"icon": "🔥"},
        )
        get_resp = test_client.get("/mcp/categories/upd_icon")
        assert get_resp.json()["category"]["icon"] == "🔥"

    def test_update_sort_order(self, test_client):
        test_client.post(
            "/mcp/categories",
            json={"id": "upd_sort", "name": "Sort Test"},
        )
        test_client.put(
            "/mcp/categories/upd_sort",
            json={"sort_order": 50},
        )
        get_resp = test_client.get("/mcp/categories/upd_sort")
        assert get_resp.json()["category"]["sort_order"] == 50

    def test_update_nonexistent(self, test_client):
        resp = test_client.put(
            "/mcp/categories/nonexistent",
            json={"name": "New Name"},
        )
        assert resp.status_code == 404

    def test_update_empty_body(self, test_client):
        test_client.post(
            "/mcp/categories",
            json={"id": "empty_upd", "name": "Empty Update"},
        )
        resp = test_client.put(
            "/mcp/categories/empty_upd",
            json={},
        )
        assert resp.status_code == 200


class TestDeleteCategory:
    """Test DELETE /mcp/categories/{category_id}."""

    def test_delete_custom_category(self, test_client):
        test_client.post(
            "/mcp/categories",
            json={"id": "temp_cat", "name": "Temporary"},
        )
        resp = test_client.delete("/mcp/categories/temp_cat")
        assert resp.json()["success"] is True
        assert resp.json()["action"] == "deleted"

        # Verify gone
        get_resp = test_client.get("/mcp/categories/temp_cat")
        assert get_resp.status_code == 404

    def test_delete_system_category_rejected(self, test_client):
        for sys_cat in ["general", "work", "learning", "life", "innovation"]:
            resp = test_client.delete(f"/mcp/categories/{sys_cat}")
            assert resp.status_code == 400
            assert "system" in resp.json()["detail"].lower()

    def test_delete_nonexistent(self, test_client):
        resp = test_client.delete("/mcp/categories/does_not_exist")
        assert resp.status_code == 404

    def test_delete_reassigns_memories(self, test_client):
        """Deleting a category should reassign memories to 'general'."""
        test_client.post(
            "/mcp/categories",
            json={"id": "to_delete", "name": "Will Delete"},
        )
        test_client.post(
            "/mcp/save",
            json={
                "content": "Memory in category to delete",
                "category_id": "to_delete",
            },
        )
        test_client.delete("/mcp/categories/to_delete")

        # The memory should now be in 'general'
        search_resp = test_client.post(
            "/mcp/search",
            json={"q": "category to delete", "limit": 10},
        )
        for r in search_resp.json().get("results", []):
            assert r["category_id"] == "general"
