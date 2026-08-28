"""Rule-based detection of findings worth investigating.

Detection is deliberately not a language model. Whether a portfolio
changed materially is arithmetic against the database: deterministic,
testable, and free to run. The model enters later, to investigate what
these rules surface. The split is what lets a quiet quarter end quietly,
because arithmetic cannot be tempted into telling a story.

Every threshold comes from config.yaml. Every finding carries the numbers
that triggered it, so any finding can be checked by hand against the
database.
"""

from __future__ import annotations

import sqlite3

from bellwether import db
from bellwether.state import make_fingerprint


def _indexed(snapshot: list[dict]) -> dict[str, dict]:
    return {p["cusip"]: p for p in snapshot}


def _label(pos: dict) -> str:
    """Best human name for a position, falling back to the raw CUSIP."""
    return pos["ticker"] or pos["company_name"] or pos["issuer_name"] or pos["cusip"]


def _top5_weight(snapshot: list[dict]) -> float:
    """Snapshots arrive sorted largest first, so the top five are the head."""
    return sum(p["weight"] for p in snapshot[:5])


def _manager_findings(
    cik: int,
    manager: str,
    cur: list[dict],
    prior: list[dict],
    det: dict,
    period: str,
    prior_period: str,
) -> list[dict]:
    """New positions, full exits, and concentration shifts for one manager."""
    findings = []
    cur_idx = _indexed(cur)
    prior_idx = _indexed(prior)

    for pos in cur:
        if pos["cusip"] in prior_idx:
            continue
        if pos["weight"] < det["new_position_min_weight"]:
            continue
        findings.append(
            {
                "type": "new_position",
                "cik": cik,
                "manager": manager,
                "cusip": pos["cusip"],
                "label": _label(pos),
                "period": period,
                "prior_period": prior_period,
                "metrics": {
                    "value_usd": pos["value_usd"],
                    "shares": pos["shares"],
                    "weight": round(pos["weight"], 4),
                },
                "summary": (
                    f"{manager} opened a new position in {_label(pos)} at "
                    f"{pos['weight']:.1%} of portfolio "
                    f"(${pos['value_usd']:,.0f}) in {period}"
                ),
                "fingerprint": make_fingerprint(
                    "new_position", cik, pos["cusip"], period
                ),
            }
        )

    for pos in prior:
        if pos["cusip"] in cur_idx:
            continue
        if pos["weight"] < det["exit_min_prior_weight"]:
            continue
        findings.append(
            {
                "type": "full_exit",
                "cik": cik,
                "manager": manager,
                "cusip": pos["cusip"],
                "label": _label(pos),
                "period": period,
                "prior_period": prior_period,
                "metrics": {
                    "prior_value_usd": pos["value_usd"],
                    "prior_shares": pos["shares"],
                    "prior_weight": round(pos["weight"], 4),
                },
                "summary": (
                    f"{manager} fully exited {_label(pos)}, which was "
                    f"{pos['weight']:.1%} of portfolio "
                    f"(${pos['value_usd']:,.0f}) in {prior_period}"
                ),
                "fingerprint": make_fingerprint(
                    "full_exit", cik, pos["cusip"], period
                ),
            }
        )

    top5_cur = _top5_weight(cur)
    top5_prior = _top5_weight(prior)
    delta = top5_cur - top5_prior
    if abs(delta) >= det["concentration_top5_delta"]:
        direction = "increased" if delta > 0 else "decreased"
        findings.append(
            {
                "type": "concentration_change",
                "cik": cik,
                "manager": manager,
                "cusip": None,
                "label": "top-5 concentration",
                "period": period,
                "prior_period": prior_period,
                "metrics": {
                    "top5_prior": round(top5_prior, 4),
                    "top5_current": round(top5_cur, 4),
                    "delta": round(delta, 4),
                },
                "summary": (
                    f"{manager} top-5 concentration {direction} from "
                    f"{top5_prior:.1%} to {top5_cur:.1%} between "
                    f"{prior_period} and {period}"
                ),
                "fingerprint": make_fingerprint(
                    "concentration_change", cik, None, period
                ),
            }
        )

    return findings


