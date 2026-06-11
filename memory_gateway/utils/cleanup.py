"""
Database cleanup and maintenance utilities.

Handles:
- Version retention policy (limit versions per memory)
- Old relations cleanup
- Old change_log cleanup
- Database VACUUM optimization
"""

import sqlite3
import logging
from datetime import datetime, timedelta, timezone

from memory_gateway.config import log
from memory_gateway.utils import now_iso

# 版本保留策略配置
VERSION_KEEP_COUNT = 10  # 每个记忆最多保留的版本数
RELATION_MAX_AGE_DAYS = 90  # 关系最大保留天数
CHANGE_LOG_MAX_AGE_DAYS = 30  # 变更日志最大保留天数
EVOLUTION_LOG_MAX_AGE_DAYS = 180  # 进化日志最大保留天数


def cleanup_old_versions(db: sqlite3.Connection, keep_count: int = VERSION_KEEP_COUNT) -> dict:
    """删除超过保留数量的旧版本。
    
    每个记忆只保留最新的 keep_count 个版本，删除更早的版本。
    
    Args:
        db: 数据库连接
        keep_count: 每个记忆保留的版本数
    
    Returns:
        清理统计信息
    """
    stats = {"deleted_versions": 0, "affected_memories": 0}
    
    # 找出需要清理的记忆
    rows = db.execute("""
        SELECT memory_id, COUNT(*) as version_count
        FROM memory_versions
        GROUP BY memory_id
        HAVING version_count > ?
    """, (keep_count,)).fetchall()
    
    stats["affected_memories"] = len(rows)
    
    for row in rows:
        memory_id = row["memory_id"]
        version_count = row["version_count"]
        versions_to_delete = version_count - keep_count
        
        # 删除最旧的版本
        db.execute("""
            DELETE FROM memory_versions 
            WHERE memory_id = ? AND version IN (
                SELECT version FROM memory_versions 
                WHERE memory_id = ? 
                ORDER BY version ASC 
                LIMIT ?
            )
        """, (memory_id, memory_id, versions_to_delete))
        
        stats["deleted_versions"] += versions_to_delete
    
    if stats["deleted_versions"] > 0:
        log.info(f"Version cleanup: deleted {stats['deleted_versions']} old versions from {stats['affected_memories']} memories")
    
    return stats


def cleanup_old_relations(db: sqlite3.Connection, max_age_days: int = RELATION_MAX_AGE_DAYS) -> dict:
    """删除过期的关系记录。
    
    删除超过 max_age_days 天的关系记录。
    
    Args:
        db: 数据库连接
        max_age_days: 关系最大保留天数
    
    Returns:
        清理统计信息
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    
    result = db.execute(
        "DELETE FROM memory_relations WHERE created_at < ?",
        (cutoff,)
    )
    
    stats = {"deleted_relations": result.rowcount}
    
    if stats["deleted_relations"] > 0:
        log.info(f"Relation cleanup: deleted {stats['deleted_relations']} old relations (>{max_age_days} days)")
    
    return stats


def cleanup_old_change_log(db: sqlite3.Connection, max_age_days: int = CHANGE_LOG_MAX_AGE_DAYS) -> dict:
    """删除过期的变更日志。
    
    删除超过 max_age_days 天的变更日志。
    
    Args:
        db: 数据库连接
        max_age_days: 日志最大保留天数
    
    Returns:
        清理统计信息
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    
    result = db.execute(
        "DELETE FROM change_log WHERE timestamp < ?",
        (cutoff,)
    )
    
    stats = {"deleted_logs": result.rowcount}
    
    if stats["deleted_logs"] > 0:
        log.info(f"Change log cleanup: deleted {stats['deleted_logs']} old entries (>{max_age_days} days)")
    
    return stats


