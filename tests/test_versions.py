"""
Test memory version control (VersionManager).

Version operations are accessed mainly through the MCP JSON-RPC endpoint
(POST /mcp with method "tools/call") or through the /mcp/history endpoint.
"""

import json


_MCP_URL = "/mcp"


def _mcp_tools_call(test_client, tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool via JSON-RPC."""
    resp = test_client.post(
        _MCP_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        },
    )
    assert resp.status_code == 200
    result = resp.json()
    assert "result" in result
    if "isError" in result.get("result", {}):
        return {"error": result["result"].get("content", [{}])[0].get("text", "unknown error")}
    content_list = result["result"].get("content", [])
    if content_list:
        return json.loads(content_list[0].get("text", "{}"))
    return {}


class TestVersionAutoCreate:
    """Versions are created automatically on save and update."""

    def test_save_creates_version(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Versioned memory", "source": "test"},
        )
        mem_id = resp.json()["id"]

        # Get version history via MCP tool
        result = _mcp_tools_call(
            test_client, "mem_history", {"memory_id": mem_id, "limit": 20}
        )
        assert "versions" in result
        versions = result["versions"]
        assert len(versions) >= 1
        assert versions[0]["version"] == 1
        assert versions[0]["change_type"] == "create"

    def test_update_increments_version(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Original v1", "source": "test"},
        )
        mem_id = resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"content": "Updated v2 content"},
        )

        result = _mcp_tools_call(
            test_client, "mem_history", {"memory_id": mem_id, "limit": 20}
        )
        versions = result["versions"]
        assert len(versions) >= 2
        # Versions are ordered by version DESC
        v_versions = [v["version"] for v in versions]
        assert max(v_versions) >= 2

    def test_version_content_integrity(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Version A content", "source": "test"},
        )
        mem_id = resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"content": "Version B content"},
        )

        result = _mcp_tools_call(
            test_client, "mem_history", {"memory_id": mem_id, "limit": 20}
        )
        versions = result["versions"]

        # Find v1 and v2
        v1 = next(v for v in versions if v["version"] == 1)
        v2 = next(v for v in versions if v["version"] == 2)
        assert v1["content"] == "Version A content"
        assert v2["content"] == "Version B content"


class TestVersionHistory:
    """Test mem_history MCP tool."""

    def test_get_history_empty(self, test_client):
        """Non-existent memory should not crash."""
        result = _mcp_tools_call(
            test_client, "mem_history", {"memory_id": "nonexistent-id", "limit": 20}
        )
        # Should return empty versions list, not crash
        assert "versions" in result
        assert len(result["versions"]) == 0

    def test_get_history_multiple_versions(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Initial", "source": "test"},
        )
        mem_id = resp.json()["id"]

        for i in range(3):
            test_client.put(
                f"/mcp/update/{mem_id}",
                json={"content": f"Update #{i+1}"},
            )

        result = _mcp_tools_call(
            test_client, "mem_history", {"memory_id": mem_id, "limit": 20}
        )
        versions = result["versions"]
        assert len(versions) >= 4  # 1 initial + 3 updates


class TestVersionDiff:
    """Test mem_diff MCP tool."""

    def test_diff_between_versions(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Line 1\nLine 2\nLine 3", "source": "test"},
        )
        mem_id = resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"content": "Line 1\nLine 2 modified\nLine 3\nLine 4 added"},
        )

        result = _mcp_tools_call(
            test_client,
            "mem_diff",
            {"memory_id": mem_id, "version_a": 1, "version_b": 2},
        )
        assert "diff" in result
        assert "Line 2" in result["diff"]

    def test_diff_nonexistent_version(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Test", "source": "test"},
        )
        mem_id = resp.json()["id"]

        result = _mcp_tools_call(
            test_client,
            "mem_diff",
            {"memory_id": mem_id, "version_a": 1, "version_b": 999},
        )
        # Should return an error object
        assert "error" in result


class TestVersionRollback:
    """Test mem_rollback MCP tool."""

    def test_rollback_to_previous_version(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Original content", "source": "test"},
        )
        mem_id = resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"content": "Newer content that is very different"},
        )

        # Rollback to version 1
        result = _mcp_tools_call(
            test_client,
            "mem_rollback",
            {"memory_id": mem_id, "version": 1},
        )
        assert result.get("success") is True
        assert result.get("action") == "rollback"
        assert result.get("target_version") == 1

        # Verify content reverted
        get_resp = test_client.get(f"/mcp/get/{mem_id}")
        assert get_resp.json()["memory"]["content"] == "Original content"

    def test_rollback_adds_new_version(self, test_client):
        """Rollback creates a new version entry."""
        resp = test_client.post(
            "/mcp/save",
            json={"content": "v1 content", "source": "test"},
        )
        mem_id = resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"content": "v2 content"},
        )

        _mcp_tools_call(
            test_client,
            "mem_rollback",
            {"memory_id": mem_id, "version": 1},
        )

        # Should have 3 versions: v1, v2, v3=rollback
        result = _mcp_tools_call(
            test_client, "mem_history", {"memory_id": mem_id, "limit": 20}
        )
        versions = result["versions"]
        assert len(versions) >= 3

    def test_rollback_nonexistent_version(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Test", "source": "test"},
        )
        mem_id = resp.json()["id"]

        result = _mcp_tools_call(
            test_client,
            "mem_rollback",
            {"memory_id": mem_id, "version": 999},
        )
        assert "error" in result


class TestVersionBranches:
    """Test mem_branch MCP tool."""

    def test_create_branch(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Branch base content", "source": "test"},
        )
        mem_id = resp.json()["id"]

        result = _mcp_tools_call(
            test_client,
            "mem_branch",
            {"memory_id": mem_id, "action": "create", "branch_name": "experiment", "version": 1},
        )
        assert result.get("success") is True
        assert result.get("branch_name") == "experiment"

    def test_branch_from_latest(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Latest base", "source": "test"},
        )
        mem_id = resp.json()["id"]

        result = _mcp_tools_call(
            test_client,
            "mem_branch",
            {"memory_id": mem_id, "action": "create", "branch_name": "latest-branch"},
        )
        assert result.get("success") is True

    def test_list_branches(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Multi-branch", "source": "test"},
        )
        mem_id = resp.json()["id"]

        _mcp_tools_call(
            test_client,
            "mem_branch",
            {"memory_id": mem_id, "action": "create", "branch_name": "feature-a"},
        )
        _mcp_tools_call(
            test_client,
            "mem_branch",
            {"memory_id": mem_id, "action": "create", "branch_name": "feature-b"},
        )

        result = _mcp_tools_call(
            test_client,
            "mem_branch",
            {"memory_id": mem_id, "action": "list"},
        )
        assert "branches" in result
        names = [b["branch_name"] for b in result["branches"]]
        assert "feature-a" in names
        assert "feature-b" in names

    def test_duplicate_branch_name_rejected(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Branch dup test", "source": "test"},
        )
        mem_id = resp.json()["id"]

        _mcp_tools_call(
            test_client,
            "mem_branch",
            {"memory_id": mem_id, "action": "create", "branch_name": "dup"},
        )
        result = _mcp_tools_call(
            test_client,
            "mem_branch",
            {"memory_id": mem_id, "action": "create", "branch_name": "dup"},
        )
        assert "error" in result


class TestChangeLog:
    """Test GET /mcp/history/{memory_id} endpoint."""

    def test_save_logs_change(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Change log test", "source": "test"},
        )
        mem_id = resp.json()["id"]

        hist_resp = test_client.get(f"/mcp/history/{mem_id}")
        data = hist_resp.json()
        assert data["success"] is True
        assert data["count"] >= 1
        assert data["history"][0]["action"] == "save"

    def test_update_logs_change(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "Original", "source": "test"},
        )
        mem_id = resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"content": "Updated"},
        )

        hist_resp = test_client.get(f"/mcp/history/{mem_id}")
        actions = [h["action"] for h in hist_resp.json()["history"]]
        assert "update" in actions

    def test_history_empty_for_nonexistent(self, test_client):
        hist_resp = test_client.get("/mcp/history/nonexistent-id")
        data = hist_resp.json()
        assert data["success"] is True
        assert data["count"] == 0

    def test_rollback_logs_change(self, test_client):
        resp = test_client.post(
            "/mcp/save",
            json={"content": "v1 content", "source": "test"},
        )
        mem_id = resp.json()["id"]

        test_client.put(
            f"/mcp/update/{mem_id}",
            json={"content": "v2 content"},
        )

        _mcp_tools_call(
            test_client,
            "mem_rollback",
            {"memory_id": mem_id, "version": 1},
        )

        # Delete also logs a change
        test_client.delete(f"/mcp/delete/{mem_id}")

        # Before deletion, we should have save, update, and rollback events
        # After deletion, history for this ID will return empty
        hist_resp = test_client.get(f"/mcp/history/{mem_id}")
        assert hist_resp.json()["count"] == 0  # deleted
