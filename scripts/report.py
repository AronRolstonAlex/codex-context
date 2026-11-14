#!/usr/bin/env python3
"""Render a Codex context-insight report.

Rebuilds the dsh-context dashboard for Codex on top of the rollout JSONL files
Codex writes itself:

  * KPI bar              - sessions, billed tokens, cost, cache hit, reasoning
  * token composition    - billed tokens split into uncached / cached / output
  * context trend        - per-request context used against the window
  * context pressure     - how close each session sits to its window limit
  * daily usage          - tokens, cost and turns per day
  * conversation anatomy - how many messages / reasoning / tool items
  * model mix, tools     - what carried the work
  * session table        - one row per session with peak and current fill

Charts are hand-drawn inline SVG: no JavaScript, no CDN, no network. The file
opens offline and prints correctly.

Metadata only by default. Detailed local reports require a fresh, explicit
human confirmation for each run (see context_core.py).

Some dsh-context widgets cannot be rebuilt here, because Codex does not record
the underlying figures. Those are named explicitly in ``DATA_GAPS`` and shown
in the report's footer rather than faked.

Usage:
    python3 scripts/report.py                      # newest 30 sessions
    python3 scripts/report.py --limit 100 --out ~/report.html
    python3 scripts/report.py --json out.json --md out.md
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_core import (  # noqa: E402
    Aggregate,
    Session,
    aggregate,
    codex_home,
    load_sessions,
)

# Shown in the report footer and the sub-header.
AUTHOR = "凪梦尘"

# Figures dsh-context shows that Codex does not record. Stated, not faked.
DATA_GAPS = [
    "分类上下文 token：此报告无法可靠还原每次请求的完整系统提示、工具定义及各分类占比。",
    "首 token 时间仅在轮次事件提供 time_to_first_token_ms 时显示，缺失表示未知。",
    "记录浏览器展示保存的条目，并非每次请求真实发送的完整上下文。",
    "协作关系和状态只基于已记录事件，不代表实时状态或完整 Agent 家族。",
    "历史可能包含父会话复制的内容或缺失片段，跨会话总计可能重复。",
]

CSS = """
:root{
  --bg:#0f1117; --panel:#171a23; --panel2:#1d2130; --line:#2a2f3f;
  --fg:#e6e8ef; --muted:#98a0b3; --accent:#7aa2ff; --accent2:#48c78e;
  --warn:#ffb454; --bad:#ff6b6b; --violet:#b28dff; --cyan:#4dd6e8;
  --radius:12px;
}
*{box-sizing:border-box}
body{
  margin:0;padding:30px 20px 70px;background:var(--bg);color:var(--fg);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC",
  "Microsoft YaHei",sans-serif;
}
.wrap{max-width:1240px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.02em}
h2{font-size:16px;margin:0 0 14px;letter-spacing:.01em}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
@media(max-width:980px){.grid2,.grid3{grid-template-columns:1fr}}
@media(max-width:640px){.kpis{grid-template-columns:repeat(2,1fr)!important}}
section{margin-top:26px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:18px 20px}
.card h2{border:0;padding:0;margin:0 0 14px;font-size:14px;color:var(--muted);
  text-transform:uppercase;letter-spacing:.07em;font-weight:500}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:11px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:15px 16px}
.kpi .label{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.07em}
.kpi .value{font-size:23px;font-weight:600;margin-top:5px;font-variant-numeric:tabular-nums;
  letter-spacing:-.01em}
.kpi .note{color:var(--muted);font-size:11.5px;margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:hover{background:var(--panel2)}
tbody tr:last-child td{border-bottom:0}
.bar{position:relative;height:7px;background:var(--panel2);border-radius:99px;
  overflow:hidden;min-width:70px}
.bar > i{position:absolute;inset:0 auto 0 0;border-radius:99px;display:block}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.muted{color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:12px;font-size:12.5px}
.legend span{display:flex;align-items:center;gap:6px;color:var(--muted)}
.legend i{width:9px;height:9px;border-radius:2px;display:inline-block}
.empty{background:var(--panel);border:1px dashed var(--line);border-radius:var(--radius);
  padding:26px;color:var(--muted)}
.foot{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);
  padding-top:16px}
