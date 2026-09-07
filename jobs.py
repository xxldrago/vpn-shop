"""Flat CLI entry point for scheduled shop maintenance jobs.

Run from the shop container/repository root with ``python -m jobs``.  The
module deliberately contains no scheduler: systemd invokes these one-shot
commands and records every non-dry-run attempt in ``job_runs``.
"""

import argparse
import asyncio
import logging
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


async def _run_subcommand(job_name, dry_run):
    if job_name == "expiry":
        await run_expiry(dry_run=dry_run)
    elif job_name == "reconcile":
        await run_reconcile(dry_run=dry_run)
    elif job_name == "migrate-money":
        print("reserved — implemented in plan 07")
    else:
        raise ValueError(f"unknown job: {job_name}")


def run_job(job_name, dry_run=False):
    started_at = services.now_iso()
    try:
        asyncio.run(_run_subcommand(job_name, dry_run))
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
    args = parser.parse_args(argv)
    return run_job(args.job, dry_run=args.dry_run)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
