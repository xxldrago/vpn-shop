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
import bot
from config import DB_PATH as CONFIGURED_DB_PATH
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
    if path == os.path.abspath(CONFIGURED_DB_PATH):
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


def _ensure_job_runs_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS job_runs ("
        "job_name TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL, "
        "ok INTEGER NOT NULL, error TEXT)"
    )


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
        _ensure_job_runs_table(conn)
        _ensure_backfill_columns(conn)
        report = _verify_backfill(conn)
        timestamp_report = _timestamp_report(conn)
        _print_migration_report(report, timestamp_report, prefix="verify: ")
        if verify_only:
            # A rehearsal on the live configured DB must be read-only. Explicit
            # copy paths retain the backfill columns so operators can inspect them.
            if path == os.path.abspath(CONFIGURED_DB_PATH):
                conn.rollback()
            else:
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
        _ensure_job_runs_table(conn)
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
    # Fold auto-renewal and reminders into the expiry driver (D-16)
    # so they run on the existing systemd timer without a new timer.
    await run_auto_renewal(dry_run=dry_run)
    await run_reminder_scan(dry_run=dry_run)
    
    expired, stale = _expiry_plan()
    if dry_run:
        for order in expired:
            print(f"expiry: would expire order {order['id']} user={order['user_id']} "
                  f"subscription={order.get('plan_name') or 'unknown'}")
        for order in stale:
            print(f"expiry: would refund stale order {order['id']} user={order['user_id']}")
        return

    now = services.now_iso()
    panel = get_panel_client()
    users_to_disable = set()
    for order in expired:
        conn = database.get_db()
        try:
            active = conn.execute(
                "SELECT 1 FROM orders WHERE user_id = ? AND status = 'paid' "
                "AND expires_at IS NOT NULL AND expires_at > ? LIMIT 1",
                (order["user_id"], now),
            ).fetchone()
            if active:
                with database.tx() as tx_conn:
                    tx_conn.execute(
                        "UPDATE orders SET status = 'expired' WHERE id = ? AND status = 'paid' "
                        "AND expires_at IS NOT NULL AND expires_at <= ?",
                        (order["id"], now),
                    )
                continue
            users_to_disable.add(order["user_id"])
        finally:
            conn.close()

    if users_to_disable:
        panel = get_panel_client()
        conn = database.get_db()
        try:
            users = {
                row["id"]: dict(row)
                for row in conn.execute(
                    "SELECT id, username FROM app_users WHERE id IN ({})".format(
                        ",".join("?" for _ in users_to_disable)
                    ),
                    tuple(users_to_disable),
                ).fetchall()
            }
        finally:
            conn.close()
        for user_id in users_to_disable:
            app_user = users.get(user_id)
            if not app_user:
                continue
            try:
                panel_user = await panel.find_user_by_username(app_user["username"])
                if panel_user:
                    await panel.update_panel_user(panel_user["id"], expiration_date=None)
                with database.tx() as tx_conn:
                    tx_conn.execute(
                        "UPDATE orders SET status = 'expired' WHERE user_id = ? AND status = 'paid' "
                        "AND expires_at IS NOT NULL AND expires_at <= ?",
                        (user_id, now),
                    )
            except PanelClientError as exc:
                logger.error("expiry: panel disable failed for user %s: %s", user_id, exc)

    for order in stale:
        services.refund_order_balance(order["id"])


def _auto_renewal_plan():
    """Find due auto-renewal candidates: paid, non-trial, plan-based orders where
    the owning user has auto_renewal=1, expires_at <= now, and no newer active paid subscription exists (D-14)."""
    now = services.now_iso()
    conn = database.get_db()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT o.id, o.user_id, o.plan_id, o.plan_name, o.expires_at "
            "FROM orders o "
            "JOIN app_users u ON u.id = o.user_id "
            "WHERE o.status = 'paid' AND u.auto_renewal = 1 "
            "AND o.plan_id IS NOT NULL AND o.is_trial = 0 "
            "AND o.expires_at IS NOT NULL AND o.expires_at <= ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM orders o2 "
            "  WHERE o2.user_id = o.user_id AND o2.status = 'paid' "
            "  AND o2.plan_id IS NOT NULL AND o2.is_trial = 0 "
            "  AND o2.expires_at IS NOT NULL AND o2.expires_at > ? AND o2.id != o.id"
            ") "
            "ORDER BY o.expires_at, o.id",
            (now, now),
        ).fetchall()]
    finally:
        conn.close()


def _reminder_plan():
    """Find subscription reminders due ~3 days before expires_at for auto-renewal users.
    Uses a 1-hour dedup window: [now + (days-1)h, now + (days+1)h]."""
    now = services.now_iso()
    reminder_days = services.get_setting_int("reminder_before_days", 3)
    window_start = (services.utcnow() + timedelta(hours=reminder_days * 24 - 1)).isoformat()
    window_end = (services.utcnow() + timedelta(hours=reminder_days * 24 + 1)).isoformat()
    conn = database.get_db()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT o.id, o.user_id, o.plan_name, o.expires_at "
            "FROM orders o "
            "JOIN app_users u ON u.id = o.user_id "
            "WHERE o.status = 'paid' AND u.auto_renewal = 1 "
            "AND o.plan_id IS NOT NULL AND o.is_trial = 0 "
            "AND o.expires_at IS NOT NULL "
            "AND o.expires_at > ? AND o.expires_at <= ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM notifications n "
            "  WHERE n.order_id = o.id AND n.kind = 'reminder'"
            ") "
            "ORDER BY o.expires_at, o.id",
            (window_start, window_end),
        ).fetchall()]
    finally:
        conn.close()


