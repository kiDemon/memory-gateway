#!/usr/bin/env python3
"""批量删除 Memory Gateway 错误记录 (source=system, category=learning)。"""

import json
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "http://8.137.178.236:8650"
HEADERS = {
    "X-API-Key": "sk-mg-7HaIpKYDNty-yBguQwV-UoeeyPhEftDVbbnVCWOvRT7bpnC6",
    "Content-Type": "application/json",
}


def _request(method: str, path: str, body: dict = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=HEADERS,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_all_ids() -> list[str]:
    """通过 /mcp/export 获取所有 source=system 记录的ID。"""
    print("Fetching all system record IDs via export...")
    result = _request("GET", "/mcp/export?source=system")
    memories = result.get("memories", [])
    ids = [m["id"] for m in memories]
    print(f"  Got {len(ids)} IDs")
    return ids


def delete_one(memory_id: str) -> tuple:
    """删除单条记录。"""
    try:
        _request("DELETE", f"/mcp/delete/{memory_id}")
        return (memory_id, True, "")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:100] if e.fp else ""
        if e.code == 404:
            return (memory_id, True, "already deleted")
        return (memory_id, False, f"HTTP {e.code}: {body}")
    except Exception as e:
        return (memory_id, False, str(e))


def main():
    print("=" * 60)
    print("Memory Gateway System Record Cleanup")
    print("=" * 60)

    # Stats before
    stats = _request("GET", "/mcp/stats")
    total_system = stats.get("by_source", {}).get("system", 0)
    total_all = stats.get("total", 0)
    print(f"\nBefore: {total_system} system records / {total_all} total")

    # Get all IDs
    all_ids = get_all_ids()
    if not all_ids:
        print("No records to delete!")
        return

    print(f"\nDeleting {len(all_ids)} records in parallel...")

    BATCH_SIZE = 30
    CONCURRENT = 15
    total = len(all_ids)
    deleted = 0
    failed = 0
    errors = []
    start = time.time()

    for batch_start in range(0, total, BATCH_SIZE):
        batch = all_ids[batch_start:batch_start + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=CONCURRENT) as pool:
            futures = {pool.submit(delete_one, mid): mid for mid in batch}
            for f in as_completed(futures):
                mid, ok, err = f.result()
                if ok:
                    deleted += 1
                else:
                    failed += 1
                    errors.append((mid[:8], err))

        done = min(batch_start + BATCH_SIZE, total)
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        pct = done / total * 100
        eta = (total - done) / rate if rate > 0 else 0

        # Print progress periodically
        if (batch_start // BATCH_SIZE + 1) % 20 == 0 or done == total:
            print(f"  [{done}/{total}] {pct:.1f}% | Deleted: {deleted} | "
                  f"Failed: {failed} | {rate:.0f}/s | ETA: {eta:.0f}s")

        # Rate limit
        time.sleep(0.05)

    elapsed = time.time() - start
    print(f"\n{'=' * 60}")
    print(f"Complete! Time: {elapsed:.0f}s | Rate: {deleted/elapsed:.0f}/s")
    print(f"  Deleted: {deleted} | Failed: {failed}")

    if errors:
        print(f"\n  First 5 errors:")
        for mid, err in errors[:5]:
            print(f"    {mid}: {err}")

    # Verify
    stats = _request("GET", "/mcp/stats")
    remaining = stats.get("by_source", {}).get("system", 0)
    print(f"\n  Remaining system records: {remaining}")
    print(f"  Total records: {stats.get('total', 0)}")


if __name__ == "__main__":
    main()
