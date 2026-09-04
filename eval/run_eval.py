"""Evaluation harness: run each scenario repeatedly and grade the results.

The agent is non-deterministic, so a single run per scenario is an
anecdote. This harness runs every scenario N times and records each
trial in full, producing rates rather than examples.

Trials call investigate() directly rather than going through
run_agent.py, so the agent's memory is never written to and one trial
cannot suppress the next. Each trial gets a fresh budget so trials do
not degrade each other. Results are appended to JSONL immediately, so a
session interrupted by rate limits can be resumed rather than restarted.

Usage:
  python eval/run_eval.py --trials 5 --label baseline
  python eval/run_eval.py --trials 5 --label higher_budget --resume
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from groq import Groq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellwether import db, detection  # noqa: E402
from bellwether.agent.guardrails import Budget  # noqa: E402
from bellwether.agent.loop import investigate  # noqa: E402
from bellwether.agent.tools import ToolDispatcher  # noqa: E402
from bellwether.config import load_config  # noqa: E402

RESULTS_DIR = ROOT / "eval" / "results"
PAUSE_BETWEEN_TRIALS = 60  # seconds, to stay inside free-tier rate limits


def load_scenarios() -> list[dict]:
    path = ROOT / "eval" / "scenarios.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["scenarios"]


def resolve_finding(conn, managers, det, scenario: dict) -> dict | None:
    """Find the one real finding a scenario refers to."""
    findings = detection.detect(conn, managers, det,
                                scenario["period"], scenario["prior_period"])
    matches = [
        f for f in findings
        if f["type"] == scenario["finding_type"]
        and scenario["finding_label"].lower() in (f["label"] or "").lower()
    ]
    return matches[0] if len(matches) == 1 else None


def _explanation_section(memo: str) -> str:
    """The LIKELY EXPLANATION section, or the whole memo if unlabelled."""
    upper = memo.upper()
    start = upper.find("LIKELY EXPLANATION")
    if start == -1:
        return memo.lower()
    end = upper.find("CONFIDENCE:", start)
    return memo[start:end if end != -1 else len(memo)].lower()


def _stated_confidence(memo: str) -> str:
    upper = memo.upper()
    idx = upper.find("CONFIDENCE:")
    if idx == -1:
        return "none"
    tail = memo[idx + len("CONFIDENCE:"):].strip().lower()
    for level in ("high", "medium", "low"):
        if tail.startswith(level):
            return level
    return "unclear"


def grade(scenario: dict, result: dict) -> dict:
    """Grade one trial against its scenario's ground truth.

    Keyword-based on purpose: grading with a second language model would
    make the measurement depend on another non-deterministic system, and
    a disputed score could not be traced to a cause.
    """
    memo = result["memo"].lower()
    explanation = _explanation_section(result["memo"])
    reasons: list[str] = []

    groups = scenario.get("must_mention") or []
    matched_groups = []
    for group in groups:
        hit = next((term for term in group if term.lower() in memo), None)
        matched_groups.append(hit)
        if hit is None:
            reasons.append(f"missing any of: {group}")

    forbidden = [t for t in (scenario.get("must_not_conclude") or [])
                 if t.lower() in explanation]
    for t in forbidden:
        reasons.append(f"forbidden conclusion present: '{t}'")

    # Conclusiveness. For scenarios whose correct answer is "cause not
    # established", reaching that answer only counts when the agent got
    # there by investigating, not because a guardrail stopped it.
    completed = result["stop_reason"] == "completed"
    if not scenario["expect_conclusive"] and not completed:
        reasons.append("investigation did not complete, so an inconclusive "
                       "memo does not demonstrate honest uncertainty")

    if not result["memo_verified"]:
        reasons.append("memo figures not fully traceable to evidence")

    return {
        "passed": len(reasons) == 0,
        "reasons": reasons,
        "matched_terms": matched_groups,
        "stated_confidence": _stated_confidence(result["memo"]),
        "completed": completed,
    }


def already_done(path: Path) -> set[tuple[str, int]]:
    done = set()
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    # Trials where the agent never ran are infrastructure
                    # failures, not results, so they are re-run rather than
                    # counted. Their records stay in the file as evidence.
                    if rec.get("agent_ran", True):
                        done.add((rec["scenario_id"], rec["trial"]))
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation suite.")
    parser.add_argument("--trials", type=int, default=5,
                        help="trials per scenario")
    parser.add_argument("--label", default="baseline",
                        help="tag for this results file, e.g. baseline")
    parser.add_argument("--resume", action="store_true",
                        help="skip trials already recorded in the file")
    parser.add_argument("--scenario", default=None,
                        help="run only this scenario id")
    args = parser.parse_args()

    cfg = load_config()
    conn = db.connect(cfg.portfolio_db)
    scenarios = load_scenarios()
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] == args.scenario]
        if not scenarios:
            print(f"No scenario with id '{args.scenario}'")
            return 1

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{args.label}.jsonl"
    done = already_done(out_path) if args.resume else set()
    if done:
        print(f"Resuming: {len(done)} trials already recorded.")

    client = Groq(api_key=cfg.secrets.groq_api_key)
    total = passed = 0

    resolved = []
    for scenario in scenarios:
        finding = resolve_finding(conn, cfg.managers, cfg.detection, scenario)
        if finding is None:
            print(f"[{scenario['id']}] SKIPPED: finding did not resolve to "
                  "exactly one match")
            continue
        resolved.append((scenario, finding))

    # Trial-major order: every scenario gets its first trial before any
    # gets its second, so no scenario is systematically run last when
    # the provider's rate window is most depleted.
    for trial in range(1, args.trials + 1):
        for scenario, finding in resolved:
            print(f"\n=== {scenario['id']} trial {trial} "
                  f"({scenario['truth_source']}) ===")
            print(f"    {finding['summary'][:100]}")
            if (scenario["id"], trial) in done:
                print(f"  trial {trial}: already recorded, skipping")
                continue

            dispatcher = ToolDispatcher(conn, cfg.managers,
                                        cfg.secrets.tavily_api_key,
                                        cfg.agent["max_tavily_calls_per_run"])
            budget = Budget(cfg.agent["max_llm_calls_per_run"])
            started = time.time()
            result = investigate(finding, client, dispatcher, cfg.agent, budget)
            elapsed = round(time.time() - started, 1)
            verdict = grade(scenario, result)
            agent_stops = ("completed", "step limit", "stuck", "repeated")
            agent_ran = any(s in result["stop_reason"] for s in agent_stops)
            if budget.provider_unavailable():
                cause = ("the daily token quota is exhausted; it refills on a "
                         "rolling 24 hour window"
                         if budget.daily_quota_exhausted
                         else "the provider refused several calls in a row")
                print(f"\nABORTING: {cause}.\n"
                      "This trial was not recorded. Resume later with:\n"
                      f"  python eval/run_eval.py --trials {args.trials} "
                      f"--label {args.label} --resume")
                if budget.errors:
                    print(f"Last error: {budget.errors[-1][:160]}")
                return 1

            record = {
                "label": args.label,
                "scenario_id": scenario["id"],
                "truth_source": scenario["truth_source"],
                "trial": trial,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "passed": verdict["passed"],
                "agent_ran": agent_ran,
                "reasons": verdict["reasons"],
                "stated_confidence": verdict["stated_confidence"],
                "stop_reason": result["stop_reason"],
                "steps_used": result["steps_used"],
                "memo_verified": result["memo_verified"],
                "revision_attempted": result["revision_attempted"],
                "llm_calls": budget.llm_calls,
                "tavily_calls": dispatcher.tavily_calls,
                "api_errors": budget.errors,
                "elapsed_seconds": elapsed,
                "tools_used": [t["tool"] for t in result["tool_log"]],
                "tool_log": result["tool_log"],
                "plan": result["plan"],
                "memo": result["memo"],
                "config_snapshot": {
                    "model": cfg.agent["model"],
                    "temperature": cfg.agent["temperature"],
                    "max_steps_per_investigation":
                        cfg.agent["max_steps_per_investigation"],
                    "max_llm_calls_per_run":
                        cfg.agent["max_llm_calls_per_run"],
                },
            }
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")

            total += 1
            passed += 1 if verdict["passed"] else 0
            mark = "PASS" if verdict["passed"] else "FAIL"
            print(f"  trial {trial}: {mark} | {result['stop_reason'][:40]} | "
                  f"steps {result['steps_used']} | {elapsed}s"
                  + (f" | {verdict['reasons'][0][:60]}"
                     if verdict["reasons"] else ""))
            time.sleep(PAUSE_BETWEEN_TRIALS)

    if total:
        print(f"\nThis session: {passed}/{total} trials passed "
              f"({passed / total:.0%}). Results: {out_path}")
    else:
        print("\nNo trials run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
