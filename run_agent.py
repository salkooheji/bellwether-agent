"""bellwether-agent entry point.

Runs one complete cycle with no human prompt: check for new data,
detect findings, suppress those already reported, prioritize under the
per-run budget, investigate each with the agent, verify memos, write
them to disk, and record the run in the state database and a JSON log.

Usage:
  python run_agent.py                 normal scheduled behaviour: quiet
                                      unless a new quarter has appeared
  python run_agent.py --force         re-examine the latest quarter even
                                      if already examined (memory still
                                      suppresses reported findings)
  python run_agent.py --quarter Q     examine a specific quarter end,
                                      e.g. --quarter 2023-12-31
  python run_agent.py --trigger T     label the run: manual, scheduled,
                                      or eval (default manual)
"""

from __future__ import annotations

import argparse
import re
import sys

from groq import Groq

from bellwether import db, detection, state
from bellwether.agent.guardrails import Budget
from bellwether.agent.loop import investigate
from bellwether.agent.tools import ToolDispatcher
from bellwether.config import ConfigError, load_config
from bellwether.runlog import RunLog


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bellwether agent once.")
    parser.add_argument("--force", action="store_true",
                        help="re-examine the latest quarter")
    parser.add_argument("--quarter", default=None,
                        help="examine a specific quarter end, e.g. 2023-12-31")
    parser.add_argument("--trigger", default="manual",
                        choices=["manual", "scheduled", "eval"])
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    holdings_conn = db.connect(cfg.portfolio_db)
    state_conn = state.connect(cfg.state_db)
    run_id = state.start_run(state_conn, args.trigger)
    log = RunLog(cfg.logs_dir, args.trigger)
    print(f"Run {run_id} started ({args.trigger}).")

    quarters = db.list_quarters(holdings_conn)
    if len(quarters) < 2:
        log.finish("error", "fewer than two parsed quarters in the database")
        log.save(run_id)
        state.finish_run(state_conn, run_id, "error", None, 0, 0, 0,
                        "fewer than two parsed quarters")
        print("Not enough data to compare quarters.")
        return 1

    # Decide what to examine.
    if args.quarter:
        if args.quarter not in quarters[1:]:
            valid = ", ".join(quarters[1:])
            log.finish("error", f"invalid quarter {args.quarter}")
            log.save(run_id)
            state.finish_run(state_conn, run_id, "error", None, 0, 0, 0,
                            f"invalid quarter {args.quarter}; valid: {valid}")
            print(f"Quarter must be one of: {valid}")
            return 1
        period = args.quarter
    else:
        period = quarters[-1]
        already = state.last_examined_quarter(state_conn)
        if not args.force and already is not None and period <= already:
            log.event("trigger", f"latest quarter {period} already examined "
                                 f"(last examined {already}); nothing new")
            log.finish("quiet", "no new data since last run")
            path = log.save(run_id)
            state.finish_run(state_conn, run_id, "quiet", None, 0, 0, 0,
                            "no new data since last run")
            print(f"Nothing new to examine. Log: {path}")
            return 0
    prior = quarters[quarters.index(period) - 1]
    log.event("trigger", f"examining {period} against {prior}")
    print(f"Examining {period} against {prior}.")

    # Detect, remember, prioritize.
    findings = detection.detect(holdings_conn, cfg.managers, cfg.detection,
                                period, prior)
    fresh = []
    for f in findings:
        if state.is_reported(state_conn, f["fingerprint"]):
            log.suppressed(f, "already reported in a previous run")
        else:
            fresh.append(f)
    selected = detection.prioritize(fresh, cfg.detection,
                                    cfg.agent["max_findings_per_run"])
    log.set_detection(period, prior, findings, selected)
    print(f"Findings: {len(findings)} detected, "
          f"{len(findings) - len(fresh)} already reported, "
          f"{len(selected)} selected for investigation.")

    if not selected:
        note = ("nothing notable happened this quarter" if not findings
                else "all findings were already reported in previous runs")
        log.finish("quiet", note)
        path = log.save(run_id)
        state.finish_run(state_conn, run_id, "quiet", period,
                        len(findings), 0, 0, note)
        print(f"Quiet run: {note}. Log: {path}")
        return 0

    # Investigate.
    client = Groq(api_key=cfg.secrets.groq_api_key)
    dispatcher = ToolDispatcher(holdings_conn, cfg.managers,
                                cfg.secrets.tavily_api_key,
                                cfg.agent["max_tavily_calls_per_run"])
    budget = Budget(cfg.agent["max_llm_calls_per_run"])
    cfg.memos_dir.mkdir(parents=True, exist_ok=True)
    memos_written = 0

    for f in selected:
        print(f"Investigating: {f['summary']}")
        result = investigate(f, client, dispatcher, cfg.agent, budget)
        log.investigation(result)
        state.mark_reported(state_conn, f["fingerprint"], f["type"],
                            f["cik"], f["cusip"], f["period"], run_id)
        memo_path = cfg.memos_dir / (
            f"run{run_id:04d}_{_safe_name(f['fingerprint'])}.md"
        )
        header = (
            f"<!-- run {run_id} | verified: {result['memo_verified']} | "
            f"stop: {result['stop_reason']} -->\n\n"
        )
        with open(memo_path, "w", encoding="utf-8") as fh:
            fh.write(header + result["memo"] + "\n")
        memos_written += 1
        print(f"  -> {result['stop_reason']} | steps {result['steps_used']} | "
              f"verified {result['memo_verified']} | {memo_path.name}")

    log.event("budget", f"llm calls used: {budget.llm_calls} of "
                        f"{cfg.agent['max_llm_calls_per_run']}; tavily calls "
                        f"used: {dispatcher.tavily_calls} of "
                        f"{cfg.agent['max_tavily_calls_per_run']}")
    log.finish("ok", f"{memos_written} memos produced")
    path = log.save(run_id)
    state.finish_run(state_conn, run_id, "ok", period, len(findings),
                    len(selected), memos_written, "")
    print(f"Run {run_id} complete: {memos_written} memos in {cfg.memos_dir}. "
          f"Log: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
