#!/usr/bin/env python3
"""Tests for codex-context-insight. Run: python3 -m unittest discover -s tests -v

Covers the three properties the plugin must never lose:
  1. Metadata only - prompt/answer/tool argument text never enters the model.
  2. Correct accounting - cumulative token snapshots are not double-counted.
  3. Never fatal - malformed or unknown rollout data cannot raise.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from context_core import (  # noqa: E402
    Session,
    aggregate,
    find_rollouts,
    load_sessions,
    parse_rollout,
    session_id_from_path,
)

SID = "019eaadc-d12d-7f33-84e8-cd56bba38544"


def write_rollout(root: Path, name: str, lines: list) -> Path:
    path = root / name
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
    return path


def token_event(
    total: int,
    *,
    window: int = 400_000,
    context: int = 0,
    delta_input: int = 0,
    delta_output: int = 0,
) -> dict:
    """A token_count line shaped like real Codex output.

    ``total`` drives total_token_usage (cumulative); ``delta_input`` drives
    last_token_usage (per request). Leaving ``context`` at 0 exercises the
    fallback where per-request input becomes the window estimate.
    """
    info = {
        "total_token_usage": {
            "input_tokens": total,
            "cached_input_tokens": total // 4,
            "output_tokens": total // 20,
            "reasoning_output_tokens": 10,
            "total_tokens": total + total // 20,
        },
        "model_context_window": window,
    }
    if delta_input or delta_output:
        info["last_token_usage"] = {
            "input_tokens": delta_input,
            "cached_input_tokens": delta_input // 2,
            "output_tokens": delta_output,
            "reasoning_output_tokens": delta_output // 2,
            "total_tokens": delta_input + delta_output,
        }
    if context:
        info["context_tokens"] = context
    return {
        "timestamp": "2026-11-14T02:00:05.000Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    }


class TestPrivacy(unittest.TestCase):
    """Prompt text, answers and tool arguments must never survive parsing."""

    def test_sensitive_text_is_not_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_rollout(
                root,
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [
                    {
                        "timestamp": "2026-11-14T02:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": SID, "cwd": "/root/demo"},
                    },
                    {
                        "timestamp": "2026-11-14T02:00:01.000Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "TOPSECRET_PROMPT"},
                    },
                    {
                        "timestamp": "2026-11-14T02:00:02.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "shell",
                            "arguments": "TOPSECRET_ARGS",
                        },
                    },
                    {
                        "timestamp": "2026-11-14T02:00:03.000Z",
                        "type": "response_item",
                        "payload": {"type": "message", "content": "TOPSECRET_ANSWER"},
                    },
                    token_event(1000),
                ],
            )
            session = parse_rollout(path)
            blob = json.dumps(session.__dict__, default=str)
            for secret in ("TOPSECRET_PROMPT", "TOPSECRET_ARGS", "TOPSECRET_ANSWER"):
                self.assertNotIn(secret, blob)

    def test_tool_names_are_kept_but_not_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [
                    {
                        "timestamp": "2026-11-14T02:00:02.000Z",
                        "type": "response_item",
                        "payload": {"type": "function_call", "name": "shell"},
                    },
                ],
            )
            session = parse_rollout(path)
            self.assertEqual(session.tool_histogram(), {"shell": 1})


class TestAccounting(unittest.TestCase):
    """Cumulative snapshots must be taken, not summed."""

    def test_last_snapshot_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [token_event(1000), token_event(5000)],
            )
            session = parse_rollout(path)
            self.assertEqual(len(session.snapshots), 2)
            self.assertEqual(session.totals["input"], 5000)

    def test_cache_hit_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [token_event(4000)],
            )
            session = parse_rollout(path)
            # cached = total // 4 = 1000, input = 4000
            self.assertAlmostEqual(session.totals["cached"] / session.totals["input"], 0.25)

    def test_context_window_and_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [token_event(1000, window=200_000, context=50_000)],
            )
            session = parse_rollout(path)
            self.assertEqual(session.context_window, 200_000)
            # latest reads per-request usage; with no delta it is None, and the
            # cumulative snapshot must never be used as window pressure.
            self.assertIsNone(session.latest)

    def test_cumulative_snapshot_is_not_window_pressure(self) -> None:
        """A running total over every request must not look like one window."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [token_event(570_171, window=400_000, delta_input=19_876, delta_output=500)],
            )
            session = parse_rollout(path)
            snapshot = session.snapshots[-1]
            self.assertEqual(snapshot.context_size, 0, "cumulative total is not context")
            self.assertEqual(session.deltas[-1].context_size, 19_876)
            self.assertAlmostEqual(session.latest.usage_ratio, 19_876 / 400_000)

    def test_delta_input_derives_context_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [
                    token_event(1000, delta_input=8000),
                    token_event(9000, delta_input=9000),
                ],
            )
            session = parse_rollout(path)
            series = session.context_series()
            self.assertEqual(len(series), 2)
            self.assertEqual(session.peak_context, 9000)

    def test_unknown_model_has_no_cost(self) -> None:
        model = Session(path=Path("/dev/null"))
        model.models = {"totally-unknown-model": 1}
        self.assertIsNone(model.cost())

    def test_known_model_cost(self) -> None:
        session = Session(path=Path("/dev/null"))
        session.models = {"gpt-6.1-sol": 1}
        from context_core import Usage

        session.snapshots = [
            Usage(
                timestamp=0,
                input_tokens=1_000_000,
                cached_input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                total_tokens=1_000_000,
                context_window=400_000,
                context_used=1000,
                cumulative=True,
            )
        ]
        # gpt-6.1-sol input price is 2.50 USD per 1M tokens.
        self.assertAlmostEqual(session.cost(), 2.50)

    def test_aggregate_skips_unpriced(self) -> None:
        priced = Session(path=Path("/a"))
        priced.models = {"gpt-6.1-sol": 1}
        unpriced = Session(path=Path("/b"))
        unpriced.models = {"mystery": 1}
        agg = aggregate([priced, unpriced])
        self.assertEqual(agg.unpriced_sessions, 1)
        self.assertEqual(agg.cost, 0.0)


