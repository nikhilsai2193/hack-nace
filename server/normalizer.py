"""Turns raw Intern events into labelled UI events. Pure display logic: the app never judges or blocks anything,
it only reports what Intern's System 1 (Drex) and harness did. Every normalized event keeps its raw payload."""

from __future__ import annotations

import json
from typing import Any

from models import InternEvent

STUCK_MODES = ("re_investigating", "retrying")
MODE_LABELS = {
    "advancing": "advancing",
    "re_investigating": "re-investigating",
    "retrying": "retrying",
    "off_plan": "off plan",
    "finished": "finished",
    "unclear": "unclear",
}
CONTEXT_WARN_SHARE = 0.85
STREAMING_TYPES = {"text_delta", "reasoning_delta", "state_delta"}


def _answer_value(answer: Any) -> tuple[str | None, float | None, dict[str, float]]:
    """(choice, scalar, probabilities) from Intern's compact answer form."""
    if not isinstance(answer, dict):
        return None, None, {}
    probs = answer.get("probabilities")
    probs = probs if isinstance(probs, dict) else {}
    if "noul" in answer:
        try:
            return None, float(answer["noul"]), {}
        except (TypeError, ValueError):
            return None, None, {}
    if "choice" in answer:
        return str(answer["choice"]), None, {k: float(v) for k, v in probs.items() if isinstance(v, (int, float))}
    if "score" in answer:
        try:
            return None, float(answer["score"]), {}
        except (TypeError, ValueError):
            return None, None, {}
    return None, None, {}


def _pct(x: float | None) -> str:
    return "?" if x is None else f"{x * 100:.0f}%"


