#!/usr/bin/env python3
"""Codex context-insight: read Codex session rollouts and report token/context usage.

Design constraints (user decisions, 2026-11-14):
  * Pure Python, no third-party dependencies, no network.
  * Metadata-only by default. Explicit, per-run user consent is required before
    the optional detailed collector accesses message text, paths or tool data.

Codex writes one JSONL file per session at
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`` (CODEX_HOME defaults to
``~/.codex``). The format is Codex's own and undocumented, so every field is
read defensively: unknown line types are counted and skipped, never fatal.

Two views are kept, because Codex records both:
  * ``token_count.info.total_token_usage``  - cumulative snapshot per request
  * ``token_count.info.last_token_usage``   - per-request delta (drives trends)
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_CODEX_HOME = Path.home() / ".codex"

# Fallback context windows, used only when a rollout never reports
# model_context_window. Codex reports the real value on every token_count.
DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-6.1-sol": 400_000,
    "gpt-6.1-terra": 400_000,
    "gpt-6-astra": 400_000,
    "gpt-6": 400_000,
    "gpt-5.2-codex": 400_000,
    "gpt-5.1-codex": 400_000,
    "gpt-5-codex": 400_000,
    "gpt-5": 400_000,
    "o3": 200_000,
    "o4-mini": 200_000,
}

# USD per 1M tokens: (input, cached input, output).
# Unknown models report no cost rather than a wrong number.
PRICE_TABLE: dict[str, tuple[float, float, float]] = {
    "gpt-6.1-sol": (2.50, 0.25, 10.00),
    "gpt-6.1-terra": (2.50, 0.25, 10.00),
    "gpt-6-astra": (2.50, 0.25, 10.00),
    "gpt-6": (2.50, 0.25, 10.00),
    "gpt-5.2-codex": (1.25, 0.125, 10.00),
    "gpt-5.1-codex": (1.25, 0.125, 10.00),
    "gpt-5-codex": (1.25, 0.125, 10.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "o3": (2.00, 0.50, 8.00),
    "o4-mini": (1.10, 0.275, 4.40),
}

# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Usage:
    """One usage record: either a cumulative snapshot or a per-request delta.

    ``cumulative`` decides how ``input_tokens`` may be interpreted. In a
    cumulative snapshot it is the running sum over every request, so it says
    nothing about window pressure. In a per-request delta the whole window is
    re-sent, so ``input_tokens`` IS the context that request carried.
    """

    timestamp: float | None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    context_window: int | None = None
    context_used: int | None = None
    cumulative: bool = False

    @property
    def uncached_input(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def cache_ratio(self) -> float | None:
        if self.input_tokens <= 0:
            return None
        return self.cached_input_tokens / self.input_tokens

    @property
    def context_size(self) -> int:
        """How much context this record says was in the window.

        Prefers an explicit ``context_tokens`` figure. Otherwise a per-request
        delta's input is used, because every turn re-sends the window. A
        cumulative snapshot has no standalone context figure, so it returns 0
        rather than a misleading number.
        """
        if self.context_used:
            return self.context_used
        if not self.cumulative and self.input_tokens:
            return self.input_tokens
        return 0

    @property
    def usage_ratio(self) -> float | None:
        if not self.context_window:
            return None
        size = self.context_size
        if not size:
            return None
        return min(1.0, size / self.context_window)

    @property
    def visible_output(self) -> int:
        """Output that is not internal reasoning."""
        return max(0, self.output_tokens - self.reasoning_tokens)


@dataclass
class ToolCall:
    """A tool invocation. Name only - arguments are never stored."""

    name: str
    timestamp: float | None
    kind: str = "function"  # function | custom_tool | local_shell


@dataclass
class TurnTiming:
    """One turn: task_started -> task_complete.

    Codex records ``started_at``/``completed_at`` in epoch seconds on those two
    events, and also stamps ``duration_ms`` on the completion. The line
    ``timestamp`` is a fallback for older builds that omit the explicit fields.
    """

    started_at: float | None = None
    completed_at: float | None = None
    duration_ms: int = 0
    time_to_first_token_ms: int | None = None

    @property
    def duration(self) -> float | None:
        if self.duration_ms:
            return self.duration_ms / 1000.0
        if self.started_at is not None and self.completed_at is not None and self.completed_at >= self.started_at:
            return self.completed_at - self.started_at
        return None


@dataclass
class ToolTiming:
    """One tool execution: the call line -> its output line."""

    name: str
    started_at: float | None = None
    completed_at: float | None = None
    started_at_ms: int = 0
    completed_at_ms: int = 0

    @property
    def duration(self) -> float | None:
        if self.started_at_ms and self.completed_at_ms:
            return ((self.completed_at_ms - self.started_at_ms) / 1000.0) if self.completed_at_ms >= self.started_at_ms else None
        if self.started_at is not None and self.completed_at is not None and self.completed_at >= self.started_at:
            return self.completed_at - self.started_at
        return None


@dataclass
class TimingStats:
    """Aggregated timing, mirroring dsh-context's timing card.

    Active time is split three ways:
      * model  - turn time not spent inside a tool call
      * tools  - summed tool execution time
      * gap    - wall time between turns that belongs to neither
    """

    turns: list[TurnTiming] = field(default_factory=list)
    tools: list[ToolTiming] = field(default_factory=list)

    @property
    def turn_total(self) -> float:
        return sum(t.duration or 0.0 for t in self.turns)

    @property
    def tool_total(self) -> float:
        return sum(t.duration or 0.0 for t in self.tools)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def timed_turn_count(self) -> int:
        return sum(1 for t in self.turns if t.duration is not None)

    @property
    def timed_tool_count(self) -> int:
        return sum(1 for t in self.tools if t.duration is not None)

    @property
    def avg_turn(self) -> float | None:
        values = [t.duration for t in self.turns if t.duration is not None]
        return sum(values) / len(values) if values else None

    @property
    def avg_tool(self) -> float | None:
        values = [t.duration for t in self.tools if t.duration is not None]
        return sum(values) / len(values) if values else None

    def tool_breakdown(self) -> dict[str, dict[str, float]]:
        """Per-tool-name total and average execution time."""
        acc: dict[str, dict[str, float]] = {}
        for tool in self.tools:
            d = tool.duration
            if d is None:
                continue
            row = acc.setdefault(tool.name, {"total": 0.0, "count": 0.0})
            row["total"] += d
            row["count"] += 1
        for row in acc.values():
            row["avg"] = row["total"] / row["count"] if row["count"] else 0.0
        return dict(sorted(acc.items(), key=lambda kv: -kv[1]["total"]))


@dataclass
class ItemStats:
    """Conversation item counts, by kind. No content is retained."""

    user_messages: int = 0
    assistant_messages: int = 0
    reasoning_items: int = 0
    tool_calls: int = 0
    tool_outputs: int = 0
    other: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "user_messages": self.user_messages,
            "assistant_messages": self.assistant_messages,
            "reasoning_items": self.reasoning_items,
            "tool_calls": self.tool_calls,
            "tool_outputs": self.tool_outputs,
        }


@dataclass
class Session:
    """Everything the report needs about one Codex session."""

    path: Path
    session_id: str | None = None
    cwd: str | None = None
    cli_version: str | None = None
    model_provider: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    models: dict[str, int] = field(default_factory=dict)
    effort: str | None = None
    approx_input_tokens: int = 0

    # cumulative snapshots (one per request) and per-request deltas
    snapshots: list[Usage] = field(default_factory=list)
    deltas: list[Usage] = field(default_factory=list)

    tool_calls: list[ToolCall] = field(default_factory=list)
    items: ItemStats = field(default_factory=ItemStats)
    timing: TimingStats = field(default_factory=TimingStats)

    turn_count: int = 0
    user_prompt_count: int = 0
    compaction_count: int = 0
    rate_limit_events: list[dict[str, Any]] = field(default_factory=list)
    unknown_lines: dict[str, int] = field(default_factory=dict)
    # None in the default privacy mode; populated only after explicit consent.
    details: dict[str, Any] | None = None

    # ---- derived -------------------------------------------------------
    @property
    def primary_model(self) -> str | None:
        if not self.models:
            return None
        return max(self.models.items(), key=lambda kv: kv[1])[0]

    @property
    def context_window(self) -> int | None:
        for u in reversed(self.snapshots):
            if u.context_window:
                return u.context_window
        for u in reversed(self.deltas):
            if u.context_window:
                return u.context_window
        model = self.primary_model
        if model and model in DEFAULT_CONTEXT_WINDOWS:
            return DEFAULT_CONTEXT_WINDOWS[model]
        return None

    @property
    def latest(self) -> Usage | None:
        """Most recent *context* reading, i.e. the last per-request usage.

        Deliberately not the cumulative snapshot: for window pressure the
        useful figure is what the last request carried, not the running total.
        """
        return self.deltas[-1] if self.deltas else None

    @property
    def totals(self) -> dict[str, int]:
        """Cumulative usage.

        Codex's total_token_usage is already cumulative, so the last snapshot
        wins rather than a sum. When only deltas exist, sum those instead.
        """
        if self.snapshots:
            last = self.snapshots[-1]
            return {
                "input": last.input_tokens,
                "cached": last.cached_input_tokens,
                "output": last.output_tokens,
                "reasoning": last.reasoning_tokens,
                "total": last.total_tokens,
            }
        if self.deltas:
            return {
                "input": sum(d.input_tokens for d in self.deltas),
                "cached": sum(d.cached_input_tokens for d in self.deltas),
                "output": sum(d.output_tokens for d in self.deltas),
                "reasoning": sum(d.reasoning_tokens for d in self.deltas),
                "total": sum(d.total_tokens for d in self.deltas),
            }
        return {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0}

    @property
    def peak_context(self) -> int:
        """Largest context carried in a single request."""
        values = [u.context_size for u in self.deltas if u.context_size]
        if not values:
            values = [u.context_size for u in self.snapshots if u.context_size]
        return max(values) if values else 0

    @property
    def min_context(self) -> int:
        values = [u.context_size for u in self.deltas if u.context_size]
        if not values:
            values = [u.context_size for u in self.snapshots if u.context_size]
        return min(values) if values else 0

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return max(0.0, self.ended_at - self.started_at)

    def cost(self) -> float | None:
        """Estimated USD, or None when the model has no known price."""
        model = self.primary_model
        if not model or model not in PRICE_TABLE:
            return None
        in_price, cached_price, out_price = PRICE_TABLE[model]
        t = self.totals
        return (
            (t["input"] - t["cached"]) / 1_000_000 * in_price
            + t["cached"] / 1_000_000 * cached_price
            + t["output"] / 1_000_000 * out_price
        )

    def tool_histogram(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for call in self.tool_calls:
            hist[call.name] = hist.get(call.name, 0) + 1
        return dict(sorted(hist.items(), key=lambda kv: -kv[1]))

    def context_series(self) -> list[tuple[float | None, int, int | None]]:
        """Per-request (timestamp, context_used, context_window) for trends.

        Codex does not always record ``context_tokens``. When it does not, the
        context actually sent with request N is the input of that request: the
        whole window is re-sent each turn. So ``last_token_usage.input_tokens``
        is the fallback, and it is the figure dsh-context would call "current
        context". Snapshots are tried first because they are authoritative.
        """
        out: list[tuple[float | None, int, int | None]] = []
        for u in self.deltas:
            size = u.context_size
            if size:
                out.append((u.timestamp, size, u.context_window))
        if out:
            return out

        # No deltas recorded: fall back to snapshots that report a context size.
        for u in self.snapshots:
            size = u.context_size
            if size:
                out.append((u.timestamp, size, u.context_window))
        return out

    def token_series(self) -> list[tuple[float | None, Usage]]:
        """Per-request deltas, for the growth chart."""
        return [(d.timestamp, d) for d in self.deltas]

    def context_changes(self) -> list[dict[str, Any]]:
        """Difference between successive recorded context readings, not causal attribution."""
        previous = None
        rows = []
        for timestamp, used, window in self.context_series():
            rows.append({"timestamp": timestamp, "used": used, "window": window,
                         "change": None if previous is None else used - previous})
            previous = used
        return rows


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def codex_home(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else DEFAULT_CODEX_HOME


def find_rollouts(home: Path | None = None) -> list[Path]:
    """Return rollout JSONL files, newest first."""
    root = (home or codex_home()) / "sessions"
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("rollout-*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def session_id_from_path(path: Path) -> str | None:
    """rollout-2026-06-09T13-30-58-019eaadc-....jsonl -> uuid."""
    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        path.name,
    )
    return match.group(0) if match else None


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else 0
    if isinstance(value, str):
        try:
            number = float(value)
            return int(number) if math.isfinite(number) else 0
        except (ValueError, OverflowError):
            return 0
    return 0


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            return number if math.isfinite(number) and abs(number) < 253402300000 else None
        except OverflowError:
            return None
    if isinstance(value, str):
        try:
            number = float(value)
            return number if math.isfinite(number) and abs(number) < 253402300000 else None
        except (ValueError, OverflowError):
            pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _payload_time(payload: dict[str, Any], *names: str) -> float | None:
    """Read an explicit epoch timestamp from a payload.

    Codex mixes units: ``task_*`` fields are epoch seconds, ``item_completed``
    fields are epoch milliseconds. Callers say which name means what, so this
    stays a pure reader with no unit guessing.
    """
    for name in names:
        if name in payload:
            value = _as_float(payload[name])
            if value is not None:
                return value
    return None


def _payload_time_ms(payload: dict[str, Any], *names: str) -> int:
    """Read an explicit epoch-millisecond field."""
    for name in names:
        if name in payload:
            value = _as_int(payload[name])
            if value:
                return value
    return 0


def _line_time(obj: dict[str, Any]) -> float | None:
    for key in ("timestamp", "time", "ts"):
        if key in obj:
            parsed = _as_float(obj[key])
            if parsed is not None:
                return parsed
    return None


def _usage_from(mapping: dict[str, Any]) -> dict[str, int]:
    """Extract token figures from one usage object with defensive aliases."""

    def num(*names: str) -> int:
        for name in names:
            if name in mapping:
                return _as_int(mapping[name])
        return 0

    input_tokens = num("input_tokens", "input")
    cached = num("cached_input_tokens", "cached_tokens", "cached")
    output = num("output_tokens", "output")
    reasoning = num("reasoning_tokens", "reasoning_output_tokens", "reasoning")
    total = num("total_tokens", "total")
    if total == 0:
        total = input_tokens + output
    return {
        "input": input_tokens,
        "cached": cached,
        "output": output,
        "reasoning": reasoning,
        "total": total,
    }


def _parse_token_count(payload: dict[str, Any], when: float | None) -> tuple[Usage | None, Usage | None]:
    """Return (cumulative snapshot, per-request delta) from a token_count event."""
    info = payload.get("info")
    if not isinstance(info, dict):
        info = payload

    window = _as_int(info.get("model_context_window")) or None
    context_used = _as_int(
        info.get("context_tokens") or info.get("current_context_tokens") or 0
    ) or None

    snapshot: Usage | None = None
    delta: Usage | None = None

    total_obj = info.get("total_token_usage")
    if isinstance(total_obj, dict):
        figures = _usage_from(total_obj)
        if figures["total"] or figures["input"]:
            snapshot = Usage(
                timestamp=when,
                input_tokens=figures["input"],
                cached_input_tokens=figures["cached"],
                output_tokens=figures["output"],
                reasoning_tokens=figures["reasoning"],
                total_tokens=figures["total"],
                context_window=window,
                context_used=context_used,
                cumulative=True,
            )

    last_obj = info.get("last_token_usage")
    if isinstance(last_obj, dict):
        figures = _usage_from(last_obj)
        if figures["total"] or figures["input"]:
            delta = Usage(
                timestamp=when,
                input_tokens=figures["input"],
                cached_input_tokens=figures["cached"],
                output_tokens=figures["output"],
                reasoning_tokens=figures["reasoning"],
                total_tokens=figures["total"],
                context_window=window,
                context_used=context_used,
                cumulative=False,
            )

    # A payload with no nested usage objects may itself be the usage record.
    if snapshot is None and delta is None:
        figures = _usage_from(info)
        if figures["total"] or figures["input"]:
            snapshot = Usage(
                timestamp=when,
                input_tokens=figures["input"],
                cached_input_tokens=figures["cached"],
                output_tokens=figures["output"],
                reasoning_tokens=figures["reasoning"],
                total_tokens=figures["total"],
                context_window=window,
                context_used=context_used,
                cumulative=True,
            )

    return snapshot, delta


TOOL_CALL_TYPES = {
    "function_call": "function",
    "custom_tool_call": "custom_tool",
    "local_shell_call": "local_shell",
    "web_search_call": "web_search",
}

TOOL_OUTPUT_TYPES = {
    "function_call_output",
    "custom_tool_call_output",
    "local_shell_call_output",
}


def _content_block_types(payload: dict[str, Any]) -> list[str]:
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    kinds = []
    for block in content:
        if isinstance(block, dict):
            kind = block.get("type")
            if isinstance(kind, str):
                kinds.append(kind)
    return kinds


def _item_kind(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Classify a response_item. Returns (bucket, tool_name)."""
    ptype = payload.get("type")
    if not isinstance(ptype, str):
        return "other", None

    if ptype in TOOL_CALL_TYPES:
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            name = ptype.replace("_call", "")
        return "tool_call", name

    if ptype in TOOL_OUTPUT_TYPES:
        return "tool_output", None

    if ptype == "reasoning":
        return "reasoning", None

    if ptype == "message":
        role = payload.get("role")
        kinds = _content_block_types(payload)
        if role == "user":
            return "user_message", None
        if role == "assistant":
            # assistant text carries output_text blocks in modern Codex
            if not kinds or "output_text" in kinds or "text" in kinds:
                return "assistant_message", None
            return "other", None
        if role in ("developer", "system"):
            return "other", None
        return "other", None

    return "other", None


