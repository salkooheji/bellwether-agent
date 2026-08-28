"""Read-only access to the portfolio-lens database.

This is the only module in the project that runs SQL against portfolio.db.
It encodes the three rules a consumer of that database must respect:

1. Amendment policy: a quarter is reconstructed from the latest RESTATEMENT
   if one exists, otherwise the original 13F-HR, plus every NEW HOLDINGS
   amendment. Selecting all filings for a manager-quarter double counts.
2. Aggregation: the same CUSIP can appear in several rows of one filing
   (sub-accounts), so positions are summed by CUSIP within a quarter.
3. Equity filters: share_type = 'SH' and put_call IS NULL keeps common
   equity only; value_usd IS NOT NULL drops filings that failed unit
   verification.

The connection is opened in SQLite read-only mode, so this project can
never write to, or corrupt, the source database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    """Open portfolio.db strictly read-only."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_quarters(conn: sqlite3.Connection) -> list[str]:
    """All quarters with at least one fully parsed filing, oldest first."""
    rows = conn.execute(
        "SELECT DISTINCT period_of_report FROM filings "
        "WHERE parse_status = 'parsed' ORDER BY period_of_report"
    ).fetchall()
    return [r["period_of_report"] for r in rows]


def quarter_filings(conn: sqlite3.Connection, cik: int, period: str) -> list[str]:
    """Accession numbers that together represent one manager-quarter.

    Implements the amendment policy. Returns an empty list if the manager
    has no usable filing for the period.
    """
    rows = conn.execute(
        "SELECT accession_no, form_type, amendment_type, filed_date "
        "FROM filings "
        "WHERE cik = ? AND period_of_report = ? AND parse_status = 'parsed' "
        "ORDER BY filed_date",
        (cik, period),
    ).fetchall()

    restatements = [r for r in rows if r["amendment_type"] == "RESTATEMENT"]
    originals = [r for r in rows if r["form_type"] == "13F-HR"]
    new_holdings = [r for r in rows if r["amendment_type"] == "NEW HOLDINGS"]

    if restatements:
        base = restatements[-1]  # latest, thanks to ORDER BY filed_date
    elif originals:
        base = originals[-1]
    else:
        return []

    return [base["accession_no"]] + [r["accession_no"] for r in new_holdings]


def portfolio_snapshot(conn: sqlite3.Connection, cik: int, period: str) -> list[dict]:
    """One manager's equity portfolio for one quarter.

    Returns a list of positions, largest first, each with cusip, names,
    ticker (None when unresolved), value_usd, shares, and weight as a
    fraction of the portfolio's total equity value.
    """
    accessions = quarter_filings(conn, cik, period)
    if not accessions:
        return []

    placeholders = ",".join("?" * len(accessions))
    rows = conn.execute(
        f"""
        SELECT h.cusip,
               MAX(h.issuer_name) AS issuer_name,
               m.ticker            AS ticker,
               m.company_name      AS company_name,
               SUM(h.value_usd)    AS value_usd,
               SUM(h.shares)       AS shares
        FROM holdings h
        LEFT JOIN cusip_map m ON m.cusip = h.cusip
        WHERE h.accession_no IN ({placeholders})
          AND h.share_type = 'SH'
          AND h.put_call IS NULL
          AND h.value_usd IS NOT NULL
        GROUP BY h.cusip
        ORDER BY value_usd DESC
        """,
        accessions,
    ).fetchall()

    total = sum(r["value_usd"] for r in rows)
    positions = []
    for r in rows:
        positions.append(
            {
                "cusip": r["cusip"],
                "issuer_name": r["issuer_name"],
                "ticker": r["ticker"],
                "company_name": r["company_name"],
                "value_usd": r["value_usd"],
                "shares": r["shares"],
                "weight": (r["value_usd"] / total) if total else 0.0,
            }
        )
    return positions


def position_history(conn: sqlite3.Connection, cik: int, cusip: str) -> list[dict]:
    """One security's trajectory in one manager's portfolio across quarters.

    Quarters where the manager filed but did not hold the security are
    included with zero values, so entries and exits are visible.
    """
    history = []
    for period in list_quarters(conn):
        snapshot = portfolio_snapshot(conn, cik, period)
        if not snapshot:
            continue  # manager has no filing this quarter
        match = next((p for p in snapshot if p["cusip"] == cusip), None)
        history.append(
            {
                "period": period,
                "value_usd": match["value_usd"] if match else 0,
                "shares": match["shares"] if match else 0,
                "weight": match["weight"] if match else 0.0,
            }
        )
    return history
