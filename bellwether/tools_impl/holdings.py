"""Tools that query the holdings database.

Tool results are fed directly into the model's context, so they are
JSON-serializable dicts, kept small on purpose, and every failure path
returns ok=False with an error message written for the model to reason
about, never an exception.
"""

from __future__ import annotations

import sqlite3

from bellwether import db

MAX_POSITIONS = 12


def get_portfolio(
    conn: sqlite3.Connection,
    managers: dict[int, str],
    cik: int,
    period: str,
    top_n: int = MAX_POSITIONS,
) -> dict:
    """A manager's equity portfolio for one quarter, largest first."""
    try:
        if cik not in managers:
            known = ", ".join(f"{c} ({n})" for c, n in managers.items())
            return {"ok": False,
                    "error": f"unknown manager cik {cik}; known managers: {known}"}
        snap = db.portfolio_snapshot(conn, cik, period)
        if not snap:
            return {"ok": False,
                    "error": f"no parsed filing for {managers[cik]} in {period}; "
                             "check the period is one of the database's quarters"}
        positions = [
            {
                "cusip": p["cusip"],
                "ticker": p["ticker"],
                "name": p["company_name"] or p["issuer_name"],
                "value_usd": p["value_usd"],
                "shares": p["shares"],
                "weight": round(p["weight"], 4),
            }
            for p in snap[:top_n]
        ]
        return {
            "ok": True,
            "manager": managers[cik],
            "period": period,
            "total_positions": len(snap),
            "total_value_usd": sum(p["value_usd"] for p in snap),
            "showing_top": len(positions),
            "positions": positions,
        }
    except Exception as e:
        return {"ok": False, "error": f"portfolio query failed: {e}"}


def get_position_history(
    conn: sqlite3.Connection,
    managers: dict[int, str],
    cik: int,
    cusip: str,
) -> dict:
    """One security's trajectory in one manager's portfolio, all quarters."""
    try:
        if cik not in managers:
            return {"ok": False, "error": f"unknown manager cik {cik}"}
        ident = db.resolve_cusip(conn, cusip)
        history = db.position_history(conn, cik, cusip)
        if not history:
            return {"ok": False,
                    "error": f"no filings found for {managers[cik]} in any quarter"}
        if all(h["value_usd"] == 0 for h in history):
            return {"ok": False,
                    "error": f"{managers[cik]} never held cusip {cusip} in any "
                             "quarter on record; check the cusip is correct"}
        return {
            "ok": True,
            "manager": managers[cik],
            "cusip": cusip,
            "ticker": ident["ticker"],
            "name": ident["company_name"],
            "history": [
                {
                    "period": h["period"],
                    "value_usd": h["value_usd"],
                    "shares": h["shares"],
                    "weight": round(h["weight"], 4),
                }
                for h in history
            ],
        }
    except Exception as e:
        return {"ok": False, "error": f"position history query failed: {e}"}