# --------------------------------------------------------------------------
# Optional detailed collection. This is never instantiated in metadata mode.
# It is intentionally conservative: records are observations, not a rebuilt
# model context or proof that an agent/process is still running.
# --------------------------------------------------------------------------

_DETAIL_ITEMS = 1000
_DETAIL_CHARS = 4000


def _detail_text(value: Any) -> str:
    if isinstance(value, str):
        text = value
    elif value is None:
        return ""
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return ""
    return text[:_DETAIL_CHARS] + ("\n…[内容已截断]" if len(text) > _DETAIL_CHARS else "")


class DetailCollector:
    """Collect bounded evidence from explicitly authorized local rollouts."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {
            "records": [], "commands": [], "files": [], "events": [],
            "collaboration": [], "parent_thread_id": None, "truncated": False,
        }
        self._legacy_commands: list[dict[str, Any]] = []
        self._open_commands: dict[str, dict[str, Any]] = {}
        self._step = 0
        self._turn = 0

    def _add(self, bucket: str, row: dict[str, Any]) -> None:
        rows = self.data[bucket]
        if len(rows) >= _DETAIL_ITEMS:
            self.data["truncated"] = True
            return
        row.update({"step": self._step, "turn": self._turn})
        rows.append(row)

    def accept(self, kind: Any, payload: dict[str, Any], when: float | None) -> None:
        ptype = payload.get("type")
        if kind == "session_meta":
            parent = payload.get("parent_thread_id")
            if isinstance(parent, str):
                self.data["parent_thread_id"] = _detail_text(parent)
        if ptype in ("turn_started", "task_started"):
            self._turn += 1
        if ptype == "token_count" or kind == "token_usage_record":
            self._step += 1
        if kind == "compacted" or ptype in ("context_compacted", "compaction", "compact"):
            self._add("events", {"time": when, "kind": str(kind if kind == "compacted" else ptype)})
        if kind == "inter_agent_communication":
            self._add("collaboration", {
                "time": when, "kind": "message", "sender": _detail_text(payload.get("author")),
                "recipients": _detail_text([payload.get("recipient")] + (payload.get("other_recipients") if isinstance(payload.get("other_recipients"), list) else [])),
                "content": _detail_text(payload.get("content")), "status": "已记录，非实时状态",
            })
        if kind == "response_item":
            self._response(payload, when)
        if ptype == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict):
                self._completed(item, payload, when)

    def _response(self, payload: dict[str, Any], when: float | None) -> None:
        kind = payload.get("type")
        if not isinstance(kind, str):
            return
        if kind == "message" or kind == "agent_message":
            role = payload.get("role") if kind == "message" else "assistant"
            content = payload.get("content")
            parts = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
            self._add("records", {"time": when, "kind": _detail_text(role or "message"), "content": _detail_text("\n".join(parts))})
        elif kind in TOOL_CALL_TYPES:
            name = payload.get("name") or kind
            arg = payload.get("arguments") if kind == "function_call" else payload.get("input")
            if kind == "local_shell_call":
                arg = payload.get("action")
            self._add("records", {"time": when, "kind": "工具调用 · " + _detail_text(name), "content": _detail_text(arg)})
            if name in ("shell", "exec_command", "shell_command", "local_shell") or kind == "local_shell_call":
                command = {"time": when, "command": _detail_text(arg), "status": "未知", "exit_code": None,
                           "duration": None, "output": "", "source": "response_item"}
                if len(self._legacy_commands) < _DETAIL_ITEMS:
                    self._legacy_commands.append(command)
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str):
                        self._open_commands[call_id] = command
                else:
                    self.data["truncated"] = True
            if name == "apply_patch":
                patch = payload.get("input")
                if isinstance(patch, str):
                    for match in re.finditer(r"^\*\*\* (Add|Update|Delete) File: (.+)$", patch, re.MULTILINE):
                        self._add("files", {"time": when, "kind": "write", "path": _detail_text(match.group(2)), "source": "apply_patch 调用（未证明成功）"})
        elif kind in TOOL_OUTPUT_TYPES:
            output = payload.get("output")
            self._add("records", {"time": when, "kind": "工具结果", "content": _detail_text(output)})
            call_id = payload.get("call_id")
            cmd = self._open_commands.pop(call_id, None) if isinstance(call_id, str) else None
            if cmd is not None:
                cmd["output"] = _detail_text(output)
                # Raw tool output is not a reliable exit status.

    def _completed(self, item: dict[str, Any], envelope: dict[str, Any], when: float | None) -> None:
        kind = item.get("type")
        if kind == "CommandExecution":
            start_ms = _payload_time_ms(envelope, "started_at_ms")
            end_ms = _payload_time_ms(envelope, "completed_at_ms")
            duration = (end_ms - start_ms) / 1000 if start_ms and end_ms >= start_ms else None
            exit_code = item.get("exit_code")
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                exit_code = None
            raw_command = item.get("command")
            program = raw_command[0] if isinstance(raw_command, list) and raw_command and isinstance(raw_command[0], str) else None
            self._add("commands", {"time": when, "command": _detail_text(raw_command), "program": _detail_text(program),
                "status": _detail_text(item.get("status")) or "未知", "exit_code": exit_code,
                "duration": duration, "output": _detail_text(item.get("aggregated_output")),
                "source": "item_completed.CommandExecution"})
            parsed = item.get("parsed_cmd")
            if isinstance(parsed, list):
                for part in parsed:
                    if isinstance(part, dict) and part.get("type") in ("read", "search", "list_files") and isinstance(part.get("path"), str):
                        self._add("files", {"time": when, "kind": "read" if part["type"] == "read" else "search", "path": _detail_text(part["path"]), "source": "parsed_cmd（尽力识别）"})
        elif kind == "FileChange":
            changes = item.get("changes")
            if isinstance(changes, dict):
                for path in changes:
                    if isinstance(path, str):
                        self._add("files", {"time": when, "kind": "write", "path": _detail_text(path), "source": "item_completed.FileChange · " + _detail_text(item.get("status") or "状态未知")})
        elif kind == "ImageView" and isinstance(item.get("path"), str):
            self._add("files", {"time": when, "kind": "image", "path": _detail_text(item["path"]), "source": "item_completed.ImageView"})
        elif kind == "ContextCompaction":
            self._add("events", {"time": when, "kind": "item_completed.ContextCompaction"})
        elif kind == "CollabAgentToolCall":
            self._add("collaboration", {"time": when, "kind": _detail_text(item.get("tool")),
                "sender": _detail_text(item.get("sender_thread_id")),
                "recipients": _detail_text(item.get("receiver_thread_ids")),
                "content": _detail_text(item.get("prompt")), "status": _detail_text(item.get("status"))})
        elif kind == "SubAgentActivity":
            self._add("collaboration", {"time": when, "kind": _detail_text(item.get("kind")),
                "sender": "", "recipients": _detail_text(item.get("agent_thread_id")),
                "content": "", "status": "历史事件，非实时状态"})

    def finish(self) -> dict[str, Any]:
        # Avoid double-counting raw response calls when richer command items exist.
        # Mixed old/new sessions may consequently omit legacy-only calls.
        if not self.data["commands"]:
            self.data["commands"] = self._legacy_commands
        for row in self.data["commands"]:
            tags = []
            status = str(row.get('status') or '').lower()
            code = row.get('exit_code')
            duration = row.get('duration')
            if status == 'failed' or (isinstance(code, int) and not isinstance(code, bool) and code != 0):
                tags.append('failed')
            if isinstance(duration, (int, float)) and duration >= 10:
                tags.append('slow')
            if status in ('in_progress', 'inprogress'):
                tags.append('background')
            row['tags'] = tags
        return self.data


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_rollout(path: Path, *, max_lines: int | None = None, detailed: bool = False) -> Session:
    """Parse a rollout; detailed access is an explicit caller responsibility."""
    session = Session(path=path, session_id=session_id_from_path(path))
    collector = DetailCollector() if detailed else None
    first_time: float | None = None
    last_time: float | None = None

    # Timing state: one open turn, and tool calls awaiting their output line.
    open_turn: TurnTiming | None = None
    open_tools: dict[str, ToolTiming] = {}
    anonymous_tools: list[ToolTiming] = []
    record_snapshots: list[Usage] = []
    record_deltas: list[Usage] = []
    compacted_records = 0
    compacted_items = 0

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return session

    with handle:
        for index, raw in enumerate(handle):
            if max_lines is not None and index >= max_lines:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                session.unknown_lines["malformed"] = (
                    session.unknown_lines.get("malformed", 0) + 1
                )
                continue
            if not isinstance(obj, dict):
                continue

            when = _line_time(obj)
            if when is not None:
                if first_time is None or when < first_time:
                    first_time = when
                if last_time is None or when > last_time:
                    last_time = when

            top_type = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                payload = obj
            if collector is not None:
                collector.accept(top_type, payload, when)

            # --- session metadata ------------------------------------
            if top_type in ("session_meta", "session_start", "session_configured"):
                sid = payload.get("id") or payload.get("session_id")
                if isinstance(sid, str) and sid:
                    session.session_id = sid
                if detailed:
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        session.cwd = cwd
                version = payload.get("cli_version")
                if isinstance(version, str) and version:
                    session.cli_version = version
                provider = payload.get("model_provider")
                if isinstance(provider, str) and provider:
                    session.model_provider = provider
                created_at = _as_float(payload.get("timestamp"))
                if created_at is not None:
                    session.started_at = created_at
                continue

            if top_type == "turn_context":
                if detailed:
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        session.cwd = cwd
                model = payload.get("model")
                if isinstance(model, str) and model:
                    session.models[model] = session.models.get(model, 0) + 1
                effort = payload.get("effort") or payload.get("reasoning_effort")
                if isinstance(effort, str) and effort:
                    session.effort = effort
                continue

            payload_type = payload.get("type")

            # --- token accounting ------------------------------------
            if top_type == "token_count" or payload_type == "token_count":
                snapshot, delta = _parse_token_count(payload, when)
                if snapshot is not None:
                    session.snapshots.append(snapshot)
                if delta is not None:
                    session.deltas.append(delta)
                # Do not retain the unbounded rate_limits object: providers may
                # add fields that are not metadata or are sensitive.
                continue

            if top_type == "token_usage_record":
                # Newer canonical records carry usage/thread_token_usage. They
                # overlap token_count events, so use them only as a fallback.
                owner = payload.get("thread_id")
                if isinstance(owner, str) and session.session_id and owner != session.session_id:
                    continue
                info = {"total_token_usage": payload.get("thread_token_usage"),
                        "last_token_usage": payload.get("usage")}
                snapshot, delta = _parse_token_count({"info": info}, when)
                if snapshot is not None:
                    record_snapshots.append(snapshot)
                if delta is not None:
                    record_deltas.append(delta)
                continue

            # A compacted marker and a legacy event may describe the same
            # compaction. Count each representation separately, then use the
            # largest observed count as a conservative lower bound.
            if top_type == "compacted":
                compacted_records += 1
                continue

            # --- turn boundaries --------------------------------------
            if payload_type in ("task_started", "turn_started"):
                session.turn_count += 1
                started = _payload_time(payload, "started_at") or when
                open_turn = TurnTiming(started_at=started)
                session.timing.turns.append(open_turn)
                continue
            if payload_type in ("task_complete", "turn_complete", "task_finished"):
                completed = _payload_time(payload, "completed_at") or when
                duration_ms = _as_int(payload.get("duration_ms"))
                if open_turn is None:
                    # A completion with no visible start still carries a
                    # duration; keep it so the totals do not silently drop.
                    open_turn = TurnTiming()
                    session.timing.turns.append(open_turn)
                open_turn.completed_at = completed
                if duration_ms:
                    open_turn.duration_ms = duration_ms
                ttfb = payload.get("time_to_first_token_ms")
                if isinstance(ttfb, int) and not isinstance(ttfb, bool) and ttfb >= 0:
                    open_turn.time_to_first_token_ms = ttfb
                open_turn = None
                continue
            if payload_type in ("user_message", "user_prompt"):
                session.user_prompt_count += 1
                session.items.user_messages += 1
                continue
            if payload_type in ("agent_message", "assistant_message"):
                session.items.assistant_messages += 1
                continue
            if payload_type in ("context_compacted", "compact", "compaction"):
                session.compaction_count += 1
                continue

            # --- conversation items (counts only) ----------------------
            if top_type == "response_item":
                bucket, tool_name = _item_kind(payload)
                if bucket == "tool_call":
                    session.items.tool_calls += 1
                    session.tool_calls.append(
                        ToolCall(
                            name=tool_name or "unknown",
                            timestamp=when,
                            kind=TOOL_CALL_TYPES.get(str(payload.get("type")), "function"),
                        )
                    )
                    entry = ToolTiming(name=tool_name or "unknown", started_at=when)
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str) and call_id:
                        open_tools[call_id] = entry
                    else:
                        # No id to pair on; keep it for an ordered fallback.
                        anonymous_tools.append(entry)
                    session.timing.tools.append(entry)
                elif bucket == "tool_output":
                    session.items.tool_outputs += 1
                    # Pair the result with its call. Timing lives on the call
                    # entry, which is already in session.timing.tools.
                    call_id = payload.get("call_id")
                    finished_ms = _payload_time_ms(
                        payload, "completed_at_ms", "ended_at_ms"
                    )
                    finished = (finished_ms / 1000.0) if finished_ms else when
                    entry = None
                    if isinstance(call_id, str) and call_id:
                        entry = open_tools.pop(call_id, None)
                    # Without a matching call_id, concurrent calls cannot be
                    # paired reliably; leave their durations unknown.
                    if entry is not None:
                        entry.completed_at = finished
                        if finished_ms:
                            entry.completed_at_ms = finished_ms
                elif bucket == "reasoning":
                    session.items.reasoning_items += 1
                elif bucket == "user_message":
                    session.items.user_messages += 1
                elif bucket == "assistant_message":
                    session.items.assistant_messages += 1
                else:
                    session.items.other += 1
                continue

            # Codex also emits item_completed events carrying item metadata and
            # millisecond stamps; use them to finish an unpaired tool call.
            if payload_type == "item_completed":
                # Only pair an item by an explicit ID; unrelated item events
                # must never complete an anonymous tool or use a start as end.
                item = payload.get("item")
                if isinstance(item, dict) and item.get('type') == 'ContextCompaction':
                    compacted_items += 1
                item_id = item.get("id") if isinstance(item, dict) else None
                finished_ms = _payload_time_ms(payload, "completed_at_ms", "ended_at_ms")
                entry = open_tools.pop(item_id, None) if isinstance(item_id, str) else None
                if entry is not None and finished_ms:
                    entry.completed_at_ms = finished_ms
                    entry.completed_at = finished_ms / 1000.0
                continue

            # --- anything else ----------------------------------------
            candidate = payload_type or top_type
            # Unknown type metadata must be a small snake-case identifier, not
            # an arbitrary JSON object or text containing private content.
            key = candidate if isinstance(candidate, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', candidate) else 'unknown'
            session.unknown_lines[key] = session.unknown_lines.get(key, 0) + 1

    session.compaction_count = max(session.compaction_count, compacted_records, compacted_items)
    if not session.snapshots and not session.deltas:
        session.snapshots = record_snapshots
        session.deltas = record_deltas
    if collector is not None:
        session.details = collector.finish()
    session.started_at = session.started_at or first_time
    session.ended_at = last_time or session.started_at
    if session.turn_count == 0 and session.items.user_messages:
        session.turn_count = session.items.user_messages
    return session


def load_sessions(
    home: Path | None = None, *, limit: int | None = None, detailed: bool = False
) -> list[Session]:
    """Parse the newest ``limit`` rollouts (all when limit is None)."""
    files = find_rollouts(home)
    if limit is not None:
        files = files[:limit]
    sessions = [parse_rollout(p, detailed=detailed) for p in files]
    sessions.sort(key=lambda s: s.started_at or 0, reverse=True)
    return sessions


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


@dataclass
class Aggregate:
    sessions: list[Session]

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    @property
    def totals(self) -> dict[str, int]:
        out = {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0}
        for s in self.sessions:
            t = s.totals
            for key in out:
                out[key] += t.get(key, 0)
        return out

    @property
    def cost(self) -> float | None:
        known = [v for v in (s.cost() for s in self.sessions) if v is not None]
        return sum(known) if known else None

    @property
    def unpriced_sessions(self) -> int:
        return sum(1 for s in self.sessions if s.cost() is None)

    @property
    def cache_hit_rate(self) -> float | None:
        totals = self.totals
        if totals["input"] <= 0:
            return None
        return totals["cached"] / totals["input"]

    @property
    def tool_calls_total(self) -> int:
        return sum(len(s.tool_calls) for s in self.sessions)

    @property
    def turns_total(self) -> int:
        return sum(s.turn_count for s in self.sessions)

    @property
    def compactions_total(self) -> int:
        return sum(s.compaction_count for s in self.sessions)

    @property
    def duration_total(self) -> float:
        return sum(s.duration_seconds or 0.0 for s in self.sessions)

    @property
    def turn_time_total(self) -> float:
        return sum(s.timing.turn_total for s in self.sessions)

    @property
    def tool_time_total(self) -> float:
        return sum(s.timing.tool_total for s in self.sessions)

    @property
    def timed_turns(self) -> int:
        return sum(s.timing.timed_turn_count for s in self.sessions)

    @property
    def timed_tools(self) -> int:
        return sum(s.timing.timed_tool_count for s in self.sessions)

    @property
    def avg_turn_seconds(self) -> float | None:
        values = [
            t.duration
            for s in self.sessions
            for t in s.timing.turns
            if t.duration is not None
        ]
        return sum(values) / len(values) if values else None

    @property
    def first_token_times_ms(self) -> list[int]:
        return [t.time_to_first_token_ms for s in self.sessions for t in s.timing.turns
                if t.time_to_first_token_ms is not None]

    @property
    def timed_first_tokens(self) -> int:
        return len(self.first_token_times_ms)

    @property
    def avg_first_token_ms(self) -> float | None:
        values = self.first_token_times_ms
        return sum(values) / len(values) if values else None

    @property
    def avg_tool_seconds(self) -> float | None:
        values = [
            t.duration
            for s in self.sessions
            for t in s.timing.tools
            if t.duration is not None
        ]
        return sum(values) / len(values) if values else None

    def model_vs_tool(self) -> dict[str, float]:
        """Split active turn time into model work and tool execution.

        Turn time is the wall clock of a turn. Tool time is the sum of tool
        executions inside turns. Whatever remains is the model's own work
        (thinking, generating, streaming). Clamped at zero because overlapping
        or interleaved tool records can exceed a single turn's wall clock.
        """
        turns = self.turn_time_total
        tools = self.tool_time_total
        model = max(0.0, turns - tools)
        return {"turn": turns, "tools": min(tools, turns) if turns else tools, "model": model}

    def tool_timing(self) -> dict[str, dict[str, float]]:
        acc: dict[str, dict[str, float]] = {}
        for s in self.sessions:
            for name, row in s.timing.tool_breakdown().items():
                target = acc.setdefault(name, {"total": 0.0, "count": 0.0})
                target["total"] += row["total"]
                target["count"] += row["count"]
        for row in acc.values():
            row["avg"] = row["total"] / row["count"] if row["count"] else 0.0
        return dict(sorted(acc.items(), key=lambda kv: -kv[1]["total"]))

    def slowest_turns(self, limit: int = 5) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for s in self.sessions:
            for t in s.timing.turns:
                d = t.duration
                if d is None:
                    continue
                rows.append(
                    {
                        "session_id": s.session_id,
                        "model": s.primary_model,
                        "started_at": t.started_at,
                        "duration": d,
                    }
                )
        rows.sort(key=lambda r: -float(r["duration"]))
        return rows[:limit]

    def models(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for s in self.sessions:
            for model, count in s.models.items():
                hist[model] = hist.get(model, 0) + count
        return dict(sorted(hist.items(), key=lambda kv: -kv[1]))

    def tools(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for s in self.sessions:
            for name, count in s.tool_histogram().items():
                hist[name] = hist.get(name, 0) + count
        return dict(sorted(hist.items(), key=lambda kv: -kv[1]))

    def detail_summary(self) -> dict[str, Any]:
        """Aggregate only explicitly collected observations, not inferred facts."""
        files: dict[tuple[str, str], int] = {}
        programs: dict[str, int] = {}
        commands = failed = slow = messages = 0
        for session in self.sessions:
            detail = session.details or {}
            for row in detail.get('files', []):
                key = (str(row.get('kind') or 'unknown'), str(row.get('path') or '未知'))
                files[key] = files.get(key, 0) + 1
            for row in detail.get('commands', []):
                commands += 1
                tags = row.get('tags') or []
                failed += int('failed' in tags)
                slow += int('slow' in tags)
                program = row.get('program')
                if isinstance(program, str) and program:
                    programs[program] = programs.get(program, 0) + 1
            messages += sum(1 for row in detail.get('collaboration', []) if row.get('kind') == 'message')
        return {
            'commands': commands, 'failed': failed, 'slow': slow, 'messages': messages,
            'files': [{'kind': kind, 'path': path, 'count': count}
                      for (kind, path), count in sorted(files.items(), key=lambda item: -item[1])[:20]],
            'programs': [{'name': name, 'count': count}
                         for name, count in sorted(programs.items(), key=lambda item: -item[1])[:12]],
        }

    def item_counts(self) -> dict[str, int]:
        out = {
            "user_messages": 0,
            "assistant_messages": 0,
            "reasoning_items": 0,
            "tool_calls": 0,
            "tool_outputs": 0,
        }
        for s in self.sessions:
            for key in out:
                out[key] += getattr(s.items, key)
        return out

    def daily(self) -> dict[str, dict[str, float]]:
        """Local day -> {tokens, cost, sessions, turns}."""
        buckets: dict[str, dict[str, float]] = {}
        for s in self.sessions:
            if s.started_at is None:
                continue
            try:
                day = datetime.fromtimestamp(s.started_at).strftime("%Y-%m-%d")
            except (ValueError, OverflowError, OSError):
                continue
            bucket = buckets.setdefault(
                day, {"tokens": 0.0, "cost": 0.0, "sessions": 0.0, "turns": 0.0}
            )
            bucket["tokens"] += s.totals["total"]
            bucket["cost"] += s.cost() or 0.0
            bucket["sessions"] += 1
            bucket["turns"] += s.turn_count
        return dict(sorted(buckets.items()))

    def context_pressure_buckets(self) -> dict[str, int]:
        """How many sessions sit in each window-fill band (latest snapshot)."""
        bands = {"<50%": 0, "50-70%": 0, "70-90%": 0, ">=90%": 0}
        for s in self.sessions:
            latest = s.latest
            ratio = latest.usage_ratio if latest else None
            if ratio is None:
                continue
            if ratio >= 0.9:
                bands[">=90%"] += 1
            elif ratio >= 0.7:
                bands["70-90%"] += 1
            elif ratio >= 0.5:
                bands["50-70%"] += 1
            else:
                bands["<50%"] += 1
        return bands


def aggregate(sessions: Iterable[Session]) -> Aggregate:
    return Aggregate(sessions=list(sessions))


if __name__ == "__main__":  # pragma: no cover - smoke check
    agg = aggregate(load_sessions())
    print(
        json.dumps(
            {
                "sessions": agg.session_count,
                "totals": agg.totals,
                "cost": agg.cost,
                "models": agg.models(),
                "turns": agg.turns_total,
            },
            indent=2,
        )
    )
