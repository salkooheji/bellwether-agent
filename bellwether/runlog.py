"""Structured run logging: one JSON file per run.

The log is a deliverable, not a debugging aid. It records when the run
happened, what was examined, every detector decision, every suppressed
finding, each investigation's recorded plan, every tool call with its
arguments and results, guardrail events, memo verification outcomes,
and how the run ended. The Agency and Guardrails sections of the audit
are answered by pointing at this file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunLog:
    def __init__(self, logs_dir: Path, trigger: str):
        self.logs_dir = logs_dir
        self.data = {
            "schema": 1,
            "trigger": trigger,
            "started_at": _now(),
            "finished_at": None,
            "quarter_examined": None,
            "prior_quarter": None,
            "findings_detected": [],
            "findings_selected": [],
            "findings_suppressed": [],
            "investigations": [],
            "events": [],
            "outcome": None,
            "notes": None,
        }

    def event(self, kind: str, detail: str) -> None:
        self.data["events"].append(
            {"at": _now(), "kind": kind, "detail": detail}
        )

    def set_detection(self, period: str, prior: str,
                      detected: list[dict], selected: list[dict]) -> None:
        self.data["quarter_examined"] = period
        self.data["prior_quarter"] = prior
        self.data["findings_detected"] = detected
        self.data["findings_selected"] = [f["fingerprint"] for f in selected]

    def suppressed(self, finding: dict, why: str) -> None:
        self.data["findings_suppressed"].append(
            {"fingerprint": finding["fingerprint"], "why": why}
        )

    def investigation(self, result: dict) -> None:
        self.data["investigations"].append(result)

    def finish(self, outcome: str, notes: str = "") -> None:
        self.data["finished_at"] = _now()
        self.data["outcome"] = outcome
        self.data["notes"] = notes

    def save(self, run_id: int) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.data["started_at"].replace(":", "").replace("-", "")
        path = self.logs_dir / f"run_{run_id:04d}_{stamp}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, default=str)
        return path
