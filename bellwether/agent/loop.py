"""The agent loop: plan, investigate with tools, write the memo.

One call to investigate() handles one finding end to end and always
returns a result, whatever the model or the tools do. The loop owns no
policy of its own: limits live in Guardrails and Budget, tool behaviour
lives in the dispatcher, and evidence discipline lives in the prompts.
Its job is to run the conversation, manage context, and keep records.

Context management matters here for a practical reason: an agent
resends its whole conversation on every turn, so an untrimmed
investigation grows past the provider's per-minute token limit and
starts failing. Older tool results are therefore shortened in the
model's working context while the evidence store keeps them in full,
and the memo is written from a compact digest of that store.
"""

from __future__ import annotations

import json
import re
import time

from bellwether.agent.guardrails import Budget, Guardrails
from bellwether.agent.prompts import (
    SYSTEM_PROMPT,
    investigate_request,
    memo_request,
    memo_revision_request,
    plan_request,
)
from bellwether.agent.tools import TOOL_SCHEMAS, ToolDispatcher
from bellwether.memo import normalize_memo, verify_memo

RETRY_ATTEMPTS = 3
DEFAULT_RETRY_WAIT = 20
MAX_RETRY_WAIT = 90
# The provider allows 8,000 tokens per minute, roughly 133 per second.
# A late agent turn carries several thousand tokens of context, so calls
# must be spaced by tens of seconds rather than sent back to back.
PACE_BETWEEN_CALLS = 20
KEEP_FULL_TOOL_RESULTS = 3
TRIMMED_PREVIEW_CHARS = 400
DIGEST_CHARS_PER_ITEM = 500

_WAIT_RE = re.compile(r"try again in (?:([\d.]+)m)?([\d.]+)s")


def _suggested_wait(message: str) -> float:
    """How long the provider asked us to wait, when it says so."""
    m = _WAIT_RE.search(message)
    if not m:
        return DEFAULT_RETRY_WAIT
    minutes = float(m.group(1) or 0)
    seconds = float(m.group(2))
    return min(minutes * 60 + seconds + 1, MAX_RETRY_WAIT)


def _call_llm(client, agent_cfg: dict, messages: list, budget: Budget,
              use_tools: bool):
    """One chat completion, retrying transient failures.

    Only successful calls are charged to the budget. Oversized requests
    are not retried, since waiting cannot shrink them.
    """
    if not budget.can_call_llm():
        return None
    # The provider's per-minute token limit is small relative to a late
    # agent turn, so calls are paced rather than sent back to back.
    time.sleep(PACE_BETWEEN_CALLS)
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            kwargs = {
                "model": agent_cfg["model"],
                "messages": messages,
                "temperature": agent_cfg["temperature"],
            }
            if use_tools:
                kwargs["tools"] = TOOL_SCHEMAS
                kwargs["tool_choice"] = "auto"
            resp = client.chat.completions.create(**kwargs)
            budget.note_success()
            return resp.choices[0].message
        except Exception as e:
            text = str(e)
            budget.note_failure(f"{type(e).__name__}: {text}")
            if "413" in text or "too large" in text.lower():
                return None
            # A daily token ceiling will not clear within a retry window,
            # so waiting is pointless; fail fast and let the caller stop.
            if "TPD" in text or "tokens per day" in text.lower():
                budget.daily_quota_exhausted = True
                return None
            if attempt < RETRY_ATTEMPTS:
                # 429s carry the provider's own suggested wait; honour it
                # with a margin, since a refused large request means the
                # per-minute token bucket is empty rather than merely low.
                time.sleep(_suggested_wait(text) + 20)
    return None


def _trim_context(messages: list) -> None:
    """Shorten older tool results in place, keeping the recent ones full.

    Tool messages are shortened rather than removed, because every tool
    call in the conversation must keep a matching tool response.
    """
    tool_positions = [i for i, m in enumerate(messages)
                      if m.get("role") == "tool"]
    for i in tool_positions[:-KEEP_FULL_TOOL_RESULTS]:
        content = messages[i]["content"]
        if content.startswith('{"trimmed"') or len(content) <= TRIMMED_PREVIEW_CHARS:
            continue
        messages[i]["content"] = json.dumps({
            "trimmed": True,
            "note": "older tool result, shortened to save context; it was "
                    "seen in full earlier in this investigation",
            "preview": content[:TRIMMED_PREVIEW_CHARS],
        })


def _evidence_digest(evidence: dict[str, dict]) -> str:
    """Compact rendering of everything the tools returned this run."""
    if not evidence:
        return "(no tool results were gathered)"
    lines = []
    for eid, result in evidence.items():
        body = json.dumps(result, default=str)
        if len(body) > DIGEST_CHARS_PER_ITEM:
            body = body[:DIGEST_CHARS_PER_ITEM] + " ...(truncated)"
        lines.append(f"{eid}: {body}")
    return "\n".join(lines)


def _extract_memo(text: str) -> str:
    if "MEMO_START" in text and "MEMO_END" in text:
        return text.split("MEMO_START", 1)[1].split("MEMO_END", 1)[0].strip()
    return text.strip()


