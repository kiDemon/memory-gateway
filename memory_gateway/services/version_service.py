"""Version Manager — Git for Memory core service.

Extracted from server.py to support multi-module architecture.
All logic remains unchanged; uses dependency injection for database access.
"""

import difflib
import json
import logging
import sqlite3
import uuid
from typing import Optional

from memory_gateway.config import log
from memory_gateway.utils import now_iso
from memory_gateway.utils.crypto import compute_checksum, compute_simhash, hamming_distance

log = logging.getLogger("memory-server")

# 版本保留策略：每个记忆最多保留的版本数
VERSION_KEEP_COUNT = 10


class VersionManager:
    """记忆版本管理器 - 实现 Git for Memory 的核心功能"""

    @staticmethod
    def create_version(db: sqlite3.Connection, memory_id: str, content: str,
                       change_type: str = "create", changed_by: str = "system",
                       change_reason: str = None, metadata: dict = None) -> int:
        """创建新版本快照，返回版本号（原子化版本号分配）

        使用 INSERT ... SELECT MAX(version)+1 原子化分配版本号，
        避免并发场景下 SELECT + INSERT 之间的竞态条件。
        
        自动执行版本保留策略：创建新版本后，删除超过保留数量的旧版本。
        """
        version_id = str(uuid.uuid4())
        content_hash = compute_checksum(content)
        metadata_snapshot = json.dumps(metadata or {}, ensure_ascii=False)
        now = now_iso()

        # 计算与上一个版本的 diff（仅在已有版本时）
        diff_from_prev = None
        prev_row = db.execute(
            "SELECT content FROM memory_versions WHERE memory_id=? ORDER BY version DESC LIMIT 1",
            (memory_id,)
        ).fetchone()
        if prev_row:
            prev_lines = prev_row["content"].splitlines(keepends=True)
            curr_lines = content.splitlines(keepends=True)
            diff = list(difflib.unified_diff(prev_lines, curr_lines, lineterm=''))
            diff_from_prev = "\n".join(diff) if diff else None

        # 原子化分配版本号：用一个 INSERT 同时完成版本号计算和写入
        # 利用 SQLite 的子查询原子性，MAX(version)+1 在 INSERT 时计算
        try:
            db.execute(
                """INSERT INTO memory_versions
                   (id, memory_id, version, content, content_hash, diff_from_prev,
                    change_type, changed_by, change_reason, metadata_snapshot, created_at)
                   SELECT ?, ?, COALESCE(MAX(version), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?
                   FROM memory_versions WHERE memory_id=?""",
                (version_id, memory_id, content, content_hash,
                 diff_from_prev, change_type, changed_by, change_reason,
                 metadata_snapshot, now, memory_id)
            )
        except sqlite3.IntegrityError:
            # 极低概率：并发写入导致 UNIQUE(memory_id, version) 冲突，重试一次
            log.warning(f"Version conflict for {memory_id[:8]}..., retrying...")
            db.execute(
                """INSERT INTO memory_versions
                   (id, memory_id, version, content, content_hash, diff_from_prev,
                    change_type, changed_by, change_reason, metadata_snapshot, created_at)
                   SELECT ?, ?, COALESCE(MAX(version), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?
                   FROM memory_versions WHERE memory_id=?""",
                (str(uuid.uuid4()), memory_id, content, content_hash,
                 diff_from_prev, change_type, changed_by, change_reason,
                 metadata_snapshot, now, memory_id)
            )

        # 获取实际分配的版本号
        row = db.execute(
            "SELECT version FROM memory_versions WHERE id=?", (version_id,)
        ).fetchone()
        new_version = row["version"] if row else 1

        # 记录进化日志
        db.execute(
            """INSERT INTO evolution_log
               (memory_id, event_type, from_version, to_version, agent, details)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (memory_id, change_type, new_version - 1, new_version, changed_by,
             json.dumps({"reason": change_reason, "content_hash": content_hash}, ensure_ascii=False))
        )

        log.info(f"Version {new_version} created for memory {memory_id[:8]}... ({change_type})")

        # 自动执行版本保留策略：删除超过保留数量的旧版本
        try:
            version_count = db.execute(
                "SELECT COUNT(*) FROM memory_versions WHERE memory_id=?",
                (memory_id,)
            ).fetchone()[0]
            
            if version_count > VERSION_KEEP_COUNT:
                versions_to_delete = version_count - VERSION_KEEP_COUNT
                db.execute("""
                    DELETE FROM memory_versions 
                    WHERE memory_id = ? AND version IN (
                        SELECT version FROM memory_versions 
                        WHERE memory_id = ? 
                        ORDER BY version ASC 
                        LIMIT ?
                    )
                """, (memory_id, memory_id, versions_to_delete))
                log.info(f"Version retention: deleted {versions_to_delete} old versions for memory {memory_id[:8]}...")
        except Exception as e:
            log.warning(f"Version retention cleanup failed (non-fatal): {e}", exc_info=True)

        # Auto-detect bad evolution after version creation
        try:
            bad_result = VersionManager.detect_bad_evolution(db, memory_id, new_version)
            if bad_result.get("is_bad"):
                log.warning(
                    f"Bad evolution detected for memory {memory_id[:8]}... v{new_version}: "
                    f"{bad_result.get('reasons', [])}"
                )
                # Record warning in evolution_log
                db.execute(
                    """INSERT INTO evolution_log
                       (memory_id, event_type, from_version, to_version, agent, details)
                       VALUES (?, 'bad_evolution_warning', ?, ?, ?, ?)""",
                    (memory_id, new_version - 1, new_version, changed_by,
                     json.dumps(bad_result, ensure_ascii=False))
                )
        except Exception as e:
            log.debug(f"Bad evolution check skipped: {e}")

        return new_version

    @staticmethod
    def get_history(db: sqlite3.Connection, memory_id: str, limit: int = 50) -> list[dict]:
        """获取记忆的版本历史"""
        rows = db.execute(
            """SELECT id, version, content, content_hash, change_type, changed_by,
                      change_reason, created_at
               FROM memory_versions
               WHERE memory_id=?
               ORDER BY version DESC
               LIMIT ?""",
            (memory_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_version(db: sqlite3.Connection, memory_id: str, version: int) -> Optional[dict]:
        """获取指定版本"""
        row = db.execute(
            """SELECT * FROM memory_versions
               WHERE memory_id=? AND version=?""",
            (memory_id, version)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_diff(db: sqlite3.Connection, memory_id: str,
                 version_a: int, version_b: int) -> dict:
        """获取两个版本之间的 diff"""
        va = VersionManager.get_version(db, memory_id, version_a)
        vb = VersionManager.get_version(db, memory_id, version_b)

        if not va or not vb:
            return {"error": "Version not found"}

        lines_a = va["content"].splitlines(keepends=True)
        lines_b = vb["content"].splitlines(keepends=True)
        diff = list(difflib.unified_diff(lines_a, lines_b,
                                          fromfile=f"v{version_a}", tofile=f"v{version_b}",
                                          lineterm=''))

        return {
            "memory_id": memory_id,
            "version_a": version_a,
            "version_b": version_b,
            "diff": "\n".join(diff) if diff else "(no changes)",
            "hash_a": va["content_hash"],
            "hash_b": vb["content_hash"],
        }

    @staticmethod
    def rollback(db: sqlite3.Connection, memory_id: str,
                 target_version: int, agent: str = "system") -> dict:
        """回滚到指定版本
        
        注意：FTS5 索引会通过数据库触发器(memories_au)自动更新，
        无需手动重建。回滚后需要清除热缓存以确保搜索结果一致。
        """
        target = VersionManager.get_version(db, memory_id, target_version)
        if not target:
            return {"error": f"Version {target_version} not found"}

        # 获取当前记忆信息
        current = db.execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not current:
            return {"error": "Memory not found"}

        # 获取当前最新版本号（从 memory_versions 表，而非 memories 表）
        current_ver_row = db.execute(
            "SELECT MAX(version) as v FROM memory_versions WHERE memory_id=?",
            (memory_id,)
        ).fetchone()
        from_version = current_ver_row["v"] or 0

        # 更新记忆内容为历史版本
        # 注意：FTS5 会通过 memories_au 触发器自动更新
        now = now_iso()
        db.execute(
            """UPDATE memories
               SET content=?, checksum=?, simhash=?, updated_at=?
               WHERE id=?""",
            (target["content"], target["content_hash"],
             compute_simhash(target["content"]), now, memory_id)
        )

        # 创建新版本记录（标记为 rollback，create_version 内部会自动记录 evolution_log）
        new_ver = VersionManager.create_version(
            db, memory_id, target["content"],
            change_type="rollback",
            changed_by=agent,
            change_reason=f"Rollback from v{from_version} to v{target_version}"
        )

        log.info(f"Memory {memory_id[:8]}... rolled back from v{from_version} to v{target_version}")
        return {
            "success": True,
            "action": "rollback",
            "memory_id": memory_id,
            "from_version": from_version,
            "target_version": target_version,
            "new_version": new_ver,
            "note": "FTS5 index updated automatically by trigger"
        }

    # ═══ Git for Memory: Branching & Multi-Agent Coordination ═══

    @staticmethod
    def create_branch(db: sqlite3.Connection, memory_id: str,
                      branch_name: str, from_version: int = None,
                      source: str = "system") -> dict:
        """Create a named branch pointing to a specific version.

        If from_version is None, branch from the latest version.
        """
        # Check memory exists
        mem = db.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not mem:
            return {"error": "Memory not found"}

        # Resolve from_version
        if from_version is None:
            row = db.execute(
                "SELECT MAX(version) as max_ver FROM memory_versions WHERE memory_id=?",
                (memory_id,)
            ).fetchone()
            from_version = row["max_ver"] or 1

        # Check version exists
        ver = db.execute(
            "SELECT id FROM memory_versions WHERE memory_id=? AND version=?",
            (memory_id, from_version)
        ).fetchone()
        if not ver:
            return {"error": f"Version {from_version} not found"}

        # Check branch name uniqueness
        existing = db.execute(
            "SELECT id FROM memory_branches WHERE memory_id=? AND branch_name=?",
            (memory_id, branch_name)
        ).fetchone()
        if existing:
            return {"error": f"Branch '{branch_name}' already exists"}

        branch_id = str(uuid.uuid4())
        now = now_iso()
        db.execute(
            """INSERT INTO memory_branches (id, memory_id, branch_name, version, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (branch_id, memory_id, branch_name, from_version, source, now)
        )

        # Log branch creation event
        db.execute(
            """INSERT INTO evolution_log
               (memory_id, event_type, from_version, to_version, agent, details)
               VALUES (?, 'branch_create', ?, ?, ?, ?)""",
            (memory_id, 0, from_version, source,
             json.dumps({"branch_name": branch_name}, ensure_ascii=False))
        )

        log.info(f"Branch '{branch_name}' created for memory {memory_id[:8]}... at v{from_version}")

        return {
            "success": True,
            "action": "branch_create",
            "branch_id": branch_id,
            "memory_id": memory_id,
            "branch_name": branch_name,
            "version": from_version,
        }

    @staticmethod
    def list_branches(db: sqlite3.Connection, memory_id: str) -> list[dict]:
        """List all branches for a memory."""
        rows = db.execute(
            """SELECT id, memory_id, branch_name, version, source, created_at
               FROM memory_branches
               WHERE memory_id=?
               ORDER BY created_at DESC""",
            (memory_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def merge_branch(db: sqlite3.Connection, memory_id: str,
                     source_branch: str, target_branch: str,
                     agent: str = "system") -> dict:
        """Merge source_branch into target_branch.

        Takes the content at source_branch's version, applies it as a new version,
        and updates the target_branch pointer.
        """
        src = db.execute(
            "SELECT * FROM memory_branches WHERE memory_id=? AND branch_name=?",
            (memory_id, source_branch)
        ).fetchone()
        tgt = db.execute(
            "SELECT * FROM memory_branches WHERE memory_id=? AND branch_name=?",
            (memory_id, target_branch)
        ).fetchone()

        if not src:
            return {"error": f"Source branch '{source_branch}' not found"}
        if not tgt:
            return {"error": f"Target branch '{target_branch}' not found"}

        # Get source content
        src_ver = VersionManager.get_version(db, memory_id, src["version"])
        if not src_ver:
            return {"error": f"Source version {src['version']} not found"}

        # Get target current content for diff
        tgt_ver = VersionManager.get_version(db, memory_id, tgt["version"])
        if not tgt_ver:
            return {"error": f"Target version {tgt['version']} not found"}

        # Create new version with merged content (source wins)
        merged_content = src_ver["content"]
        new_ver = VersionManager.create_version(
            db, memory_id, merged_content,
            change_type="merge",
            changed_by=agent,
            change_reason=f"Merge '{source_branch}' into '{target_branch}'"
        )

        # Update target branch pointer
        db.execute(
            "UPDATE memory_branches SET version=?, source=? WHERE id=?",
            (new_ver, agent, tgt["id"])
        )

        # Also update the actual memory content
        now = now_iso()
        db.execute(
            "UPDATE memories SET content=?, checksum=?, simhash=?, updated_at=? WHERE id=?",
            (merged_content, compute_checksum(merged_content),
             compute_simhash(merged_content), now, memory_id)
        )

        # Log merge event
        db.execute(
            """INSERT INTO evolution_log
               (memory_id, event_type, from_version, to_version, agent, details)
               VALUES (?, 'merge', ?, ?, ?, ?)""",
            (memory_id, tgt["version"], new_ver, agent,
             json.dumps({
                 "source_branch": source_branch,
                 "target_branch": target_branch,
                 "source_version": src["version"],
             }, ensure_ascii=False))
        )

        log.info(
            f"Merged '{source_branch}' (v{src['version']}) -> '{target_branch}' "
            f"for memory {memory_id[:8]}... -> v{new_ver}"
        )
        return {
            "success": True,
            "action": "merge",
            "memory_id": memory_id,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "source_version": src["version"],
            "new_version": new_ver,
        }

    @staticmethod
    def detect_bad_evolution(db: sqlite3.Connection, memory_id: str,
                             version: int) -> dict:
        """Detect bad evolution: content catastrophic loss (>80%) or large drift (>30 bits)."""
        curr = db.execute(
            "SELECT content, content_hash FROM memory_versions WHERE memory_id=? AND version=?",
            (memory_id, version)
        ).fetchone()
        if not curr:
            return {"is_bad": False, "reason": "Version not found"}

        prev = db.execute(
            "SELECT content, content_hash FROM memory_versions WHERE memory_id=? AND version=?",
            (memory_id, version - 1)
        ).fetchone()
        if not prev:
            return {"is_bad": False, "reason": "No previous version to compare"}

        reasons = []

        # Check 1: content catastrophic loss > 80%
        prev_len = len(prev["content"])
        curr_len = len(curr["content"])
        if prev_len > 100 and curr_len > 0:  # only check for non-trivial content
            shrink_pct = (prev_len - curr_len) / prev_len * 100
            if shrink_pct > 80:
                reasons.append(f"Content shortened by {shrink_pct:.1f}% ({prev_len}->{curr_len} chars)")

        # Check 2: SimHash drift > 30 bits (content ~53%+ different)
        prev_hash = compute_simhash(prev["content"])
        curr_hash = compute_simhash(curr["content"])
        distance = hamming_distance(prev_hash, curr_hash)
        if distance > 30:
            reasons.append(f"SimHash drifted {distance} bits (threshold: 30)")

        is_bad = len(reasons) > 0
        return {
            "is_bad": is_bad,
            "memory_id": memory_id,
            "version": version,
            "reasons": reasons,
            "shrink_pct": round((prev_len - curr_len) / prev_len * 100, 1) if prev_len > 0 else 0,
            "simhash_distance": distance,
            "prev_simhash": prev_hash,
            "curr_simhash": curr_hash,
        }

    @staticmethod
    def auto_rollback_if_bad(db: sqlite3.Connection, memory_id: str,
                             version: int, agent: str = "system") -> dict:
        """Check for bad evolution and auto-rollback if detected.

        Rolls back to the previous version if bad evolution is confirmed.
        """
        bad_result = VersionManager.detect_bad_evolution(db, memory_id, version)
        if not bad_result.get("is_bad"):
            return {"rolled_back": False, "reason": "Evolution is healthy", **bad_result}

        target_version = version - 1
        rollback_result = VersionManager.rollback(db, memory_id, target_version, agent)

        log.warning(
            f"Auto-rollback for memory {memory_id[:8]}... from v{version} to v{target_version}: "
            f"{bad_result.get('reasons', [])}"
        )

        return {
            "rolled_back": True,
            "reason": "Bad evolution detected",
            "bad_evolution": bad_result,
            "rollback": rollback_result,
        }