async def run_auto_renewal(dry_run=False):
    """Scan and auto-renew due subscriptions."""
    candidates = _auto_renewal_plan()
    if dry_run:
        for order in candidates:
            print(f"auto-renewal: would renew order {order['id']} user={order['user_id']} "
                  f"plan={order.get('plan_name') or 'unknown'} expires={order.get('expires_at') or '?'}")
        return

    for order in candidates:
        try:
            await services.auto_renew_order(order["id"])
        except services.OrderError as e:
            logger.warning("auto-renewal: order %s failed: %s", order["id"], e)
            # Record failure as a notification (deduped per order)
            conn = database.get_db()
            try:
                existing = conn.execute(
                    "SELECT 1 FROM notifications WHERE order_id = ? AND kind = 'auto_renewal_failed'",
                    (order["id"],)
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO notifications (user_id, order_id, kind, title, message, created_at) "
                        "VALUES (?, ?, 'auto_renewal_failed', ?, ?, ?)",
                        (order["user_id"], order["id"],
                         "Ошибка автопродления",
                         f"Недостаточно средств для автопродления. Пополните баланс.",
                         services.now_iso())
                    )
                    conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.error("auto-renewal: unexpected error for order %s: %s", order["id"], e, exc_info=True)


async def run_reminder_scan(dry_run=False):
    """Scan and send reminders ~3 days before subscription expiry."""
    candidates = _reminder_plan()
    if dry_run:
        for order in candidates:
            print(f"remind: would remind user {order['user_id']} order={order['id']} "
                  f"expires={order.get('expires_at') or '?'}")
        return

    for order in candidates:
        # Dedup check
        conn = database.get_db()
        try:
            existing = conn.execute(
                "SELECT 1 FROM notifications WHERE order_id = ? AND kind = 'reminder'",
                (order["id"],)
            ).fetchone()
            if existing:
                continue

            # Insert web notification (durable channel)
            expires_str = order["expires_at"][:10] if order.get("expires_at") else "?"
            conn.execute(
                "INSERT INTO notifications (user_id, order_id, kind, title, message, created_at) "
                "VALUES (?, ?, 'reminder', ?, ?, ?)",
                (order["user_id"], order["id"],
                 "Скоро истекает подписка",
                 f"Ваша подписка истекает {expires_str}. Пополните баланс или включите автопродление.",
                 services.now_iso())
            )
            conn.commit()
        finally:
            conn.close()

        # Telegram push (best-effort)
        conn = database.get_db()
        try:
            user = conn.execute("SELECT telegram_id FROM app_users WHERE id = ?", (order["user_id"],)).fetchone()
            if user and user["telegram_id"]:
                await bot.send_message_to_user(user["telegram_id"],
                    f"🔔 <b>Скоро истекает подписка</b>\n"
                    f"План: {order.get('plan_name') or 'unknown'}\n"
                    f"До: {expires_str}\n"
                    f"Пополните баланс или включите автопродление.")
        finally:
            conn.close()


def _reconcile_plan():
    cutoff = (services.utcnow() - timedelta(minutes=RECONCILE_AFTER_MINUTES)).isoformat()
    conn = database.get_db()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM orders WHERE "
            "((status = 'pending' AND created_at <= ? AND platega_transaction_id IS NOT NULL "
            "AND platega_transaction_id != '') OR "
            "(status = 'paid' AND plan_id IS NOT NULL AND provisioning_error IS NOT NULL)) "
            "ORDER BY created_at, id",
            (cutoff,),
        ).fetchall()]
    finally:
        conn.close()


async def _reconcile_order(order):
    if order["status"] == "paid":
        await services.fulfill_order_side_effects(order)
        return
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
        services.claim_promo_usage(conn, claimed_order)
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
    elif job_name == "auto-renewal":
        await run_auto_renewal(dry_run=dry_run)
    elif job_name == "remind":
        await run_reminder_scan(dry_run=dry_run)
    elif job_name == "migrate-money":
        migrate_money(verify_only=verify_only, dry_run=dry_run)
    else:
        raise ValueError(f"unknown job: {job_name}")


def run_job(job_name, dry_run=False, verify_only=False):
    migration_db_path = _migration_path() if os.environ.get("SHOP_DB_PATH") else None
    started_at = services.now_iso()
    try:
        if migration_db_path:
            database.DB_PATH = migration_db_path
        if job_name == "migrate-money":
            asyncio.run(
                _run_subcommand(
                    job_name,
                    dry_run,
                    verify_only=verify_only,
                )
            )
        else:
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
    parser.add_argument("job", choices=("expiry", "reconcile", "auto-renewal", "remind", "migrate-money"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    return run_job(args.job, dry_run=args.dry_run, verify_only=args.verify_only)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
