"""A fixed, non-agentic pipeline over the same tools and scenarios.

The comparison this supports: what does agency buy? This script runs a
sensible hardcoded sequence for every finding regardless of its type,
then writes a memo from the results with a single LLM call. It uses the
same tools, the same evidence discipline, the same verifier, and the
same grading rubric as the agent.

The sequence is the one a competent engineer would hardcode: identify
the security, pull its history, check prices for the quarter, search for
news, then summarise. What it cannot do is decide that a result changes
the question, which is the difference being measured.

Usage:
  python eval/fixed_pipeline.py --label fixed
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from groq import Groq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellwether import db, detection  # noqa: E402
from bellwether.agent.guardrails import Budget  # noqa: E402
from bellwether.agent.loop import _call_llm, _evidence_digest, _extract_memo  # noqa: E402
from bellwether.agent.prompts import SYSTEM_PROMPT, memo_request  # noqa: E402
from bellwether.agent.tools import ToolDispatcher  # noqa: E402
from bellwether.config import load_config  # noqa: E402
from bellwether.memo import normalize_memo, verify_memo  # noqa: E402
from run_eval import RESULTS_DIR, grade, load_scenarios, resolve_finding  # noqa: E402


def fixed_steps(finding: dict) -> list[tuple[str, dict]]:
    """The same five steps for every finding, whatever its type.

    Manager-level findings use their own cik; cross-manager findings use
    the first manager involved, since a fixed script has to pick one.
    """
    cik = finding["cik"]
    if cik is None:
        actions = finding["metrics"].get("actions") or []
        cik = actions[0]["cik"] if actions else None
    cusip = finding["cusip"]
    period = finding["period"]
    prior = finding["prior_period"]
    label = finding["label"]

    steps: list[tuple[str, dict]] = []
    if cik is not None:
        steps.append(("get_portfolio", {"cik": cik, "period": period}))
        steps.append(("get_portfolio", {"cik": cik, "period": prior}))
        if cusip:
            steps.append(("get_position_history", {"cik": cik, "cusip": cusip}))
    steps.append(("get_price_summary",
                  {"ticker": label, "start": prior, "end": period}))
    steps.append(("search_news",
                  {"query": f"{label} {period[:4]} institutional investor "
                            "position change", "max_results": 5}))
    return steps


def run_fixed(finding: dict, client, dispatcher: ToolDispatcher,
              agent_cfg: dict, budget: Budget) -> dict:
    """Execute the fixed sequence, then write one memo from the results."""
    evidence: dict[str, dict] = {}
    tool_log: list[dict] = []

    for i, (name, args) in enumerate(fixed_steps(finding), start=1):
        result = dispatcher.dispatch(name, json.dumps(args))
        eid = f"E{i}"
        evidence[eid] = result
        tool_log.append({"step": i, "tool": name,
                         "arguments": json.dumps(args), "evidence_id": eid,
                         "ok": result.get("ok"), "repeat": False})

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",
         "content": "A detection pass flagged this finding:\n\n"
                    + json.dumps(finding, indent=2, default=str)},
        {"role": "user", "content": memo_request(_evidence_digest(evidence))},
    ]
    msg = _call_llm(client, agent_cfg, messages, budget, use_tools=False)
    text = (msg.content or "").strip() if msg else ""
    memo = normalize_memo(_extract_memo(text)) if text else (
        f"SUBJECT: {finding['summary']}\n"
        "LIKELY EXPLANATION: The cause could not be established; the memo "
        "step did not complete.\nCONFIDENCE: low")
    ok, problems = verify_memo(memo, evidence)

    return {
        "finding": finding,
        "plan": "fixed sequence, identical for every finding type",
        "steps_used": len(tool_log),
        "stop_reason": "completed",
        "tool_log": tool_log,
        "evidence": evidence,
        "memo": memo,
        "raw_final": text,
        "memo_verified": ok,
        "memo_problems": problems,
        "revision_attempted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed non-agentic pipeline over the scenarios.")
    parser.add_argument("--label", default="fixed")
    parser.add_argument("--trials", type=int, default=2)
    args = parser.parse_args()

    cfg = load_config()
    conn = db.connect(cfg.portfolio_db)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{args.label}.jsonl"
    client = Groq(api_key=cfg.secrets.groq_api_key)

    total = passed = 0
    for trial in range(1, args.trials + 1):
        for scenario in load_scenarios():
            finding = resolve_finding(conn, cfg.managers, cfg.detection,
                                      scenario)
            if finding is None:
                print(f"[{scenario['id']}] SKIPPED: no unique match")
                continue

            dispatcher = ToolDispatcher(conn, cfg.managers,
                                        cfg.secrets.tavily_api_key,
                                        cfg.agent["max_tavily_calls_per_run"])
            budget = Budget(cfg.agent["max_llm_calls_per_run"])
            started = time.time()
            result = run_fixed(finding, client, dispatcher, cfg.agent, budget)
            verdict = grade(scenario, result)

            record = {
                "label": args.label,
                "scenario_id": scenario["id"],
                "truth_source": scenario["truth_source"],
                "trial": trial,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "passed": verdict["passed"],
                "agent_ran": True,
                "reasons": verdict["reasons"],
                "stated_confidence": verdict["stated_confidence"],
                "stop_reason": result["stop_reason"],
                "steps_used": result["steps_used"],
                "memo_verified": result["memo_verified"],
                "revision_attempted": False,
                "llm_calls": budget.llm_calls,
                "tavily_calls": dispatcher.tavily_calls,
                "api_errors": budget.errors,
                "elapsed_seconds": round(time.time() - started, 1),
                "tools_used": [t["tool"] for t in result["tool_log"]],
                "tool_log": result["tool_log"],
                "plan": result["plan"],
                "memo": result["memo"],
                "config_snapshot": {
                    "model": cfg.agent["model"],
                    "temperature": cfg.agent["temperature"],
                    "max_steps_per_investigation": 0,
                    "max_llm_calls_per_run": cfg.agent["max_llm_calls_per_run"],
                },
            }
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")

            total += 1
            passed += 1 if verdict["passed"] else 0
            print(f"  {scenario['id']:32s} trial {trial}: "
                  f"{'PASS' if verdict['passed'] else 'FAIL'}"
                  + (f" | {verdict['reasons'][0][:60]}"
                     if verdict["reasons"] else ""))
            time.sleep(20)

    if total:
        print(f"\nFixed pipeline: {passed}/{total} ({passed/total:.0%}). "
              f"Results: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
