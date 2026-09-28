"""Admin plans update: save works and unchecked checkbox disables (is_active=0).

Regression tests for the /admin/plans row form:
  1. The row form must submit all fields — previously a <form> nested
     directly inside <tr> was ejected from the table by the browser, so
     Save posted without required fields (422, nothing saved).
  2. An unchecked "Активен" checkbox sends no is_active field — the route
     must default it to 0, not 1 (previously the tariff could never be
     switched off).
"""
import pathlib

import app as app_module
import database


def _login_admin(client):
    resp = client.post("/login", data={"username": "admin", "password": "admin"})
    assert resp.status_code in (200, 303), f"admin login failed: {resp.status_code}"


def test_plan_update_unchecked_disables(test_db):
    """POST update without is_active (unchecked box) → 303, DB is_active=0."""
    from fastapi.testclient import TestClient as TC
    client = TC(app_module.app)
    _login_admin(client)

    conn = database.get_db()
    try:
        plan = conn.execute("SELECT * FROM plans LIMIT 1").fetchone()
        assert plan is not None
        plan_id = plan["id"]
        assert plan["is_active"] == 1
    finally:
        conn.close()

    # Mimic exactly what a browser sends when the checkbox is unchecked:
    # all text fields present, is_active absent.
    resp = client.post(
        f"/admin/plans/{plan_id}/update",
        data={
            "name": plan["name"],
            "description": plan["description"] or "",
            "price_rub": str(plan["price_rub"]),
            "duration_days": str(plan["duration_days"]),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"expected redirect, got {resp.status_code}: {resp.text[:200]}"

    conn = database.get_db()
    try:
        row = conn.execute("SELECT is_active FROM plans WHERE id = ?", (plan_id,)).fetchone()
        assert row["is_active"] == 0, "unchecked checkbox must disable the tariff"
    finally:
        conn.close()


def test_plans_template_inputs_bound_to_row_form():
    """No <form> directly inside <tr>; every row input is bound via form= attr."""
    html = pathlib.Path("templates/admin/plans.html").read_text(encoding="utf-8")
    import re
    assert not re.search(r"<tr[^>]*>\s*<form", html), "<form> must not be a direct child of <tr>"
    for name in ("name", "description", "price_rub", "duration_days", "is_active"):
        assert re.search(r'name="%s"[^>]*form="planform-' % name, html) or re.search(
            r'form="planform-[^"]*"[^>]*name="%s"' % name, html
        ), f"input {name} must carry a form= binding"
