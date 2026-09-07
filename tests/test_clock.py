"""Golden tests for the canonical tz-aware clock (FOUND-05 / D-08).

One canonical now() = datetime.now(timezone.utc) lives in services.py;
app.py and database.py import it instead of defining their own naive
utcnow()-based helpers.
"""
import pathlib
import re
from datetime import timedelta

import app
import database
import services


def test_now_tz_aware():
    """services.now() must be tz-aware UTC (never naive)."""
    now = services.now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_now_iso_has_utc_suffix():
    """services.now_iso() must render the +00:00 suffix on every call."""
    assert re.search(r"\+00:00$", services.now_iso())


def test_duplicates_removed():
    """app.py and database.py must not define their own clock helpers."""
    app_source = pathlib.Path(app.__file__).read_text(encoding="utf-8")
    database_source = pathlib.Path(database.__file__).read_text(encoding="utf-8")
    assert "def now_iso" not in database_source
    assert "def utcnow" not in app_source


def test_no_utcnow_anywhere():
    """The deprecated naive-clock token must not appear in any top-level module.

    The needle is built from parts so this test file itself never contains
    the contiguous token (the grep gate scans vpn-shop/*.py including tests).
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    needle = "datetime" + ".utcnow"
    offenders = [
        str(path) for path in sorted(root.glob("*.py"))
        if needle in path.read_text(encoding="utf-8")
    ]
    assert offenders == []