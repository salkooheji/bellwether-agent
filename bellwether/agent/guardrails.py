"""Limits on autonomous behaviour.

Part 5 of the project brief: an autonomous system needs a step limit, a
spend cap, loop detection, and a defined behaviour when it cannot
conclude. This module implements the first three as one stateful object
the agent loop consults every iteration; the fourth is a prompt-level
contract enforced by the loop and the memo layer.

Loop detection: every tool call is fingerprinted by name plus sorted
arguments. A first repeat returns the cached result with a warning, so
the model gets a chance to change course without a wasted API call. A
second repeat of any signature aborts the investigation, because an
agent re-asking answered questions is stuck, not thinking.
"""

from __future__ import annotations

import json


class Budget:
    """Per-run spend counters, shared across investigations in one run."""

    def __init__(self, max_llm_calls: int):
        self.max_llm_calls = max_llm_calls
        self.llm_calls = 0

    def can_call_llm(self) -> bool:
        return self.llm_calls < self.max_llm_calls

    def note_llm_call(self) -> None:
        self.llm_calls += 1


class Guardrails:
    """Per-investigation limits: steps and loop detection."""

    def __init__(self, max_steps: int):
        self.max_steps = max_steps
        self.steps = 0
        self._seen: dict[str, dict] = {}   # signature -> cached result
        self._repeats: dict[str, int] = {}  # signature -> repeat count

    @staticmethod
    def signature(name: str, arguments: str) -> str:
        """Canonical identity of a tool call, stable under key order."""
        try:
            args = json.loads(arguments) if arguments else {}
            canon = json.dumps(args, sort_keys=True)
        except json.JSONDecodeError:
            canon = arguments or ""
        return f"{name}({canon})"

    def note_step(self) -> str | None:
        """Count a loop iteration; a stop reason string means abort."""
        self.steps += 1
        if self.steps > self.max_steps:
            return (f"step limit of {self.max_steps} reached; the "
                    "investigation is taking too many actions")
        return None

    def check_repeat(self, sig: str) -> tuple[str, dict | None]:
        """Classify a tool call before running it.

        Returns ('fresh', None)            first time this exact call
                ('repeat', cached_result)  first repeat: reuse, warn
                ('stuck', cached_result)   second repeat: abort
        """
        if sig not in self._seen:
            return "fresh", None
        self._repeats[sig] = self._repeats.get(sig, 0) + 1
        if self._repeats[sig] >= 2:
            return "stuck", self._seen[sig]
        return "repeat", self._seen[sig]

    def remember(self, sig: str, result: dict) -> None:
        self._seen[sig] = result