class TestRobustness(unittest.TestCase):
    """Bad input must degrade gracefully, never raise."""

    def test_malformed_lines_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"rollout-2026-11-14T02-00-00-{SID}.jsonl"
            path.write_text("not json\n{broken\n", encoding="utf-8")
            session = parse_rollout(path)
            self.assertGreaterEqual(session.unknown_lines.get("malformed", 0), 2)

    def test_unknown_line_types_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [{"type": "some_future_thing", "payload": {"x": 1}}],
            )
            session = parse_rollout(path)
            self.assertIn("some_future_thing", session.unknown_lines)

    def test_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"rollout-2026-11-14T02-00-00-{SID}.jsonl"
            path.write_text("", encoding="utf-8")
            session = parse_rollout(path)
            self.assertEqual(session.snapshots, [])
            self.assertEqual(session.deltas, [])

    def test_missing_file(self) -> None:
        session = parse_rollout(Path("/nonexistent/rollout-x.jsonl"))
        self.assertEqual(session.session_id, None)

    def test_no_sessions_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(find_rollouts(Path(tmp)), [])
            self.assertEqual(load_sessions(Path(tmp)), [])

    def test_binary_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"rollout-2026-11-14T02-00-00-{SID}.jsonl"
            path.write_bytes(b"\x00\xff\xfe garbage \n")
            session = parse_rollout(path)  # must not raise
            self.assertIsInstance(session, Session)


class TestSessionId(unittest.TestCase):
    def test_uuid_extracted_from_filename(self) -> None:
        path = Path(f"rollout-2026-06-09T13-30-58-{SID}.jsonl")
        self.assertEqual(session_id_from_path(path), SID)

    def test_no_uuid(self) -> None:
        self.assertIsNone(session_id_from_path(Path("rollout-weird.jsonl")))


