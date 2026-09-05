"""Tests for the amendment policy and the detection rules.

Both are pure functions over data, so they are tested against a small
in-memory database built to contain exactly the cases that matter: a
restated quarter, a new-holdings amendment, sub-account rows for one
CUSIP, and rows that must be excluded from equity analysis.
"""

import sqlite3

import pytest

from bellwether import db, detection

SCHEMA = """
CREATE TABLE filings (
    accession_no TEXT PRIMARY KEY, cik INTEGER, form_type TEXT,
    period_of_report TEXT, filed_date TEXT, amendment_type TEXT,
    parse_status TEXT
);
CREATE TABLE holdings (
    accession_no TEXT, row_index INTEGER, issuer_name TEXT, cusip TEXT,
    value_usd INTEGER, shares INTEGER, share_type TEXT, put_call TEXT
);
CREATE TABLE cusip_map (cusip TEXT PRIMARY KEY, ticker TEXT,
                        company_name TEXT);
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)

    filings = [
        # Q1: original, later restated. Only the restatement should count.
        ("orig-1", 1, "13F-HR", "2024-03-31", "2024-04-10", None, "parsed"),
        ("rest-1", 1, "13F-HR/A", "2024-03-31", "2024-05-01",
         "RESTATEMENT", "parsed"),
        # Q2: original plus a new-holdings amendment. Both should count.
        ("orig-2", 1, "13F-HR", "2024-06-30", "2024-07-10", None, "parsed"),
        ("new-2", 1, "13F-HR/A", "2024-06-30", "2024-08-01",
         "NEW HOLDINGS", "parsed"),
    ]
    c.executemany("INSERT INTO filings VALUES (?,?,?,?,?,?,?)", filings)

    holdings = [
        ("orig-1", 0, "WRONG CO", "AAA", 999, 999, "SH", None),
        ("rest-1", 0, "ALPHA CO", "AAA", 600_000, 6_000, "SH", None),
        ("rest-1", 1, "BETA CO", "BBB", 400_000, 4_000, "SH", None),
        # Two sub-account rows for one CUSIP: must be summed, not counted twice.
        ("orig-2", 0, "ALPHA CO", "AAA", 300_000, 3_000, "SH", None),
        ("orig-2", 1, "ALPHA CO", "AAA", 300_000, 3_000, "SH", None),
        # Excluded rows: option notional and debt principal.
        ("orig-2", 2, "ALPHA CO", "AAA", 5_000_000, 50_000, "SH", "Put"),
        ("orig-2", 3, "GAMMA CO", "CCC", 5_000_000, 50_000, "PRN", None),
        # Added by the amendment, so it must appear in the quarter.
        ("new-2", 0, "DELTA CO", "DDD", 400_000, 4_000, "SH", None),
    ]
    c.executemany("INSERT INTO holdings VALUES (?,?,?,?,?,?,?,?)", holdings)
    c.executemany("INSERT INTO cusip_map VALUES (?,?,?)", [
        ("AAA", "ALPHA", "Alpha Co"), ("BBB", "BETA", "Beta Co"),
        ("DDD", "DELTA", "Delta Co"),
    ])
    c.commit()
    return c


def test_restatement_replaces_the_original(conn):
    assert db.quarter_filings(conn, 1, "2024-03-31") == ["rest-1"]


def test_new_holdings_amendment_is_added_to_the_original(conn):
    assert set(db.quarter_filings(conn, 1, "2024-06-30")) == {"orig-2", "new-2"}


def test_subaccount_rows_are_summed_by_cusip(conn):
    snap = db.portfolio_snapshot(conn, 1, "2024-06-30")
    alpha = next(p for p in snap if p["cusip"] == "AAA")
    assert alpha["value_usd"] == 600_000
    assert alpha["shares"] == 6_000


def test_options_and_debt_rows_are_excluded(conn):
    snap = db.portfolio_snapshot(conn, 1, "2024-06-30")
    assert {p["cusip"] for p in snap} == {"AAA", "DDD"}


def test_weights_sum_to_one(conn):
    snap = db.portfolio_snapshot(conn, 1, "2024-06-30")
    assert sum(p["weight"] for p in snap) == pytest.approx(1.0)


DET = {
    "new_position_min_weight": 0.03,
    "exit_min_prior_weight": 0.02,
    "concentration_top5_delta": 0.05,
    "accumulation_min_managers": 3,
    "accumulation_min_shares_increase": 0.25,
    "investigation_priority": ["accumulation", "concentration_change",
                               "new_position", "full_exit"],
}


def test_detects_new_position_and_full_exit(conn):
    findings = detection.detect(conn, {1: "Test Manager"}, DET,
                                "2024-06-30", "2024-03-31")
    types = {(f["type"], f["cusip"]) for f in findings}
    assert ("new_position", "DDD") in types   # added by the amendment
    assert ("full_exit", "BBB") in types      # held in Q1, gone in Q2


def test_quiet_period_produces_no_findings(conn):
    """Comparing a quarter with itself is the quiet case by construction."""
    findings = detection.detect(conn, {1: "Test Manager"}, DET,
                                "2024-06-30", "2024-06-30")
    assert findings == []


def test_prioritisation_orders_by_type_then_materiality():
    findings = [
        {"type": "full_exit", "manager": "M", "label": "X",
         "metrics": {"prior_weight": 0.10}},
        {"type": "accumulation", "manager": None, "label": "Y",
         "metrics": {"managers_involved": 3}},
        {"type": "new_position", "manager": "M", "label": "Z",
         "metrics": {"weight": 0.05}},
        {"type": "new_position", "manager": "M", "label": "W",
         "metrics": {"weight": 0.20}},
    ]
    ordered = detection.prioritize(findings, DET, max_findings=4)
    assert [f["type"] for f in ordered] == [
        "accumulation", "new_position", "new_position", "full_exit"]
    assert ordered[1]["label"] == "W"  # larger new position first
