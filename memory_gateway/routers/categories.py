"""
Categories router - category management API endpoints.

Extracted from server.py (/mcp/categories endpoints).
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from memory_gateway.database.connection import db_conn
from memory_gateway.models.requests import CategoryRequest, CategoryUpdateRequest

router = APIRouter(tags=["categories"])


@router.get("/mcp/categories")
async def list_categories(parent_id: Optional[str] = None) -> dict:
    """Get category tree. If parent_id is provided, return children of that category."""
    with db_conn() as db:
        if parent_id is not None:
            rows = db.execute(
                "SELECT * FROM categories WHERE parent_id=? ORDER BY sort_order, name",
                (parent_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM categories ORDER BY sort_order, name"
            ).fetchall()
    return {"success": True, "categories": [dict(r) for r in rows]}


@router.get("/mcp/categories/{category_id}")
async def get_category(category_id: str) -> dict:
    """Get a single category by ID."""
    with db_conn() as db:
        row = db.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Category {category_id} not found")
    return {"success": True, "category": dict(row)}


@router.post("/mcp/categories")
async def create_category(req: CategoryRequest) -> dict:
    """Create a new custom category."""
    with db_conn() as db:
        existing = db.execute("SELECT id FROM categories WHERE id=?", (req.id,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Category {req.id} already exists")
        db.execute(
            "INSERT INTO categories (id, name, parent_id, icon, sort_order) VALUES (?, ?, ?, ?, ?)",
            (req.id, req.name, req.parent_id, req.icon or "📁", req.sort_order or 0),
        )
    return {"success": True, "category": {"id": req.id, "name": req.name}}


@router.put("/mcp/categories/{category_id}")
async def update_category(category_id: str, req: CategoryUpdateRequest) -> dict:
    """Update a category."""
    with db_conn() as db:
        existing = db.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Category {category_id} not found")
        updates = []
        params: list = []
        if req.name is not None:
            updates.append("name=?")
            params.append(req.name)
        if req.parent_id is not None:
            updates.append("parent_id=?")
            params.append(req.parent_id)
        if req.icon is not None:
            updates.append("icon=?")
            params.append(req.icon)
        if req.sort_order is not None:
            updates.append("sort_order=?")
            params.append(req.sort_order)
        if updates:
            sql = f"UPDATE categories SET {', '.join(updates)} WHERE id=?"
            params.append(category_id)
            db.execute(sql, params)
    return {"success": True, "category_id": category_id}


@router.delete("/mcp/categories/{category_id}")
async def delete_category(category_id: str) -> dict:
    """Delete a category. Memories using this category will revert to 'general'."""
    with db_conn() as db:
        existing = db.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Category {category_id} not found")
        if category_id in ("general", "learning", "life", "work", "innovation"):
            raise HTTPException(status_code=400, detail="Cannot delete system categories")
        db.execute("UPDATE memories SET category_id='general' WHERE category_id=?", (category_id,))
        db.execute("DELETE FROM categories WHERE id=?", (category_id,))
    return {"success": True, "action": "deleted", "category_id": category_id}