def _fallback_memo(finding: dict, reason: str) -> str:
    """Constructed memo for when no LLM budget remains to write one."""
    return (
        f"SUBJECT: {finding['summary']}\n"
        f"WHAT CHANGED: {finding['summary']}\n"
        "LIKELY EXPLANATION: The cause could not be established. The "
        f"investigation stopped before reaching a conclusion ({reason}), "
        "and no explanation is offered rather than an invented one.\n"
        "CONFIDENCE: low, the investigation did not complete"
    )


def investigate(finding: dict, client, dispatcher: ToolDispatcher,
                agent_cfg: dict, budget: Budget) -> dict:
    """Investigate one finding. Always returns a complete result dict."""
    guard = Guardrails(agent_cfg["max_steps_per_investigation"])
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": plan_request(finding)},
    ]
    tool_log: list[dict] = []
    evidence: dict[str, dict] = {}
    sig_to_eid: dict[str, str] = {}

    def _early_exit(stop_reason: str, note: str) -> dict:
        return {"finding": finding, "plan": "", "steps_used": 0,
                "stop_reason": stop_reason, "tool_log": tool_log,
                "evidence": evidence, "memo": _fallback_memo(finding, note),
                "raw_final": None, "memo_verified": False,
                "memo_problems": ["the investigation never ran"],
                "revision_attempted": False}

    # Phase 1: the recorded plan, tools disabled.
    if not budget.can_call_llm():
        return _early_exit("budget_exhausted_before_start",
                           "the LLM call budget for this run was exhausted "
                           "before this investigation began")
    msg = _call_llm(client, agent_cfg, messages, budget, use_tools=False)
    if msg is None:
        return _early_exit("llm_unreachable",
                           "the LLM was unreachable at planning")
    plan = (msg.content or "").strip()
    messages.append({"role": "assistant", "content": plan})
    messages.append({"role": "user", "content": investigate_request()})

    # Phase 2: the investigation loop.
    stop_reason = None
    while True:
        reason = guard.note_step()
        if reason:
            stop_reason = reason
            break
        _trim_context(messages)
        msg = _call_llm(client, agent_cfg, messages, budget, use_tools=True)
        if msg is None:
            stop_reason = (
                "the LLM call budget for this run was exhausted"
                if budget.consecutive_failures == 0
                else "the LLM provider was unavailable, likely rate limited"
            )
            break

        if not msg.tool_calls:
            messages.append({"role": "assistant",
                             "content": msg.content or ""})
            stop_reason = "completed"
            break

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        stuck = False
        for tc in msg.tool_calls:
            name = tc.function.name
            arguments = tc.function.arguments
            sig = Guardrails.signature(name, arguments)
            kind, cached = guard.check_repeat(sig)

            if kind == "fresh":
                result = dispatcher.dispatch(name, arguments)
                guard.remember(sig, result)
                eid = f"E{len(sig_to_eid) + 1}"
                sig_to_eid[sig] = eid
                evidence[eid] = result
                wrapped = dict(result)
            else:
                eid = sig_to_eid.get(sig, "E?")
                wrapped = dict(cached or {})
                wrapped["warning"] = (
                    "this exact call was already made; returning the previous "
                    "result. Do not repeat it again."
                )
                if kind == "stuck":
                    stuck = True
            wrapped["evidence_id"] = eid

            tool_log.append({
                "step": guard.steps,
                "tool": name,
                "arguments": arguments,
                "evidence_id": eid,
                "ok": wrapped.get("ok"),
                "repeat": kind != "fresh",
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(wrapped, default=str),
            })

        if stuck:
            stop_reason = ("the same tool call was repeated multiple times; "
                           "the agent is stuck")
            break

    # Phase 3: write the memo in a fresh, compact conversation.
    # The investigation conversation is large enough to exceed the
    # provider's per-request token ceiling, and the memo only needs the
    # finding, the plan, and the evidence, so it is written from those
    # alone rather than from the full transcript.
    memo_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": plan_request(finding)},
        {"role": "assistant", "content": plan},
        {"role": "user", "content": memo_request(
            _evidence_digest(evidence),
            None if stop_reason == "completed" else stop_reason,
        )},
    ]
    msg = _call_llm(client, agent_cfg, memo_messages, budget, use_tools=False)
    final_text = (msg.content or "").strip() if msg else None
    if not final_text:
        final_text = _fallback_memo(finding, stop_reason)

    memo = normalize_memo(_extract_memo(final_text))
    ok, problems = verify_memo(memo, evidence)
    revision_attempted = False
    if not ok and budget.can_call_llm():
        revision_attempted = True
        memo_messages.append({"role": "assistant", "content": memo})
        memo_messages.append({"role": "user",
                              "content": memo_revision_request(problems)})
        msg = _call_llm(client, agent_cfg, memo_messages, budget,
                        use_tools=False)
        if msg and msg.content:
            candidate = normalize_memo(_extract_memo(msg.content))
            ok2, problems2 = verify_memo(candidate, evidence)
            if ok2 or len(problems2) < len(problems):
                memo, ok, problems = candidate, ok2, problems2

    return {
        "finding": finding,
        "plan": plan,
        "steps_used": guard.steps,
        "stop_reason": stop_reason,
        "tool_log": tool_log,
        "evidence": evidence,
        "memo": memo,
        "raw_final": final_text,
        "memo_verified": ok,
        "memo_problems": problems,
        "revision_attempted": revision_attempted,
    }
