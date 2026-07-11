"""Throwaway diagnostic (Ticket A dry-run): why does the active-attestation
harvest query hang? Runs a statement-timeout-bounded count of
``api_contributorattestation`` and, if it blocks, reports what holds a lock on
the table. Safe to delete once the harvest is verified.

    python -m clustering.harvest._probe_attestations
"""

from __future__ import annotations

import asyncio


async def _main() -> None:
    from clustering.pg_client import pg_connection

    async with pg_connection() as c:
        await c.execute("SET statement_timeout='10s'")
        # 1. table present?
        exists = await c.fetchval("SELECT to_regclass('api_contributorattestation')")
        print("table exists:", exists)
        if exists is None:
            print("=> api_contributorattestation does NOT exist in this DB")
            return
        # 2. bounded counts
        try:
            active = await c.fetchval(
                "SELECT count(*) FROM api_contributorattestation WHERE status='active'")
            total = await c.fetchval("SELECT count(*) FROM api_contributorattestation")
            print(f"active: {active}   total: {total}")
            by_status = await c.fetch(
                "SELECT status, count(*) AS n FROM api_contributorattestation "
                "GROUP BY status ORDER BY n DESC")
            for r in by_status:
                print(f"  status={r['status']!r}: {r['n']}")
        except Exception as exc:  # noqa: BLE001
            print("BLOCKED/slow:", type(exc).__name__, exc)
            # 3. who holds a lock on the table?
            try:
                rows = await c.fetch(
                    "SELECT a.pid, a.state, l.mode, a.wait_event_type, "
                    "left(a.query, 200) AS query "
                    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.relation = 'api_contributorattestation'::regclass "
                    "AND a.pid <> pg_backend_pid()")
                if not rows:
                    print("  (no other backend holds a lock on the table)")
                for r in rows:
                    print("  lock:", dict(r))
            except Exception as exc2:  # noqa: BLE001
                print("  lock check failed:", exc2)


if __name__ == "__main__":
    asyncio.run(_main())
