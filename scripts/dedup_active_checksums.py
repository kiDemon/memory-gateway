#!/usr/bin/env python3
"""活跃记忆去重维护脚本（checksum 重复组）。

背景：历史上 save 与 batch_save 两条写入路径的指纹算法不一致（是否 strip/
过滤敏感信息），并发或跨路径写入会在库里留下 content 相同、checksum 相同的
多条活跃记忆。checksum 唯一索引（部分索引 WHERE archived=0）能挡住新重复，
但历史重复需要本脚本清理，否则唯一索引创建会失败并降级为告警。

行为：
  - 默认 dry-run，只报告重复组，不改数据；
  - --apply 时每组保留"最优"一条（recall_count 高 → updated_at 新），
    其余置 archived=1 并把 superseded_by 写回保留条 ID（保留追溯链），
    同时写 change_log(archived_by_dedup)。

用法（容器内）：
    docker exec memory-gateway python scripts/dedup_active_checksums.py
    docker exec memory-gateway python scripts/dedup_active_checksums.py --apply
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_gateway.database.connection import get_db  # noqa: E402


def find_groups(db) -> list[list]:
    rows = db.execute(
        """SELECT id, checksum, recall_count, updated_at, created_at, length(content) AS clen
           FROM memories WHERE archived=0
           ORDER BY checksum, recall_count DESC, updated_at DESC"""
    ).fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["checksum"], []).append(r)
    return [g for g in groups.values() if len(g) > 1]


def main() -> int:
    ap = argparse.ArgumentParser(description="清理活跃记忆中的 checksum 重复组")
    ap.add_argument("--apply", action="store_true", help="实际执行归档（默认只报告）")
    args = ap.parse_args()

    db = get_db()
    try:
        groups = find_groups(db)
        if not groups:
            print("没有发现活跃重复：库是干净的")
            return 0

        total_dup = sum(len(g) - 1 for g in groups)
        print(f"发现 {len(groups)} 组重复，涉及 {total_dup} 条可归档的冗余记忆")
        for g in groups:
            keep = g[0]
            others = g[1:]
            print(f"\n  checksum={keep['checksum'][:12]}… 保留 {keep['id'][:8]} "
                  f"(recall={keep['recall_count']}, {keep['updated_at']})")
            for o in others:
                print(f"    → 归档 {o['id'][:8]} (recall={o['recall_count']}, {o['updated_at']})")

        if not args.apply:
            print("\n（dry-run，未修改数据；加 --apply 执行归档）")
            return 0

        archived = 0
        for g in groups:
            keep = g[0]
            for o in g[1:]:
                db.execute(
                    "UPDATE memories SET archived=1, superseded_by=?, updated_at=datetime('now') "
                    "WHERE id=?",
                    (keep["id"], o["id"]),
                )
                db.execute(
                    "INSERT INTO change_log (memory_id, action, snapshot, timestamp) "
                    "VALUES (?, 'archived_by_dedup', ?, datetime('now'))",
                    (o["id"], f"duplicate_of={keep['id']}"),
                )
                archived += 1
        db.commit()
        print(f"\n已归档 {archived} 条冗余记忆（未删除，仍可 mem_get 追溯）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