.foot ul{margin:8px 0 0;padding-left:20px}
.foot li{margin:3px 0}
.statline{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.statline:last-child{border-bottom:0}
.statline span:last-child{font-variant-numeric:tabular-nums;font-weight:500}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0}
input,select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px;max-width:100%}
pre{white-space:pre-wrap;word-break:break-word;max-height:24rem;overflow:auto}
details{border-top:1px solid var(--line);padding:9px 0}summary{cursor:pointer} .table-scroll{overflow-x:auto}
.warning{border:1px solid var(--warn);background:#30261c;border-radius:8px;padding:12px;margin:12px 0}
"""

LEVEL_GOOD = "var(--accent2)"
LEVEL_WARN = "var(--warn)"
LEVEL_BAD = "var(--bad)"

# Palette for the token-composition donut.
COMPOSITION_COLORS = {
    "uncached_input": "#7aa2ff",
    "cached_input": "#4dd6e8",
    "visible_output": "#48c78e",
    "reasoning": "#b28dff",
}
COMPOSITION_LABELS = {
    "uncached_input": "未缓存输入",
    "cached_input": "缓存命中输入",
    "visible_output": "可见输出",
    "reasoning": "推理 token",
}


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def human_int(value: float | int | None) -> str:
    if value is None:
        return "—"
    n = float(value)
    for unit, scale in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= scale:
            return f"{n / scale:.1f}{unit}"
    return f"{n:.0f}"


def signed_int(value: int | None) -> str:
    return f"{value:+,}" if value is not None else "—"


def human_usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "$0"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def fmt_time(ts: float | None) -> str:
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "—"


def level_color(ratio: float) -> str:
    if ratio >= 0.9:
        return LEVEL_BAD
    if ratio >= 0.7:
        return LEVEL_WARN
    return LEVEL_GOOD


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def short_path(path: str | None, limit: int = 34) -> str:
    if not path:
        return "—"
    if len(path) <= limit:
        return path
    parts = Path(path).parts
    tail = "/".join(parts[-2:]) if len(parts) >= 2 else path
    return f"…/{tail}"


# --------------------------------------------------------------------------
# SVG primitives
# --------------------------------------------------------------------------


def svg_donut(
    segments: list[tuple[str, float, str]],
    *,
    size: int = 190,
    thickness: int = 26,
) -> str:
    """Ring chart. segments = [(label, value, color)]."""
    total = sum(v for _, v, _ in segments) or 1.0
    cx = cy = size / 2
    radius = (size - thickness) / 2 - 2
    circumference = 2 * math.pi * radius
    parts: list[str] = [
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'role="img" aria-label="token 构成">'
    ]
    parts.append(
        f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
        f'stroke="var(--panel2)" stroke-width="{thickness}"/>'
    )
    offset = 0.0
    for _label, value, color in segments:
        if value <= 0:
            continue
        frac = value / total
        dash = frac * circumference
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{color}" '
            f'stroke-width="{thickness}" '
            f'stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})">'
            f"<title>{esc(_label)}: {value:,.0f} ({frac * 100:.1f}%)</title></circle>"
        )
        offset += dash
    parts.append(
        f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" fill="var(--fg)" '
        f'font-size="17" font-weight="600">{human_int(total)}</text>'
        f'<text x="{cx}" y="{cy + 15}" text-anchor="middle" fill="var(--muted)" '
        f'font-size="10.5">计费 token</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def svg_line_chart(
    series: list[tuple[float | None, int, int | None]],
    *,
    width: int = 720,
    height: int = 220,
) -> str:
    """Context used over requests, with the window ceiling drawn behind it.

    A session that never gets close to its window would otherwise render as a
    flat line pinned to the floor. So the y-axis is scaled to the data, and the
    ceiling is drawn as an annotation with its true ratio in the caption. When
    the data does approach the window, the axis already covers it and the two
    agree.
    """
    if not series:
        return '<div class="empty">没有逐请求的上下文记录。</div>'

    pad_l, pad_r, pad_t, pad_b = 54, 14, 16, 26
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b

    values = [v for _, v, _ in series]
    window = max((w for _, _, w in series if w), default=0)
    peak = max(values) or 1

    # Scale to the data, but keep the window in view when it is nearly reached.
    if window and peak / window >= 0.55:
        y_max = window * 1.08
    else:
        y_max = peak * 1.25

    n = len(series)
    step = inner_w / max(1, n - 1) if n > 1 else 0

    def x_at(i: int) -> float:
        return pad_l + (i * step if n > 1 else inner_w / 2)

    def y_at(v: float) -> float:
        return pad_t + inner_h - (v / y_max) * inner_h

    points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(values))
    area = (
        f"{pad_l:.1f},{pad_t + inner_h:.1f} "
        + points
        + f" {x_at(n - 1):.1f},{pad_t + inner_h:.1f}"
    )

    grid: list[str] = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + inner_h * (1 - frac)
        grid.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + inner_w}" y2="{y:.1f}" '
            f'stroke="var(--line)" stroke-width="1"/>'
            f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'fill="var(--muted)" font-size="10">{human_int(y_max * frac)}</text>'
        )

    ceiling = ""
    if window:
        ratio = peak / window
        if ratio >= 0.55:
            ceiling_y = y_at(window)
            ceiling = (
                f'<line x1="{pad_l}" y1="{ceiling_y:.1f}" x2="{pad_l + inner_w}" '
                f'y2="{ceiling_y:.1f}" stroke="var(--warn)" stroke-width="1" '
                f'stroke-dasharray="5 4"/>'
                f'<text x="{pad_l + inner_w}" y="{ceiling_y - 6:.1f}" '
                f'text-anchor="end" fill="var(--warn)" font-size="10">'
                f"窗口 {human_int(window)}</text>"
            )
        else:
            ceiling = (
                f'<text x="{pad_l + inner_w}" y="{pad_t + 4}" text-anchor="end" '
                f'fill="var(--warn)" font-size="10">'
                f"窗口 {human_int(window)} · 峰值仅占 {ratio * 100:.1f}%</text>"
            )

    xlabels = ""
    if n > 1:
        first_ts = next((t for t, _, _ in series if t), None)
        last_ts = next((t for t, _, _ in reversed(series) if t), None)
        if first_ts:
            xlabels += (
                f'<text x="{pad_l}" y="{height - 6}" fill="var(--muted)" '
                f'font-size="10">{esc(fmt_time(first_ts)[5:])}</text>'
            )
        if last_ts:
            xlabels += (
                f'<text x="{pad_l + inner_w}" y="{height - 6}" text-anchor="end" '
                f'fill="var(--muted)" font-size="10">{esc(fmt_time(last_ts)[5:])}</text>'
            )
        xlabels += (
            f'<text x="{pad_l + inner_w / 2}" y="{height - 6}" text-anchor="middle" '
            f'fill="var(--muted)" font-size="10">{n} 次请求</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="auto" '
        f'role="img" aria-label="上下文趋势">'
        + "".join(grid)
        + ceiling
        + f'<polygon points="{area}" fill="var(--accent)" fill-opacity="0.16"/>'
        + f'<polyline points="{points}" fill="none" stroke="var(--accent)" '
        f'stroke-width="2" stroke-linejoin="round"/>'
        + xlabels
        + "</svg>"
    )


def svg_daily_bars(daily: dict[str, dict[str, float]], *, width: int = 720, height: int = 200) -> str:
    """Paired bars: tokens per day, with cost annotated on hover."""
    if not daily:
        return '<div class="empty">暂无按日数据。</div>'

    days = list(daily.items())[-14:]
    pad_l, pad_r, pad_t, pad_b = 54, 14, 14, 30
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b
    y_max = max((b["tokens"] for _, b in days), default=0) or 1

    slot = inner_w / len(days)
    bar_w = min(38.0, slot * 0.55)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="auto" '
        f'role="img" aria-label="按日用量">'
    ]
    for frac in (0, 0.5, 1.0):
        y = pad_t + inner_h * (1 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + inner_w}" y2="{y:.1f}" '
            f'stroke="var(--line)"/>'
            f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'fill="var(--muted)" font-size="10">{human_int(y_max * frac)}</text>'
        )

    for i, (day, bucket) in enumerate(days):
        h = (bucket["tokens"] / y_max) * inner_h
        x = pad_l + slot * i + (slot - bar_w) / 2
        y = pad_t + inner_h - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(h, 1):.1f}" '
            f'rx="4" fill="var(--accent)" fill-opacity="0.85">'
            f"<title>{esc(day)}: {human_int(bucket['tokens'])} token, "
            f"{human_usd(bucket['cost'] or None)}, {int(bucket['sessions'])} 个会话"
            f"</title></rect>"
        )
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 10}" text-anchor="middle" '
            f'fill="var(--muted)" font-size="9.5">{esc(day[5:])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_ratio_bar(ratio: float | None) -> str:
    if ratio is None:
        return '<span class="muted">—</span>'
    pct = max(0.0, min(1.0, ratio)) * 100
    color = level_color(ratio)
    return (
        '<div style="display:flex;align-items:center;gap:8px">'
        f'<div class="bar" style="flex:1"><i style="width:{pct:.1f}%;background:{color}"></i></div>'
        f'<span class="mono" style="min-width:44px;text-align:right">{pct:.1f}%</span>'
        "</div>"
    )


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


def build_payload(agg: Aggregate, home: Path, *, detailed: bool = False) -> dict:
    totals = agg.totals
    sessions_payload = []
    for s in agg.sessions:
        latest = s.latest
        series = s.context_series()
        sessions_payload.append(
            {
                "session_id": s.session_id,
                "started_at": s.started_at,
                "started_text": fmt_time(s.started_at),
                "ended_text": fmt_time(s.ended_at),
                "duration_seconds": s.duration_seconds,
                "cwd": s.cwd if detailed else None,
                "model": s.primary_model,
                "models": s.models,
                "effort": s.effort,
                "cli_version": s.cli_version,
                "turns": s.turn_count,
                "user_prompts": s.user_prompt_count,
                "compactions": s.compaction_count,
                "requests": len(s.snapshots),
                "tokens": s.totals,
                "cost_usd": s.cost() if s.snapshots or s.deltas else None,
                "context_window": s.context_window,
                "peak_context": s.peak_context,
                "min_context": s.min_context,
                "current_context": (latest.context_size or None) if latest else None,
                "current_usage_ratio": latest.usage_ratio if latest else None,
                "tools": s.tool_histogram(),
                "items": s.items.as_dict(),
                "context_series": [
                    {"t": t, "used": used, "window": window} for t, used, window in series
                ],
                "context_changes": s.context_changes(),
                "unknown_lines": s.unknown_lines,
                "path": str(s.path) if detailed else "",
                "usage_available": bool(s.snapshots or s.deltas),
                **({"details": s.details} if detailed and s.details is not None else {}),
            }
        )

    totals_comp = {
        "uncached_input": max(0, totals["input"] - totals["cached"]),
        "cached_input": totals["cached"],
        "visible_output": max(0, totals["output"] - totals["reasoning"]),
        "reasoning": totals["reasoning"],
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "codex_home": str(home) if detailed else "(路径已隐藏)",
        "session_count": agg.session_count,
        "usage_available": any(s.snapshots or s.deltas for s in agg.sessions),
        "totals": totals,
        "composition": totals_comp,
        "cost_usd": agg.cost if any(s.snapshots or s.deltas for s in agg.sessions) else None,
        "unpriced_sessions": agg.unpriced_sessions,
        "cache_hit_rate": agg.cache_hit_rate,
        "models": agg.models(),
        "tools": agg.tools(),
        "items": agg.item_counts(),
        "daily": agg.daily(),
        "pressure": agg.context_pressure_buckets(),
        "turns_total": agg.turns_total,
        "timing": {
            "turn_total": agg.turn_time_total,
            "tool_total": agg.tool_time_total,
            "model_vs_tool": agg.model_vs_tool(),  # legacy estimate, not measured; retained for compatibility
            "model_vs_tool_note": "兼容字段：简单相减的粗略估算，不适用于并发或缺失数据。请勿视为实测模型耗时。",
            "avg_turn": agg.avg_turn_seconds,
            "avg_tool": agg.avg_tool_seconds,
            "timed_turns": agg.timed_turns,
            "timed_tools": agg.timed_tools,
            "first_token_ms": [t.time_to_first_token_ms for s in agg.sessions for t in s.timing.turns if t.time_to_first_token_ms is not None],
            "by_tool": agg.tool_timing(),
            "slowest": [
                {
                    "session_id": r["session_id"],
                    "model": r["model"],
                    "duration": r["duration"],
                    "started_text": fmt_time(r["started_at"]),  # type: ignore[arg-type]
                }
                for r in agg.slowest_turns(5)
            ],
        },
        "tool_calls_total": agg.tool_calls_total,
        "compactions_total": agg.compactions_total,
        "duration_total": agg.duration_total,
        "sessions": sessions_payload,
        "privacy": "detailed" if detailed else "metadata_only",
        "data_gaps": DATA_GAPS,
    }


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------


def _kpis(payload: dict) -> str:
    totals = payload["totals"]
    cache = payload["cache_hit_rate"]
    cards = [
        ("会话数", str(payload["session_count"]), f"{payload['turns_total']} 轮"),
        ("计费 Token", human_int(totals["total"]) if payload["usage_available"] else "—",
         f"输入 {human_int(totals['input'])} / 输出 {human_int(totals['output'])}"),
        ("预估成本", human_usd(payload["cost_usd"]),
         "部分模型未计价" if payload["unpriced_sessions"] else "全部模型已计价"),
        ("缓存命中", f"{cache * 100:.1f}%" if cache is not None else "—",
         f"命中 {human_int(totals['cached'])}"),
        ("推理 Token", human_int(totals["reasoning"]) if payload["usage_available"] else "—",
         f"占总输出 {totals['reasoning'] / max(1, totals['output']) * 100:.0f}%"),
        ("工具调用", str(payload["tool_calls_total"]), f"{len(payload['tools'])} 种工具"),
        ("上下文压缩", str(payload["compactions_total"]), "已识别的保守下限，可能不完整"),
        ("平均每轮", human_duration(payload["timing"]["avg_turn"]),
         f"共 {human_duration(payload['timing']['turn_total'])}"),
    ]
    return "".join(
        f'<div class="kpi"><div class="label">{esc(l)}</div>'
        f'<div class="value">{esc(v)}</div>'
        f'<div class="note">{esc(n)}</div></div>'
        for l, v, n in cards
    )


def _composition_card(payload: dict) -> str:
    """Token composition, twice: by all tokens, then by output only.

    Input dwarfs output in a coding session (often 99%+), so a single ring
    makes the model's own work invisible. The second ring answers the question
    people actually ask about output: how much of it was reasoning.
    """
    comp = payload["composition"]
    totals = payload["totals"]
    if not payload["usage_available"]:
        return '<div class="card"><h2>Token 构成</h2><div class="empty">没有可用的 token 记录。</div></div>'
    total = sum(comp.values()) or 1

    segments = [(COMPOSITION_LABELS[k], comp[k], COMPOSITION_COLORS[k]) for k in comp]
    rows = "".join(
        '<div class="statline">'
        f'<span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;'
        f'background:{COMPOSITION_COLORS[k]};margin-right:8px"></i>'
        f"{esc(COMPOSITION_LABELS[k])}</span>"
        f"<span>{human_int(comp[k])} "
        f'<span class="muted">({comp[k] / total * 100:.1f}%)</span></span>'
        "</div>"
        for k in comp
    )

    out_segments = [
        ("可见输出", comp["visible_output"], COMPOSITION_COLORS["visible_output"]),
        ("推理 token", comp["reasoning"], COMPOSITION_COLORS["reasoning"]),
    ]

    input_share = (totals["input"] / totals["total"] * 100) if totals["total"] else 0
    note = (
        f"输入占全部 token 的 {input_share:.1f}%，所以两张环分开看："
        "左环是计费全貌，右环只看模型输出了什么。"
    )

    return (
        '<div class="card"><h2>Token 构成</h2>'
        '<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">'
        '<div style="flex:0 0 auto;text-align:center">'
        f"{svg_donut(segments, size=168)}"
        '<div class="muted" style="font-size:11px;margin-top:2px">全部计费 token</div>'
        "</div>"
        '<div style="flex:0 0 auto;text-align:center">'
        f'{svg_donut(out_segments, size=168)}'
        '<div class="muted" style="font-size:11px;margin-top:2px">仅模型输出</div>'
        "</div>"
        f'<div style="flex:1;min-width:200px">{rows}</div>'
        "</div>"
        f'<p class="muted" style="font-size:11.5px;margin:12px 0 0">{esc(note)}</p>'
        "</div>"
    )


def _pressure_card(payload: dict) -> str:
    pressure = payload["pressure"]
    total = sum(pressure.values())
    bands = [
        ("<50%", pressure["<50%"], LEVEL_GOOD),
        ("50–70%", pressure["50-70%"], "#8fd18f"),
        ("70–90%", pressure["70-90%"], LEVEL_WARN),
        ("≥90%", pressure[">=90%"], LEVEL_BAD),
    ]
    rows = "".join(
        '<div class="statline">'
        f"<span>{esc(label)}</span>"
        f'<span>{count} <span class="muted">个会话</span></span></div>'
        for label, count, _ in bands
    )
    stack = ""
    if total:
        for label, count, color in bands:
            if count:
                stack += (
                    f'<i style="width:{count / total * 100:.1f}%;background:{color};'
                    'height:100%;display:block"></i>'
                )
    stack_html = (
        '<div style="display:flex;height:10px;border-radius:99px;overflow:hidden;'
        f'background:var(--panel2);margin:14px 0 10px">{stack}</div>'
        if stack
        else ""
    )
    note = (
        "按每个会话最后一次有记录的请求读数分档，输入量回退为近似；不代表实时窗口。"
        "压缩时机由 Codex 和模型决定。"
    )
    return (
        '<div class="card"><h2>上下文压力</h2>'
        f"{stack_html}{''.join(rows)}"
        f'<p class="muted" style="font-size:11.5px;margin:12px 0 0">{esc(note)}</p>'
        "</div>"
    )


def _anatomy_card(payload: dict) -> str:
    items = payload["items"]
    rows = [
        ("用户消息", items["user_messages"]),
        ("助手消息", items["assistant_messages"]),
        ("推理条目", items["reasoning_items"]),
        ("工具调用", items["tool_calls"]),
        ("工具结果", items["tool_outputs"]),
    ]
    body = "".join(
        '<div class="statline">'
        f"<span>{esc(label)}</span>"
        f'<span>{count:,}</span></div>'
        for label, count in rows
    )
    return (
        '<div class="card"><h2>对话解剖</h2>'
        f"{body}"
        '<p class="muted" style="font-size:11.5px;margin:12px 0 0">'
        "只统计条目数量，不保留任何内容。</p>"
        "</div>"
    )


def svg_time_stack(model: float, tools: float, *, width: int = 560, height: int = 46) -> str:
    """Horizontal stacked bar: model time vs tool time."""
    total = model + tools
    if total <= 0:
        return '<div class="empty">没有可用的耗时记录。</div>'
    pad = 2
    inner = width - pad * 2
    model_w = inner * (model / total)
    tool_w = inner * (tools / total)
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="耗时拆分">'
        f'<rect x="{pad}" y="8" width="{model_w:.1f}" height="20" rx="4" fill="var(--accent)">'
        f"<title>模型 {human_duration(model)} ({model / total * 100:.1f}%)</title></rect>"
        f'<rect x="{pad + model_w:.1f}" y="8" width="{tool_w:.1f}" height="20" rx="4" '
        f'fill="var(--accent2)">'
        f"<title>工具 {human_duration(tools)} ({tools / total * 100:.1f}%)</title></rect>"
        f'<text x="{pad}" y="42" fill="var(--muted)" font-size="10.5">'
        f"模型 {human_duration(model)}</text>"
        f'<text x="{width - pad}" y="42" text-anchor="end" fill="var(--muted)" '
        f'font-size="10.5">工具 {human_duration(tools)}</text>'
        "</svg>"
    )


def _timing_card(payload: dict) -> str:
    """Timing card, mirroring dsh-context's 耗时统计."""
    timing = payload["timing"]
    turn_total = timing["turn_total"]
    tool_total = timing["tool_total"]
    split = timing["model_vs_tool"]

    if turn_total <= 0 and tool_total <= 0:
        return (
            '<div class="card"><h2>耗时统计</h2>'
            '<div class="empty">这批会话没有可用的时间记录。</div></div>'
        )

    by_tool = timing["by_tool"]
    max_tool = max((r["total"] for r in by_tool.values()), default=0) or 1
    tool_rows = "".join(
        "<tr>"
        f'<td class="mono">{esc(name)}</td>'
        f'<td class="num">{int(row["count"])}</td>'
        f'<td class="num">{human_duration(row["total"])}</td>'
        f'<td class="num">{row["avg"]:.1f}s</td>'
        f'<td><div class="bar"><i style="width:{row["total"] / max_tool * 100:.1f}%;'
        'background:var(--accent2)"></i></div></td>'
        "</tr>"
        for name, row in list(by_tool.items())[:8]
    )
    tool_table = (
        '<table><thead><tr><th>工具</th><th class="num">次数</th>'
        '<th class="num">总耗时</th><th class="num">平均</th><th>占比</th></tr></thead>'
        f"<tbody>{tool_rows}</tbody></table>"
        if tool_rows
        else '<p class="muted" style="font-size:12px">没有工具耗时记录。</p>'
    )

    slowest = timing["slowest"]
    slowest_rows = "".join(
        "<tr>"
        f'<td class="mono">{esc(str(r["session_id"] or "")[:8])}</td>'
        f'<td class="mono muted">{esc(r["started_text"])}</td>'
        f'<td class="mono">{esc(r["model"] or "—")}</td>'
        f'<td class="num">{human_duration(r["duration"])}</td>'
        "</tr>"
        for r in slowest
    )
    slowest_table = (
        '<table><thead><tr><th>会话</th><th>开始</th><th>模型</th>'
        f'<th class="num">耗时</th></tr></thead><tbody>{slowest_rows}</tbody></table>'
        if slowest_rows
        else '<p class="muted" style="font-size:12px">没有轮次耗时记录。</p>'
    )

    stats = [
        ("轮次耗时之和", human_duration(turn_total if timing['timed_turns'] else None), f"{timing['timed_turns']} 轮可计时"),
        ("工具耗时之和", human_duration(tool_total if timing['timed_tools'] else None), f"{timing['timed_tools']} 次可计时；可重叠"),
        ("平均每轮", human_duration(timing["avg_turn"]), "含模型与工具"),
        ("平均每工具", human_duration(timing["avg_tool"]), "单次执行"),
    ]
    stat_rows = "".join(
        '<div class="statline">'
        f"<span>{esc(label)}</span>"
        f'<span>{esc(value)} <span class="muted">{esc(note)}</span></span></div>'
        for label, value, note in stats
    )

    first = timing['first_token_ms']
    first_note = f"首 token 平均 {sum(first) / len(first):.0f}ms（{len(first)} 轮有记录）" if first else "首 token 耗时：未知"
    note = (
        "轮次耗时为单轮墙钟时间；工具耗时为调用到配对结果的时间，各次可以并发重叠。"
        "工具耗时之和不能从轮次总耗时相减得出模型耗时；无可靠拆分时不显示。" + first_note
    )

    return (
        '<div class="card"><h2>耗时统计</h2>'
        f"{stat_rows}"
        f'<p class="muted" style="font-size:11.5px;margin:12px 0 0">{esc(note)}</p>'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:18px">'
        f'<div><h2 style="margin-bottom:8px">按工具</h2>{tool_table}</div>'
        f'<div><h2 style="margin-bottom:8px">最慢轮次</h2>{slowest_table}</div>'
        "</div></div>"
    )


def _detail_panel(payload: dict) -> str:
    """Render only bounded, pre-collected details; all data is escaped text."""
    if payload["privacy"] != "detailed":
        return ''
    groups: dict[str, list[str]] = {"records": [], "events": [], "files": [], "commands": [], "fleet": []}
    notes = []
    file_counts: dict[tuple[str, str], int] = {}
    program_counts: dict[str, int] = {}
    observed_commands = observed_failed = observed_slow = observed_messages = 0
    for session in payload['sessions']:
        detail = session.get('details') or {}
        sid = str(session.get('session_id') or '未知')[:12]
        if detail.get('truncated'):
            notes.append(f"会话 {sid} 的部分条目达到上限，已截断")
        parent = detail.get('parent_thread_id')
        if parent:
            groups['fleet'].append(f'<div class="filter-row" data-tags="关系"><b>{esc(sid)}</b> 的记录父线程：{esc(parent)}</div>')
        for row in detail.get('records', []):
            title = f"{sid} · {fmt_time(row.get('time'))} · {row.get('kind', '记录')} · 轮次 {row.get('turn', 0)}"
            groups['records'].append(f'<details class="filter-row" data-tags="{esc(row.get("kind", ""))}"><summary>{esc(title)}</summary><pre>{esc(row.get("content", ""))}</pre></details>')
        for row in detail.get('events', []):
            groups['events'].append(f'<div class="filter-row" data-tags="压缩">{esc(sid)} · {esc(fmt_time(row.get("time")))} · {esc(row.get("kind"))}</div>')
        for row in detail.get('files', []):
            tags = {'read': '读取', 'write': '写入', 'search': '搜索', 'image': '图片'}.get(row.get('kind'), '未知')
            key = (tags, str(row.get('path') or '未知'))
            file_counts[key] = file_counts.get(key, 0) + 1
            groups['files'].append(f'<div class="filter-row" data-tags="{tags}"><b>{tags}</b> · {esc(sid)} · {esc(fmt_time(row.get("time")))} · <span class="mono">{esc(row.get("path"))}</span> <span class="muted">{esc(row.get("source"))}</span></div>')
        for row in detail.get('commands', []):
            observed_commands += 1
            program = row.get('program')
            if program:
                program_counts[program] = program_counts.get(program, 0) + 1
            status = str(row.get('status') or '未知')
            code = row.get('exit_code')
            duration = row.get('duration')
            tags = []
            if status.lower() == 'failed' or (isinstance(code, int) and code != 0):
                tags.append('失败')
                observed_failed += 1
            if isinstance(duration, (int, float)) and duration >= 10:
                tags.append('慢')
                observed_slow += 1
            if status.lower() in ('in_progress', 'inprogress'):
                tags.append('后台')
            title = f"{sid} · {fmt_time(row.get('time'))} · {status} · 退出码 {code if code is not None else '未知'} · {human_duration(duration)}"
            body = f"命令：\n{row.get('command', '')}\n\n输出（可能已截断）：\n{row.get('output', '')}"
            groups['commands'].append(f'<details class="filter-row" data-tags="{esc(" ".join(tags))}"><summary>{esc(title)}</summary><pre>{esc(body)}</pre></details>')
        for row in detail.get('collaboration', []):
            if row.get('kind') == 'message':
                observed_messages += 1
            title = f"{sid} · {fmt_time(row.get('time'))} · {row.get('kind', '事件')} · {row.get('status', '未知')}"
            body = f"发送方：{row.get('sender', '')}\n接收方：{row.get('recipients', '')}\n\n{row.get('content', '')}"
            groups['fleet'].append(f'<details class="filter-row" data-tags="事件"><summary>{esc(title)}</summary><pre>{esc(body)}</pre></details>')

    def panel(key: str, title: str, choices: list[str], note: str) -> str:
        options = ''.join(f'<option value="{esc(choice)}">{esc(choice)}</option>' for choice in choices)
        rows = ''.join(groups[key]) or '<div class="empty">没有可用记录；不代表没有发生。</div>'
        return (f'<section class="card" id="{key}"><h2>{esc(title)}</h2><p class="muted">{esc(note)}</p>'
                f'<div class="controls"><label>筛选 <select data-filter="{key}"><option value="">全部</option>{options}</select></label>'
                f'<label>搜索 <input type="search" data-search="{key}" placeholder="在已加载的记录中搜索"></label></div>'
                f'<div class="rows">{rows}</div></section>')
    file_summary = '；'.join(f'{kind} {path} ×{count}' for (kind, path), count in sorted(file_counts.items(), key=lambda item: -item[1])[:20]) or '没有可汇总的路径'
    program_summary = '；'.join(f'{name} ×{count}' for name, count in sorted(program_counts.items(), key=lambda item: -item[1])[:12]) or '程序分布未知'
    warning = '<div class="warning">更详细分析：此报告可能包含提示词、回答、命令、输出、文件路径及协作消息中的敏感数据。请仅保存在私密位置，不要随意分享。</div>'
    if notes:
        warning += '<p class="warning">' + esc('；'.join(notes)) + '</p>'
    return (warning
        + panel('records', '记录浏览器', ['user', 'assistant', '工具调用', '工具结果'], '展示已保存条目，不是任一请求完整上下文；历史可能包含继承内容。')
        + panel('events', '上下文事件', ['压缩'], '仅列可识别的压缩记录；不同记录可能描述同一次操作，不能直接当作去重次数。')
        + panel('files', '文件活动', ['读取', '写入', '搜索', '图片'], '来自结构化命令解析、文件变更及图片条目；不是完整文件审计，调用不等于成功。路径频次（最多 20 项）：' + file_summary)
        + panel('commands', '命令执行', ['失败', '慢', '后台'], f'已记录 {observed_commands} 条，已知失败 {observed_failed} 条，已知慢 {observed_slow} 条；程序：{program_summary}。旧格式状态未知，慢指有时长且不少于 10 秒；后台仅显示明确记录为运行中的条目。')
        + panel('fleet', '编队 · 已记录的协作', ['关系', '事件'], f'已记录消息 {observed_messages} 条；来自父线程字段、协作条目和 Agent 间消息，仅为历史观察，不推断实时运行状态。'))


_DETAIL_SCRIPT = """<script>
(function(){
  function update(id){
    const panel=document.getElementById(id); if(!panel)return;
    const filter=panel.querySelector('[data-filter]').value;
    const text=panel.querySelector('[data-search]').value.toLocaleLowerCase();
    panel.querySelectorAll('.filter-row').forEach(function(row){
      row.hidden=Boolean((filter && !(row.dataset.tags||'').includes(filter)) || (text && !row.textContent.toLocaleLowerCase().includes(text)));
    });
  }
  document.querySelectorAll('[data-filter],[data-search]').forEach(function(el){
    el.addEventListener(el.matches('select')?'change':'input', function(){update(el.dataset.filter||el.dataset.search)});
  });
})();
</script>"""


def render_html(payload: dict) -> str:
    if payload["session_count"] == 0:
        empty = (
            '<div class="empty">'
            "没有找到 Codex 会话文件。<br><br>"
            f"预期位置：<span class='mono'>{esc(payload['codex_home'])}/sessions/YYYY/MM/DD/rollout-*.jsonl</span><br>"
            "先用 Codex 跑过至少一个会话，或设置 <span class='mono'>CODEX_HOME</span> 指向正确目录后重试。"
            "</div>"
        )
        return (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:\">"
            "<title>Codex 上下文洞察</title>"
            f"<style>{CSS}</style></head><body><div class='wrap'>"
            "<h1>Codex 上下文洞察</h1>"
            f"<div class='sub'>生成于 {esc(payload['generated_at'])} · 作者 {esc(AUTHOR)}</div>"
            f"{empty}</div></body></html>"
        )

    # aggregate context trend across the busiest session
    busiest = max(
        payload["sessions"],
        key=lambda r: len(r["context_series"]),
        default=None,
    )
    trend_series: list[tuple[float | None, int, int | None]] = []
    trend_title = "上下文趋势"
    if busiest and busiest["context_series"]:
        trend_series = [
            (p["t"], p["used"], p["window"]) for p in busiest["context_series"]
        ]
        trend_title = f"上下文趋势 · {str(busiest['session_id'] or '')[:8]}"
    changes = busiest.get('context_changes', []) if busiest else []
    change_rows = ''.join(
        f'<tr><td>{index}</td><td>{esc(fmt_time(row["timestamp"]))}</td>'
        f'<td class="num">{human_int(row["used"])}</td>'
        f'<td class="num">{esc(signed_int(row["change"]))}</td></tr>'
        for index, row in enumerate(changes[-500:], max(1, len(changes) - 499))
    )
    changes_table = ('<details><summary>逐请求全量与变化（最近 500 条）</summary><div class="table-scroll"><table>'
                     '<thead><tr><th>序号</th><th>时间</th><th>记录/近似值</th><th>与上一记录差值</th></tr></thead>'
                     f'<tbody>{change_rows}</tbody></table></div></details>') if change_rows else ''

    models = payload["models"]
    total_model_turns = sum(models.values()) or 1
    model_rows = "".join(
        "<tr>"
        f'<td class="mono">{esc(name)}</td>'
        f'<td class="num">{count}</td>'
        f'<td><div class="bar"><i style="width:{count / total_model_turns * 100:.1f}%;'
        'background:var(--accent)"></i></div></td>'
        "</tr>"
        for name, count in models.items()
    )
    model_table = (
        '<table><thead><tr><th>模型</th><th class="num">轮次</th><th>占比</th></tr></thead>'
        f"<tbody>{model_rows}</tbody></table>"
        if model_rows
        else '<div class="empty">会话未记录模型名。</div>'
    )

    tools = payload["tools"]
    total_tool_calls = sum(tools.values()) or 1
    tool_rows = "".join(
        "<tr>"
        f'<td class="mono">{esc(name)}</td>'
        f'<td class="num">{count}</td>'
        f'<td><div class="bar"><i style="width:{count / total_tool_calls * 100:.1f}%;'
        'background:var(--accent2)"></i></div></td>'
        "</tr>"
        for name, count in list(tools.items())[:15]
    )
    tool_table = (
        '<table><thead><tr><th>工具</th><th class="num">调用</th><th>占比</th></tr></thead>'
        f"<tbody>{tool_rows}</tbody></table>"
        if tool_rows
        else '<div class="empty">该批会话没有工具调用记录。</div>'
    )

    session_rows = []
    for row in payload["sessions"]:
        window = row["context_window"]
        peak = row["peak_context"]
        label = row["session_id"] or (Path(row["path"]).name if row["path"] else "未知")
        session_rows.append(
            "<tr>"
            f'<td class="mono">{esc(label[:8])}</td>'
            f'<td class="mono muted">{esc(row["started_text"][5:])}</td>'
            f'<td class="mono">{esc(row["model"] or "—")}</td>'
            f'<td class="mono muted">{esc(short_path(row["cwd"]))}</td>'
            f'<td class="num">{row["turns"]}</td>'
            f'<td class="num">{row["requests"]}</td>'
            f'<td class="num">{human_int(row["tokens"]["total"]) if row["usage_available"] else "—"}</td>'
            f'<td class="num">{human_usd(row["cost_usd"])}</td>'
            f'<td class="num">{human_int(peak) if peak else "—"}'
            f'<span class="muted"> / {human_int(window) if window else "—"}</span></td>'
            f'<td class="num">{row["compactions"] or "—"}</td>'
            f'<td style="min-width:130px">{svg_ratio_bar(row["current_usage_ratio"])}</td>'
            "</tr>"
        )
    session_table = (
        '<table><thead><tr>'
        '<th>会话</th><th>开始</th><th>模型</th><th>目录</th>'
        '<th class="num">轮次</th><th class="num">请求</th><th class="num">Token</th>'
        '<th class="num">成本</th><th class="num">峰值/窗口</th><th class="num">压缩</th>'
        '<th>当前占用</th>'
        f'</tr></thead><tbody>{"".join(session_rows)}</tbody></table>'
    )

    unknown: dict[str, int] = {}
    for row in payload["sessions"]:
        for key, count in row["unknown_lines"].items():
            unknown[key] = unknown.get(key, 0) + count
    unknown_note = ""
    if unknown:
        detail = "、".join(
            f"{esc(k)}×{v}" for k, v in sorted(unknown.items(), key=lambda kv: -kv[1])[:8]
        )
        unknown_note = (
            '<p class="muted" style="font-size:12px">'
            f"未识别但已安全跳过的行类型：{detail}。"
            "这些计数用于说明解析覆盖率，不影响上方统计。</p>"
        )

    gaps = "".join(f"<li>{esc(g)}</li>" for g in payload["data_gaps"])

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<title>Codex 上下文洞察</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<h1>Codex 上下文洞察</h1>
<div class="sub">生成于 {esc(payload['generated_at'])} · 数据源 <span class="mono">{esc(payload['codex_home'])}</span> · {('更详细分析' if payload['privacy'] == 'detailed' else '仅元数据')} · 作者 {esc(AUTHOR)}</div>

<div class="kpis">{_kpis(payload)}</div>

<section class="grid2">
  {_composition_card(payload)}
  {_pressure_card(payload)}
</section>

<section class="card">
  <h2>{esc(trend_title)}</h2>
  {svg_line_chart(trend_series)}
  <div class="legend">
    <span><i style="background:var(--accent)"></i>每次有记录的请求值（可能由输入量近似）</span>
    <span><i style="background:var(--warn)"></i>模型上下文窗口上限（可能为内置回退）</span>
  </div>
  {changes_table}
</section>

{_timing_card(payload)}

<section class="card">
  <h2>按日用量</h2>
  {svg_daily_bars(payload['daily'])}
</section>

<section class="grid3">
  <div class="card"><h2>模型分布</h2>{model_table}</div>
  <div class="card"><h2>工具调用</h2>{tool_table}</div>
  {_anatomy_card(payload)}
</section>

<section class="card">
  <h2>会话明细</h2>
  {session_table}
  {unknown_note}
</section>

{_detail_panel(payload)}
<div class="foot">
<p style="margin:0 0 10px">作者：<strong>{esc(AUTHOR)}</strong></p>
{('更详细分析已获本次授权，报告可能包含敏感内容。' if payload['privacy'] == 'detailed' else '统计模式不提取对话正文、工具参数、输出或工作目录。')}
成本为估算，不等同账单。当前占用是最后一次有记录的请求读数或近似值，不保证等于实时窗口。
<p style="margin-bottom:4px"><strong>本报告的限制</strong>（无法可靠还原的数据不编造）：</p>
<ul>{gaps}</ul>
</div>
</div>{_DETAIL_SCRIPT if payload['privacy'] == 'detailed' else ''}</body></html>
"""


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def render_markdown(payload: dict) -> str:
    totals = payload["totals"]
    cache = payload["cache_hit_rate"]
    lines = [
        "# Codex 上下文洞察",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 数据源：`{payload['codex_home']}/sessions`",
        f"- 会话数：{payload['session_count']}（{payload['turns_total']} 轮）",
        (f"- 计费 Token：{totals['total']:,}（输入 {totals['input']:,} / 输出 {totals['output']:,}）" if payload['usage_available'] else "- 计费 Token：未知"),
        f"- 预估成本：{human_usd(payload['cost_usd'])}",
        f"- 缓存命中率：{f'{cache * 100:.1f}%' if cache is not None else '—'}",
        f"- 工具调用：{payload['tool_calls_total']} 次",
        f"- 上下文压缩：{payload['compactions_total']} 次",
    ]
    tm = payload["timing"]
    if tm["turn_total"] or tm["tool_total"]:
        split = tm["model_vs_tool"]
        lines.extend(
            [
                f"- 轮次耗时合计：{human_duration(tm['turn_total'])}"
                f"（工具各次耗时之和 {human_duration(tm['tool_total'])}，可能重叠）",
                f"- 平均每轮：{human_duration(tm['avg_turn'])}"
                f" · 平均每工具：{human_duration(tm['avg_tool'])}",
            ]
        )
    lines.extend(
        [
            "",
            "## 会话明细",
            "",
            "| 会话 | 开始 | 模型 | 轮次 | 请求 | Token | 成本 | 峰值/窗口 | 压缩 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["sessions"]:
        window = human_int(row["context_window"]) if row["context_window"] else "—"
        lines.append(
            f"| `{(row['session_id'] or '—')[:8]}` | {row['started_text']} | "
            f"{row['model'] or '—'} | {row['turns']} | {row['requests']} | "
            f"{format(row['tokens']['total'], ',') if row['usage_available'] else '—'} | {human_usd(row['cost_usd'])} | "
            f"{human_int(row['peak_context']) if row['peak_context'] else '—'} / {window} | "
            f"{row['compactions'] or '—'} |"
        )
    lines.append("")
    lines.append(f"---")
    lines.append(f"作者：{AUTHOR} · " + ("更详细分析：报告可能含敏感数据" if payload['privacy'] == 'detailed' else "统计模式：不提取对话正文、工具参数与输出"))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


CONSENT_MESSAGE = (
    "更详细分析将读取所选 Codex 会话中保存的消息正文、命令及参数、工具输出、文件路径和 Agent 协作内容。\n"
    "生成的本地报告可能包含密码或其他敏感数据；请仅保存在私密位置，不要随意分享。\n"
    "本工具不联网、不上传。授权仅对本次生成有效，自动钩子仍为统计模式。\n"
    "确认仅本次读取并生成详细报告吗？"
)


def request_detailed_consent(scope: str = '') -> bool:
    """Require an interactive human decision; flags, environment and pipes cannot grant it."""
    message = CONSENT_MESSAGE + ('\n\n' + scope if scope else '')
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            return bool(messagebox.askyesno('更详细分析 · 本次授权', message, parent=root, default='no'))
        finally:
            root.destroy()
    except Exception:
        # A real terminal is a fallback, not a pipe from an automated caller.
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            print('无法显示授权窗口且没有交互终端；已安全取消详细分析。', file=sys.stderr)
            return False
        print(message, file=sys.stderr)
        try:
            return input('仅本次同意请输入 YES，其他输入取消：').strip() == 'YES'
        except (EOFError, KeyboardInterrupt):
            return False


def write_private(path: Path, content: str) -> None:
    """Atomically replace the chosen output with a private temporary file.

    POSIX mode is 0600; Windows uses the destination directory's ACL. No input
    path or user data is copied elsewhere.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix='.codex-context-', dir=str(path.parent))
    try:
        if os.name == 'posix':
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            fd = -1
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        if fd != -1:
            os.close(fd)
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地 Codex 报告；默认仅统计，详细分析需要本次人工授权。"
    )
    parser.add_argument("--limit", type=int, default=30,
                        help="how many recent sessions (default 30, 0 = all)")
    parser.add_argument("--codex-home", default=None, help="path to CODEX_HOME")
    parser.add_argument("--out", default=None, help="HTML output path")
    parser.add_argument("--json", dest="json_out", default=None, help="also write JSON")
    parser.add_argument("--md", dest="md_out", default=None, help="also write Markdown")
    parser.add_argument("--stdout", choices=("html", "json", "md", "summary"),
                        default="summary", help="print to stdout")
    parser.add_argument("--detailed", action="store_true", help="更详细分析：每次需人工确认，仅写入指定的本地文件")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error('--limit must be nonnegative')
    home = codex_home(args.codex_home)
    if args.detailed:
        if not (args.out or args.json_out):
            parser.error('--detailed requires --out or --json for a local report')
        if args.stdout != 'summary':
            parser.error('--detailed cannot print report contents to stdout')
        range_text = '全部会话' if args.limit == 0 else f'最近 {args.limit} 个会话'
        outputs = '、'.join(str(Path(p).expanduser()) for p in (args.out, args.json_out, args.md_out) if p)
        scope = f'来源：{home / "sessions"}（{range_text}）\n输出：{outputs}\n同名输出文件将被替换。'
        if not request_detailed_consent(scope):
            print('已取消；没有读取详细会话内容。', file=sys.stderr)
            return 2

    limit = None if args.limit == 0 else args.limit
    sessions = load_sessions(home, limit=limit, detailed=args.detailed)
    payload = build_payload(aggregate(sessions), home, detailed=args.detailed)
    destinations = [Path(p).expanduser().resolve() for p in (args.out, args.json_out, args.md_out) if p]
    source_paths = {s.path.resolve() for s in sessions}
    protected_roots = [(home / name).resolve() for name in ('sessions', 'archived_sessions')]
    if (len(set(destinations)) != len(destinations)
            or any(p in source_paths or any(p.is_relative_to(root) for root in protected_roots) for p in destinations)):
        parser.error('输出路径必须各不相同，且不能位于会话数据目录内')

    if args.out:
        out = Path(args.out).expanduser()
        write_private(out, render_html(payload))
        print(f"HTML 报告: {out}", file=sys.stderr)

    if args.json_out:
        out = Path(args.json_out).expanduser()
        write_private(out, json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"JSON: {out}", file=sys.stderr)

    if args.md_out:
        out = Path(args.md_out).expanduser()
        write_private(out, render_markdown(payload))
        print(f"Markdown: {out}", file=sys.stderr)

    if args.stdout == "html":
        print(render_html(payload))
    elif args.stdout == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.stdout == "md":
        print(render_markdown(payload))
    else:
        totals = payload["totals"]
        cache = payload["cache_hit_rate"]
        print(f"Codex 会话数: {payload['session_count']}（{payload['turns_total']} 轮）")
        if payload['usage_available']:
            print(f"计费 Token : {totals['total']:,} (输入 {totals['input']:,} / 输出 {totals['output']:,})")
        else:
            print('计费 Token : 未知')
        print(f"预估成本   : {human_usd(payload['cost_usd'])}"
              + (f"（{payload['unpriced_sessions']} 个会话模型未知，未计价）"
                 if payload['unpriced_sessions'] else ""))
        print(f"缓存命中率 : {f'{cache * 100:.1f}%' if cache is not None else '—'}")
        if payload["models"]:
            print("模型       : " + ", ".join(f"{k}×{v}" for k, v in payload["models"].items()))
        if payload["tools"]:
            print("工具       : " + ", ".join(
                f"{k}×{v}" for k, v in list(payload["tools"].items())[:8]))
        print(f"工具调用次数: {payload['tool_calls_total']} | 上下文压缩: {payload['compactions_total']}")
        tm = payload["timing"]
        if tm["turn_total"] or tm["tool_total"]:
            split = tm["model_vs_tool"]
            print(
                f"耗时       : 轮次合计 {human_duration(tm['turn_total'])}"
                f"（工具各次耗时之和 {human_duration(tm['tool_total'])}，可能重叠）"
            )
            if tm["avg_turn"] is not None:
                print(
                    f"            平均每轮 {human_duration(tm['avg_turn'])}"
                    f" | 平均每工具 {human_duration(tm['avg_tool'])}"
                )
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
