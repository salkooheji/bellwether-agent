"""The agent loop: plan, investigate with tools, conclude.

One call to investigate() handles one finding end to end and always
returns a result, whatever the model or the tools do. The loop owns no
policy of its own: limits live in Guardrails and Budget, tool behaviour
lives in the dispatcher, and evidence discipline lives in the prompts.
Its job is to run the conversation and keep the records.
"""

from __future__ import annotations

import json
import time

from bellwether.agent.guardrails import Budget, Guardrails
from bellwether.agent.prompts import (
    SYSTEM_PROMPT,
    investigate_request,
    plan_request,
    wrap_up_request,
)
from bellwether.agent.tools import TOOL_SCHEMAS, ToolDispatcher

RETRY_WAIT_SECONDS = 15


def _call_llm(client, agent_cfg: dict, messages: list, budget: Budget,
              use_tools: bool):
    """One chat completion, with one retry for transient API failures.

    Returns the response message, or None if the API failed twice or the
    budget ran out. Failed calls still count against the budget, because
    they still hit the provider's rate limits.
    """
    for attempt in (1, 2):
        if not budget.can_call_llm():
            return None
        budget.note_llm_call()
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
            return resp.choices[0].message
        except Exception:
            if attempt == 1:
                time.sleep(RETRY_WAIT_SECONDS)
    return None


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

    # Phase 1: the recorded plan, tools disabled.
    msg = _call_llm(client, agent_cfg, messages, budget, use_tools=False)
    if msg is None:
        memo = _fallback_memo(finding, "the LLM was unreachable at planning")
        return {"finding": finding, "plan": "", "steps_used": 0,
                "stop_reason": "llm_unreachable", "tool_log": tool_log,
                "evidence": evidence, "memo": memo, "raw_final": memo}
    plan = (msg.content or "").strip()
    messages.append({"role": "assistant", "content": plan})
    messages.append({"role": "user", "content": investigate_request()})

    # Phase 2: the investigation loop.
    stop_reason = None
    final_text = None
    while True:
        reason = guard.note_step()
        if reason:
            stop_reason = reason
            break
        msg = _call_llm(client, agent_cfg, messages, budget, use_tools=True)
        if msg is None:
            stop_reason = "the LLM call budget for this run was exhausted"
            break

        if not msg.tool_calls:
            final_text = msg.content or ""
            messages.append({"role": "assistant", "content": final_text})
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
                "content": json.dumps(wrapped),
            })

        if stuck:
            stop_reason = ("the same tool call was repeated multiple times; "
                           "the agent is stuck")
            break

    # Phase 3: conclude.
    if stop_reason != "completed":
        messages.append({"role": "user", "content": wrap_up_request(stop_reason)})
        msg = _call_llm(client, agent_cfg, messages, budget, use_tools=False)
        final_text = (msg.content or "").strip() if msg else None
        if not final_text:
            final_text = _fallback_memo(finding, stop_reason)

    memo = _extract_memo(final_text or "")
    return {
        "finding": finding,
        "plan": plan,
        "steps_used": guard.steps,
        "stop_reason": stop_reason,
        "tool_log": tool_log,
        "evidence": evidence,
        "memo": memo,
        "raw_final": final_text,
    }
