"""Flat CLI entry point for scheduled shop maintenance jobs.

Run from the shop container/repository root with ``python -m jobs``.  The
module deliberately contains no scheduler: systemd invokes these one-shot
commands and records every non-dry-run attempt in ``job_runs``.
"""

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from datetime import timedelta

import database
import services
from panel_client import PanelClientError


logger = logging.getLogger(__name__)
RECONCILE_AFTER_MINUTES = 20


def get_panel_client():
    return services.get_panel_client()


def get_platega_client():
    return services.get_platega_client()


def _migration_path(db_path=None):
    return os.path.abspath(db_path or os.environ.get("SHOP_DB_PATH") or database.DB_PATH)


def _migration_connection(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _guard_migration_path(path, verify_only, dry_run):
    if verify_only or dry_run or os.environ.get("MIGRATE_MONEY_ALLOW_PRODUCTION") == "1":
        return
    if path == os.path.abspath(database.DB_PATH):
        raise RuntimeError(
            "refusing full money migration on the configured database; "
            "set SHOP_DB_PATH to a verified copy or explicitly authorize the maintenance window"
        )


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_backfill_columns(conn):
    for table, column in database.MONEY_COLUMNS:
        columns = _table_columns(conn, table)
        backfill = f"{column}_rub"
        if backfill not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {backfill} INTEGER")
        conn.execute(
            f"UPDATE {table} SET {backfill} = CAST(FLOOR({column}) AS INTEGER) "
            f"WHERE {column} IS NOT NULL"
        )


def _verify_backfill(conn):
    report = []
    for table, column in database.MONEY_COLUMNS:
        backfill = f"{column}_rub"
        row = conn.execute(
            f"SELECT COUNT(*) AS rows, "
            f"COALESCE(MAX(ABS({column} - {backfill})), 0) AS max_delta, "
            f"COALESCE(SUM(CAST(FLOOR({column}) AS INTEGER) - {backfill}), 0) AS sum_delta "
            f"FROM {table} WHERE {column} IS NOT NULL"
        ).fetchone()
        item = {
            "table": table,
            "column": column,
            "rows": row["rows"],
            "max_delta": float(row["max_delta"]),
            "sum_delta": int(row["sum_delta"]),
        }
        report.append(item)
        if item["max_delta"] > 1 or item["sum_delta"] != 0:
            raise RuntimeError(f"money migration verification failed: {item}")
    return report


def _timestamp_report(conn):
    report = []
    for table, column in database.TIMESTAMP_COLUMNS:
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} NOT LIKE '%+00:00' "
            f"AND {column} NOT LIKE '%Z'"
        ).fetchone()
        report.append({"table": table, "column": column, "rows": row["count"]})
    return report


def _print_migration_report(report, timestamp_report, prefix=""):
    for item in report:
        print(
            f"{prefix}{item['table']}.{item['column']}: rows={item['rows']} "
            f"max_delta={item['max_delta']:g} sum_delta={item['sum_delta']}"
        )
    for item in timestamp_report:
        print(f"{prefix}{item['table']}.{item['column']}: naive_timestamp_rows={item['rows']}")


def migrate_money(db_path=None, verify_only=False, dry_run=False):
    """Rehearse or execute the one-time REAL-to-INTEGER money migration."""
    if verify_only and dry_run:
        raise ValueError("--verify-only and --dry-run are mutually exclusive")
    path = _migration_path(db_path)
    _guard_migration_path(path, verify_only, dry_run)
    conn = _migration_connection(path)
    try:
        if dry_run:
            print("migrate-money dry-run: no changes will be written")
            for table, column in database.MONEY_COLUMNS:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL"
                ).fetchone()[0]
                print(f"  ADD {table}.{column}_rub INTEGER; affected_rows={count}")
            for table, column in database.TIMESTAMP_COLUMNS:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL AND "
                    f"{column} NOT LIKE '%+00:00' AND {column} NOT LIKE '%Z'"
                ).fetchone()[0]
                print(f"  NORMALIZE {table}.{column}; affected_rows={count}")
            return

        conn.execute("BEGIN IMMEDIATE")
        _ensure_backfill_columns(conn)
        report = _verify_backfill(conn)
        timestamp_report = _timestamp_report(conn)
        _print_migration_report(report, timestamp_report, prefix="verify: ")
        if verify_only:
            conn.commit()
            return report

        for table, column in database.TIMESTAMP_COLUMNS:
            conn.execute(
                f"UPDATE {table} SET {column} = {column} || '+00:00' "
                f"WHERE {column} IS NOT NULL AND {column} NOT LIKE '%+00:00' "
                f"AND {column} NOT LIKE '%Z'"
            )

        for table in dict.fromkeys(table for table, _ in database.MONEY_COLUMNS):
            table_columns = [(t, c) for t, c in database.MONEY_COLUMNS if t == table]
            for _, column in table_columns:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            for _, column in table_columns:
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN {column}_rub TO {column}")
        conn.commit()
        print("migrate-money: full migration committed")
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _record_job_run(job_name, started_at, ok, error=None):
    conn = database.get_db()
    try:
        conn.execute(
            "INSERT INTO job_runs (job_name, started_at, finished_at, ok, error) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_name, started_at, services.now_iso(), 1 if ok else 0, error),
        )
        conn.commit()
    finally:
        conn.close()