def _accumulation_findings(
    snapshots: dict[int, tuple[list[dict], list[dict]]],
    managers: dict[int, str],
    det: dict,
    period: str,
    prior_period: str,
) -> list[dict]:
    """The same security accumulated by several managers in one quarter.

    A manager counts as accumulating if the position is new, or if the
    share count grew by at least accumulation_min_shares_increase.
    Shares are compared rather than dollar value because value moves with
    price even when the manager did nothing. Known blind spot: stock
    splits inflate share counts without any buying.
    """
    min_managers = det["accumulation_min_managers"]
    min_increase = det["accumulation_min_shares_increase"]

    buyers: dict[str, list[dict]] = {}
    labels: dict[str, str] = {}

    for cik, (cur, prior) in snapshots.items():
        if not cur or not prior:
            continue
        prior_idx = _indexed(prior)
        for pos in cur:
            prev = prior_idx.get(pos["cusip"])
            if prev is None:
                action = {"cik": cik, "manager": managers[cik], "action": "opened",
                          "shares": pos["shares"], "value_usd": pos["value_usd"]}
            elif (
                pos["shares"]
                and prev["shares"]
                and pos["shares"] >= prev["shares"] * (1 + min_increase)
            ):
                action = {"cik": cik, "manager": managers[cik], "action": "increased",
                          "shares_before": prev["shares"], "shares_after": pos["shares"],
                          "value_usd": pos["value_usd"]}
            else:
                continue
            buyers.setdefault(pos["cusip"], []).append(action)
            labels[pos["cusip"]] = _label(pos)

    findings = []
    for cusip, actions in buyers.items():
        if len(actions) < min_managers:
            continue
        names = ", ".join(a["manager"] for a in actions)
        findings.append(
            {
                "type": "accumulation",
                "cik": None,
                "manager": None,
                "cusip": cusip,
                "label": labels[cusip],
                "period": period,
                "prior_period": prior_period,
                "metrics": {"managers_involved": len(actions), "actions": actions},
                "summary": (
                    f"{len(actions)} managers accumulated {labels[cusip]} "
                    f"in {period}: {names}"
                ),
                "fingerprint": make_fingerprint("accumulation", None, cusip, period),
            }
        )
    return findings


def detect(
    conn: sqlite3.Connection,
    managers: dict[int, str],
    det: dict,
    period: str,
    prior_period: str,
) -> list[dict]:
    """All findings for one quarter transition, in deterministic order.

    A manager missing either quarter's filing contributes nothing, since
    there is no comparison to make. An empty return list is the quiet
    outcome and is a valid, expected result.
    """
    findings = []
    snapshots: dict[int, tuple[list[dict], list[dict]]] = {}

    for cik, manager in managers.items():
        cur = db.portfolio_snapshot(conn, cik, period)
        prior = db.portfolio_snapshot(conn, cik, prior_period)
        snapshots[cik] = (cur, prior)
        if not cur or not prior:
            continue
        findings.extend(
            _manager_findings(cik, manager, cur, prior, det, period, prior_period)
        )

    findings.extend(
        _accumulation_findings(snapshots, managers, det, period, prior_period)
    )

    findings.sort(key=lambda f: (f["type"], f["manager"] or "", f["label"]))
    return findings

def _materiality(f: dict) -> float:
    """How big a finding is, on its own type's natural scale."""
    m = f["metrics"]
    if f["type"] == "new_position":
        return m["weight"]
    if f["type"] == "full_exit":
        return m["prior_weight"]
    if f["type"] == "concentration_change":
        return abs(m["delta"])
    if f["type"] == "accumulation":
        return float(m["managers_involved"])
    return 0.0


def prioritize(findings: list[dict], det: dict, max_findings: int) -> list[dict]:
    """Order findings for investigation and keep the top max_findings.

    Detection flags everything above threshold; the investigation budget
    is finite, so which findings are investigated first must be a written
    rule rather than accidental ordering. Priority is by type, following
    detection.investigation_priority in config (rarest and most
    cross-cutting first), then by materiality within a type. Findings
    beyond the cap remain detected and logged; because only investigated
    findings are marked as reported, the remainder surfaces again on
    later runs.
    """
    order = {t: i for i, t in enumerate(det["investigation_priority"])}
    ranked = sorted(
        findings,
        key=lambda f: (order.get(f["type"], len(order)), -_materiality(f)),
    )
    return ranked[:max_findings]