def _short(value: Any, n: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= n else text[: n - 1] + "…"


def normalize(raw: dict[str, Any]) -> list[InternEvent]:
    t = raw.get("type")
    ev = lambda kind, layer, title, severity="info", detail=None: InternEvent(  # noqa: E731
        kind=kind, layer=layer, title=title, severity=severity, detail=detail, raw_type=t, raw_payload=raw
    )

    if t == "run_start":
        out = [
            ev("system2_started", "system2", f"System 2 run started · {raw.get('model', '')}",
               detail=f"tools: {', '.join(raw.get('tools') or [])}"),
        ]
        pv = raw.get("protocol_version")
        if pv is not None and pv != 2:
            out.append(ev("session_error", "ui", f"Unsupported Intern RPC protocol version {pv} (app supports 2)",
                          "error"))
        return out

    if t == "turn_start":
        return [ev("system2_thinking", "system2", f"Turn {raw.get('turn')} started", "debug")]

    if t in ("text_delta", "reasoning_delta"):
        return [ev("system2_thinking", "system2", "thinking" if t == "reasoning_delta" else "writing", "debug",
                   detail=raw.get("text"))]

    if t == "message":
        msg = raw.get("message") or {}
        role = msg.get("role")
        if role == "assistant":
            if msg.get("error"):
                return [ev("session_error", "system2", "System 2 reply error", "error", detail=str(msg["error"]))]
            calls = msg.get("tool_calls") or []
            if calls:
                return [ev("system2_output", "system2", f"System 2 asked for {len(calls)} tool call(s)", "debug",
                           detail=", ".join(c.get("name", "?") for c in calls))]
            return [ev("system2_output", "system2", "System 2 replied", "info", detail=_short(msg.get("text", ""), 240))]
        return []  # user/system/tool messages are shown elsewhere

    if t == "tool_call_start":
        return [ev("tool_started", "tool", f"Tool call: {raw.get('name')}", "info",
                   detail=_short(raw.get("arguments", {}), 220))]

    if t == "tool_call_end":
        status = raw.get("status")
        name = raw.get("name")
        out: list[InternEvent] = []
        if status == "ok":
            out.append(ev("tool_completed", "tool", f"Tool ok: {name} ({raw.get('duration_ms', 0)} ms)", "success",
                          detail=_short(raw.get("content", ""), 240)))
            if name in ("decision_files", "index_files"):
                out.append(ev("grounding_check", "system1",
                              "Nace workspace evidence" if name == "decision_files" else "Workspace indexed",
                              "info", detail=_short(raw.get("content", ""), 400)))
        elif status == "refused":
            out.append(ev("tool_failed", "tool", f"Tool refused: {name}", "warning",
                          detail=_short(raw.get("content", ""), 300)))
        else:
            out.append(ev("tool_failed", "tool", f"Tool error: {name}", "error",
                          detail=_short(raw.get("content", ""), 300)))
        return out

    if t == "decision":
        purpose = raw.get("purpose")
        answers = raw.get("answers") or {}
        if raw.get("ok") is False:
            return [ev("system1_decision", "system1", f"System 1 ask failed ({purpose}) — Intern retries it", "warning",
                       detail=raw.get("error"))]
        cached = " (cached)" if raw.get("cached") else ""
        if purpose == "supervise":
            choice, _, probs = _answer_value(answers.get("mode"))
            stuck = sum(probs.get(m, 0.0) for m in STUCK_MODES) if probs else (1.0 if choice in STUCK_MODES else 0.0)
            label = MODE_LABELS.get(choice or "", choice or "?")
            dist = ", ".join(f"{MODE_LABELS.get(k, k)} {_pct(v)}" for k, v in sorted(probs.items(), key=lambda kv: -kv[1]))
            out = [ev("system1_decision", "system1", f"Watching turn {raw.get('turn')}: {label}{cached}", "info",
                      detail=dist or None)]
            if stuck >= 0.5:
                out.append(ev("system1_warning", "system1",
                              f"Drex: P(re-investigating + retrying) = {_pct(stuck)}", "warning", detail=dist))
            return out
        if purpose == "guard":
            choice, _, probs = _answer_value(answers.get("risk"))
            _, asked, _ = _answer_value(answers.get("asked"))
            risk = 1.0 - probs.get("routine", 0.0) if probs else None
            return [ev("system1_decision", "system1",
                       f"Tool call judged: {raw.get('subject') or ''}{cached}".strip(), "info",
                       detail=f"risk={choice or '?'} P(any risk)={_pct(risk)} · P(asked)={_pct(asked)} · "
                              "allowed unless a block follows")]
        if purpose == "wrap_up":
            choice, _, probs = _answer_value(answers.get("wrap"))
            dist = ", ".join(f"{k} {_pct(v)}" for k, v in sorted(probs.items(), key=lambda kv: -kv[1]))
            return [ev("system1_decision", "system1", f"Wrap-up check: {choice or '?'}{cached}", "info",
                       detail=dist or None)]
        if purpose == "keep":
            _, val, _ = _answer_value(next(iter(answers.values()), None) if answers else None)
            return [ev("context_compaction", "system1", f"Compaction keep verdict: {raw.get('subject') or 'read'}",
                       "info", detail=f"P(keep)={_pct(val)}" if val is not None else _short(answers))]
        return [ev("system1_decision", "system1", f"System 1 decision: {purpose}{cached}", "info",
                   detail=_short(answers) if answers else None)]

    if t == "tool_guard":
        if raw.get("layer") == "system1":
            return [ev("system1_blocked", "system1", f"Tool call blocked by Drex: {raw.get('name')}", "error",
                       detail=f"{raw.get('rule')} — {raw.get('reason')} · P(risk)={_pct(raw.get('risk'))} "
                              f"P(asked)={_pct(raw.get('asked'))}")]
        return [ev("system1_blocked", "tool", f"Blocked by guard rule (code): {raw.get('name')}", "error",
                   detail=f"{raw.get('rule')} — {raw.get('reason')}")]

    if t == "nudge":
        reason = raw.get("reason")
        if reason == "stuck_gate":
            return [ev("system1_nudge", "system1", "Loop detected — agent nudged (System 1)", "warning",
                       detail=_short(raw.get("text", ""), 300))]
        if reason == "wrap_up":
            return [ev("system1_nudge", "system1", "Request looks done — agent told to wrap up (System 1)", "info",
                       detail=_short(raw.get("text", ""), 300))]
        return [ev("system1_nudge", "ui", f"Harness nudge: {reason}", "info", detail=_short(raw.get("text", ""), 300))]

    if t == "context_budget":
        tokens, window = raw.get("tokens") or 0, raw.get("working_window") or 0
        if window and tokens / window >= CONTEXT_WARN_SHARE:
            return [ev("context_compaction", "ui", f"Context near the window ({tokens}/{window} tokens, harness)",
                       "warning")]
        return [ev("context_compaction", "ui", f"Context {tokens}/{window} tokens", "debug")]

    if t == "state_writing":
        return [ev("context_compaction", "system2", f"Writing State for compaction (turn {raw.get('turn')})", "info")]
    if t == "state_delta":
        return [ev("context_compaction", "system2", "State streaming", "debug", detail=raw.get("text"))]
    if t == "state":
        landed = raw.get("landed")
        return [ev("context_compaction", "system2",
                   "State written" if landed else f"State write dropped ({raw.get('dropped')})",
                   "info" if landed else "warning")]
    if t == "compaction":
        return [ev("context_compaction", "ui",
                   f"Context compacted ({raw.get('method')}): {raw.get('tokens_before')} → {raw.get('tokens_after')} "
                   f"tokens, {raw.get('kept', 0)} reads kept by System 1", "info")]
    if t == "reduced":
        return [ev("context_compaction", "ui", "Large tool output reduced to a receipt", "debug")]

    if t == "extension":
        return [ev("grounding_check", "system1", f"Nace evidence: {raw.get('extension')} · {raw.get('kind')}", "info",
                   detail=_short(raw.get("data", {}), 400))]
    if t == "indexing":
        state = "finished" if raw.get("finished") else f"{raw.get('done')}/{raw.get('total')}"
        return [ev("grounding_check", "tool", f"Indexing {raw.get('name')}: {state}",
                   "info" if raw.get("finished") else "debug")]
    if t in ("memory_mount", "memory_recall"):
        return [ev("grounding_check", "ui", f"Memory: {t}", "debug")]
    if t == "process":
        p = raw.get("process") or {}
        return [ev("tool_started" if raw.get("action") == "started" else "tool_completed", "tool",
                   f"Process {raw.get('action')}: {_short(p.get('command', ''), 80)}", "debug")]
    if t == "process_check":
        return [ev("system1_decision", "system1", "Drex checked running commands", "info",
                   detail=_short(raw.get("verdicts", []), 300))]
    if t == "hook_error":
        return [ev("session_error", "ui", f"Hook error in {raw.get('extension')}", "warning", detail=raw.get("error"))]
    if t == "secret_unavailable":
        return [ev("session_error", "ui", "Secret unavailable", "warning", detail=raw.get("message"))]

    if t == "run_end":
        reason = raw.get("reason")
        s1 = raw.get("system1") or {}
        if reason == "completed":
            return [ev("system2_output", "system2", "Session completed (run ended)", "success",
                       detail=f"turns={raw.get('turns')} · System 1 asks: {_short(s1, 200)}")]
        if reason == "system1_unavailable":
            return [ev("session_error", "system1", "System 1 (Drex) unavailable — room BLOCKED", "error",
                       detail=raw.get("detail"))]
        if reason == "provider_error":
            return [ev("session_error", "system2", "System 2 (DeepInfra) unavailable", "error", detail=raw.get("detail"))]
        if reason == "stopped_by_guard":
            return [ev("system1_blocked", "system1", "Run stopped by the guard", "error", detail=raw.get("detail"))]
        return [ev("session_error" if reason not in ("aborted", "turn_budget", "waiting") else "system2_output",
                   "system2", f"Run ended: {reason}", "warning", detail=raw.get("detail"))]

    return [ev("system2_output", "ui", f"Intern event: {t}", "debug")]


def malformed(text: str) -> InternEvent:
    return InternEvent(kind="session_error", layer="ui", severity="warning", title="Malformed Intern event",
                       detail=text[:500], raw_type=None, raw_payload=text)