def _expiry_plan():
    now = services.now_iso()
    stale_before = (services.utcnow() - timedelta(hours=1)).isoformat()
    conn = database.get_db()
    try:
        expired = [dict(row) for row in conn.execute(
            "SELECT id, user_id, plan_name, expires_at FROM orders "
            "WHERE status = 'paid' AND expires_at IS NOT NULL AND expires_at <= ? "
            "ORDER BY expires_at, id",
            (now,),
        ).fetchall()]
        stale = [dict(row) for row in conn.execute(
            "SELECT id, user_id, balance_used_rub, created_at FROM orders "
            "WHERE status = 'pending' AND balance_used_rub > 0 AND created_at <= ? "
            "ORDER BY created_at, id",
            (stale_before,),
        ).fetchall()]
        return expired, stale
    finally:
        conn.close()


async def run_expiry(dry_run=False):
    expired, stale = _expiry_plan()
    if dry_run:
        for order in expired:
            print(f"expiry: would expire order {order['id']} user={order['user_id']} "
                  f"subscription={order.get('plan_name') or 'unknown'}")
        for order in stale:
            print(f"expiry: would refund stale order {order['id']} user={order['user_id']}")
        return

    now = services.now_iso()
    expired_users = set()
    with database.tx() as conn:
        for order in expired:
            claim = conn.execute(
                "UPDATE orders SET status = 'expired' WHERE id = ? AND status = 'paid' "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (order["id"], now),
            )
            if claim.rowcount == 1:
                expired_users.add(order["user_id"])

    if expired_users:
        panel = get_panel_client()
        conn = database.get_db()
        try:
            users = {
                row["id"]: dict(row)
                for row in conn.execute(
                    "SELECT id, username FROM app_users WHERE id IN ({})".format(
                        ",".join("?" for _ in expired_users)
                    ),
                    tuple(expired_users),
                ).fetchall()
            }
        finally:
            conn.close()
        for user_id in expired_users:
            app_user = users.get(user_id)
            if not app_user:
                continue
            try:
                panel_user = await panel.find_user_by_username(app_user["username"])
                if panel_user:
                    await panel.update_panel_user(panel_user["id"], expiration_date=None)
            except PanelClientError as exc:
                logger.error("expiry: panel disable failed for user %s: %s", user_id, exc)

    for order in stale:
        services.refund_order_balance(order["id"])


def _reconcile_plan():
    cutoff = (services.utcnow() - timedelta(minutes=RECONCILE_AFTER_MINUTES)).isoformat()
    conn = database.get_db()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM orders WHERE status = 'pending' "
            "AND created_at <= ? AND platega_transaction_id IS NOT NULL "
            "AND platega_transaction_id != '' ORDER BY created_at, id",
            (cutoff,),
        ).fetchall()]
    finally:
        conn.close()


async def _reconcile_order(order):
    status_response = await get_platega_client().get_payment_status(
        order["platega_transaction_id"]
    )
    status = str(status_response.get("status", "")).upper()
    if status in {"NOT_FOUND", "CANCELED", "CANCELLED"}:
        services.refund_order_balance(order["id"])
        return
    if status != "CONFIRMED":
        logger.info("reconcile: ignoring order %s with status %s", order["id"], status)
        return

    claimed_order = None
    with database.tx() as conn:
        claim = conn.execute(
            "UPDATE orders SET status = 'paid', paid_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (services.now_iso(), order["id"]),
        )
        if claim.rowcount != 1:
            return
        claimed_order = dict(conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order["id"],)
        ).fetchone())
        if claimed_order["plan_id"] is None:
            services.add_balance_int(
                conn,
                claimed_order["user_id"],
                services.money.to_rub(claimed_order["amount_rub"] or 0),
                "topup",
                ref_order_id=claimed_order["id"],
                note="Пополнение баланса",
            )
            services.log_event(conn, claimed_order["user_id"], "topup_paid")
        else:
            services.log_event(conn, claimed_order["user_id"], "order_paid")
        services.claim_apply_referral(conn, claimed_order)

    if claimed_order["plan_id"] is not None:
        await services.fulfill_order_side_effects(claimed_order)


async def run_reconcile(dry_run=False):
    orders = _reconcile_plan()
    if dry_run:
        for order in orders:
            print(f"reconcile: would check order {order['id']} "
                  f"transaction={order['platega_transaction_id']}")
        return
    for order in orders:
        await _reconcile_order(order)


async def _run_subcommand(job_name, dry_run, verify_only=False):
    if job_name == "expiry":
        await run_expiry(dry_run=dry_run)
    elif job_name == "reconcile":
        await run_reconcile(dry_run=dry_run)
    elif job_name == "migrate-money":
        migrate_money(verify_only=verify_only, dry_run=dry_run)
    else:
        raise ValueError(f"unknown job: {job_name}")


def run_job(job_name, dry_run=False, verify_only=False):
    if os.environ.get("SHOP_DB_PATH"):
        database.DB_PATH = _migration_path()
    started_at = services.now_iso()
    try:
        asyncio.run(_run_subcommand(job_name, dry_run, verify_only=verify_only))
    except Exception as exc:
        logger.error("job %s failed: %s", job_name, exc, exc_info=True)
        if not dry_run:
            _record_job_run(job_name, started_at, False, str(exc))
        raise SystemExit(1) from exc
    if not dry_run:
        _record_job_run(job_name, started_at, True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="vpn-shop scheduled jobs")
    parser.add_argument("job", choices=("expiry", "reconcile", "migrate-money"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    return run_job(args.job, dry_run=args.dry_run, verify_only=args.verify_only)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