def cleanup_old_evolution_log(db: sqlite3.Connection, max_age_days: int = EVOLUTION_LOG_MAX_AGE_DAYS) -> dict:
    """删除过期的进化日志。
    
    删除超过 max_age_days 天的进化日志。
    
    Args:
        db: 数据库连接
        max_age_days: 日志最大保留天数
    
    Returns:
        清理统计信息
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    
    result = db.execute(
        "DELETE FROM evolution_log WHERE created_at < ?",
        (cutoff,)
    )
    
    stats = {"deleted_logs": result.rowcount}
    
    if stats["deleted_logs"] > 0:
        log.info(f"Evolution log cleanup: deleted {stats['deleted_logs']} old entries (>{max_age_days} days)")
    
    return stats


def cleanup_old_search_audit(db: sqlite3.Connection, max_age_days: int = 30) -> dict:
    """删除过期的搜索审计日志。
    
    删除超过 max_age_days 天的搜索审计日志。
    
    Args:
        db: 数据库连接
        max_age_days: 日志最大保留天数
    
    Returns:
        清理统计信息
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    
    result = db.execute(
        "DELETE FROM search_audit_log WHERE created_at < ?",
        (cutoff,)
    )
    
    stats = {"deleted_logs": result.rowcount}
    
    if stats["deleted_logs"] > 0:
        log.info(f"Search audit cleanup: deleted {stats['deleted_logs']} old entries (>{max_age_days} days)")
    
    return stats


def vacuum_database(db_path: str) -> dict:
    """优化数据库，回收空间。
    
    执行 VACUUM 命令来重建数据库文件，回收已删除数据占用的空间。
    
    Args:
        db_path: 数据库文件路径
    
    Returns:
        优化统计信息
    """
    import os
    
    # 获取优化前的文件大小
    size_before = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    
    # 执行 VACUUM
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("VACUUM")
    conn.close()
    
    # 获取优化后的文件大小
    size_after = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    
    stats = {
        "size_before_mb": round(size_before / 1024 / 1024, 2),
        "size_after_mb": round(size_after / 1024 / 1024, 2),
        "reclaimed_mb": round((size_before - size_after) / 1024 / 1024, 2),
        "reclaimed_percent": round((size_before - size_after) / size_before * 100, 2) if size_before > 0 else 0
    }
    
    log.info(f"Database VACUUM: {stats['size_before_mb']}MB -> {stats['size_after_mb']}MB (reclaimed {stats['reclaimed_mb']}MB, {stats['reclaimed_percent']}%)")
    
    return stats


def full_cleanup(db_path: str, 
                 version_keep_count: int = VERSION_KEEP_COUNT,
                 relation_max_age_days: int = RELATION_MAX_AGE_DAYS,
                 change_log_max_age_days: int = CHANGE_LOG_MAX_AGE_DAYS,
                 evolution_log_max_age_days: int = EVOLUTION_LOG_MAX_AGE_DAYS,
                 search_audit_max_age_days: int = 30,
                 vacuum: bool = True) -> dict:
    """执行完整的数据库清理。
    
    Args:
        db_path: 数据库文件路径
        version_keep_count: 每个记忆保留的版本数
        relation_max_age_days: 关系最大保留天数
        change_log_max_age_days: 变更日志最大保留天数
        evolution_log_max_age_days: 进化日志最大保留天数
        search_audit_max_age_days: 搜索审计日志最大保留天数
        vacuum: 是否执行 VACUUM
    
    Returns:
        完整清理统计信息
    """
    import sqlite3
    
    stats = {
        "versions": {},
        "relations": {},
        "change_log": {},
        "evolution_log": {},
        "search_audit": {},
        "vacuum": {}
    }
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    try:
        # 执行各项清理
        stats["versions"] = cleanup_old_versions(conn, version_keep_count)
        stats["relations"] = cleanup_old_relations(conn, relation_max_age_days)
        stats["change_log"] = cleanup_old_change_log(conn, change_log_max_age_days)
        stats["evolution_log"] = cleanup_old_evolution_log(conn, evolution_log_max_age_days)
        stats["search_audit"] = cleanup_old_search_audit(conn, search_audit_max_age_days)
        
        # 提交所有更改
        conn.commit()
        
        # 执行 VACUUM
        if vacuum:
            conn.close()
            stats["vacuum"] = vacuum_database(db_path)
        else:
            stats["vacuum"] = {"skipped": True}
        
        log.info(f"Full cleanup completed: {stats}")
        
    except Exception as e:
        log.error(f"Full cleanup failed: {e}", exc_info=True)
        raise
    finally:
        if conn:
            conn.close()
    
    return stats


if __name__ == "__main__":
    # 用于测试
    import sys
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
        print(f"Running full cleanup on {db_path}...")
        result = full_cleanup(db_path)
        print(f"Result: {result}")
    else:
        print("Usage: python cleanup_utils.py <db_path>")