class TestCli(unittest.TestCase):
    """The report builder must exit 0 on every supported invocation."""

    def test_builds_all_outputs(self) -> None:
        import report

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            day = home / "sessions/2026/11/14"
            day.mkdir(parents=True)
            write_rollout(
                day,
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [
                    {
                        "timestamp": "2026-11-14T02:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": SID, "cwd": "/root/demo"},
                    },
                    token_event(2000),
                ],
            )
            out = Path(tmp) / "r.html"
            jout = Path(tmp) / "r.json"
            mout = Path(tmp) / "r.md"
            code = report.main(
                [
                    "--codex-home",
                    str(home),
                    "--out",
                    str(out),
                    "--json",
                    str(jout),
                    "--md",
                    str(mout),
                    "--stdout",
                    "summary",
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("上下文洞察", out.read_text(encoding="utf-8"))
            payload = json.loads(jout.read_text(encoding="utf-8"))
            self.assertEqual(payload["session_count"], 1)
            self.assertEqual(payload["privacy"], "metadata_only")
            self.assertTrue(mout.exists())

    def test_empty_home_still_renders(self) -> None:
        import report

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "empty.html"
            code = report.main(
                ["--codex-home", str(Path(tmp) / "nope"), "--out", str(out), "--stdout", "summary"]
            )
            self.assertEqual(code, 0)
            self.assertIn("没有找到 Codex 会话文件", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTiming(unittest.TestCase):
    """Timing must come from Codex's own stamps, not from guesswork."""

    def _turn(self, started: float, completed: float, duration_ms: int = 0) -> list:
        return [
            {
                "timestamp": "2026-11-14T02:00:00.000Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "started_at": started},
            },
            {
                "timestamp": "2026-11-14T02:00:30.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "completed_at": completed,
                    "duration_ms": duration_ms,
                },
            },
        ]

    def test_turn_duration_from_explicit_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                self._turn(1000.0, 1060.0),
            )
            session = parse_rollout(path)
            self.assertEqual(session.timing.turn_count, 1)
            self.assertAlmostEqual(session.timing.turn_total, 60.0)

    def test_duration_ms_wins_over_clock_delta(self) -> None:
        """Codex's own duration_ms is authoritative when present."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                self._turn(1000.0, 1060.0, duration_ms=45_500),
            )
            session = parse_rollout(path)
            self.assertAlmostEqual(session.timing.turn_total, 45.5)

    def test_tool_duration_pairs_by_call_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [
                    {
                        "timestamp": "2026-11-14T02:00:00.000Z",
                        "type": "response_item",
                        "payload": {"type": "function_call", "name": "shell", "call_id": "a"},
                    },
                    {
                        "timestamp": "2026-11-14T02:00:07.000Z",
                        "type": "response_item",
                        "payload": {"type": "function_call_output", "call_id": "a"},
                    },
                ],
            )
            session = parse_rollout(path)
            self.assertEqual(session.timing.timed_tool_count, 1)
            self.assertAlmostEqual(session.timing.tool_total, 7.0)
            self.assertEqual(session.timing.tool_breakdown()["shell"]["count"], 1)

    def test_tool_without_output_has_no_duration(self) -> None:
        """An unfinished tool must not be counted as instant."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                [
                    {
                        "timestamp": "2026-11-14T02:00:00.000Z",
                        "type": "response_item",
                        "payload": {"type": "function_call", "name": "shell", "call_id": "z"},
                    },
                ],
            )
            session = parse_rollout(path)
            self.assertEqual(session.timing.tool_total, 0)
            self.assertEqual(session.timing.timed_tool_count, 0)

    def test_model_time_is_turn_minus_tool(self) -> None:
        from context_core import Aggregate

        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                f"rollout-2026-11-14T02-00-00-{SID}.jsonl",
                self._turn(1000.0, 1100.0, duration_ms=100_000)
                + [
                    {
                        "timestamp": "2026-11-14T02:00:00.000Z",
                        "type": "response_item",
                        "payload": {"type": "function_call", "name": "shell", "call_id": "a"},
                    },
                    {
                        "timestamp": "2026-11-14T02:00:30.000Z",
                        "type": "response_item",
                        "payload": {"type": "function_call_output", "call_id": "a"},
                    },
                ],
            )
            agg = Aggregate(sessions=[parse_rollout(path)])
            split = agg.model_vs_tool()
            self.assertAlmostEqual(split["turn"], 100.0)
            self.assertAlmostEqual(split["tools"], 30.0)
            self.assertAlmostEqual(split["model"], 70.0)

    def test_model_time_never_negative(self) -> None:
        """Overlapping tool records must not produce negative model time."""
        from context_core import Aggregate, Session, TimingStats, ToolTiming, TurnTiming

        session = Session(path=Path("/x"))
        session.timing = TimingStats(
            turns=[TurnTiming(started_at=0.0, completed_at=10.0)],
            tools=[
                ToolTiming(name="a", started_at=0.0, completed_at=8.0),
                ToolTiming(name="b", started_at=0.0, completed_at=8.0),
            ],
        )
        split = Aggregate(sessions=[session]).model_vs_tool()
        self.assertGreaterEqual(split["model"], 0.0)
