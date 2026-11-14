#!/usr/bin/env python3
"""Codex hook: refresh the context-insight report on session lifecycle events.

Codex passes one JSON object on stdin for every command hook. The only fields
we rely on are:

    hook_event_name   the event being handled
    transcript_path   path to this session's rollout JSONL (may be null)
    session_id        current session id
    cwd               working directory

Everything else is ignored. This hook never reads the transcript's content
itself; it only tells the report builder which file is the newest one.

Design rules:
  * Never fail the turn. Any exception exits 0 with no output.
  * Stay quiet. Only ``SessionEnd``/``Stop`` write anything, and only to the
    plugin data directory, never to stdout (stdout is model-visible).
  * Bounded work. Reports are regenerated at most once per N seconds so a long
    session cannot spawn a build per hook event.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Minimum seconds between report rebuilds triggered by hooks.
REBUILD_INTERVAL_SECONDS = 120

# Events that are worth a rebuild. SessionStart is included so the report
# appears as soon as Codex opens a session.
TRIGGER_EVENTS = {
    "SessionStart",
    "SessionEnd",
    "Stop",
    "PreCompact",
    "PostCompact",
}


def plugin_root() -> Path:
    """PLUGIN_ROOT is set by Codex; fall back to this file's parent's parent."""
    env = os.environ.get("PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def plugin_data() -> Path:
    """Writable data dir. PLUGIN_DATA is set by Codex; else a user cache path."""
    env = os.environ.get("PLUGIN_DATA")
    if env:
        path = Path(env)
    else:
        base = os.environ.get("XDG_CACHE_HOME")
        root = Path(base) if base else Path.home() / ".cache"
        path = root / "codex-context"
    path.mkdir(parents=True, exist_ok=True)
    return path


def should_rebuild(marker: Path) -> bool:
    try:
        last = marker.stat().st_mtime
    except OSError:
        return True
    return (time.time() - last) >= REBUILD_INTERVAL_SECONDS


def rebuild_report(root: Path, data: Path) -> None:
    """Run the report builder as a detached, silent subprocess."""
    script = root / "scripts" / "report.py"
    if not script.is_file():
        return
    out = data / "report.html"
    python = sys.executable or "python3"
    try:
        subprocess.run(
            [python, str(script), "--out", str(out), "--limit", "50"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
            cwd=str(root),
        )
    except (OSError, subprocess.SubprocessError):
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError, OSError):
        return 0

    if not isinstance(event, dict):
        return 0

    name = event.get("hook_event_name")
    if not isinstance(name, str) or name not in TRIGGER_EVENTS:
        return 0

    try:
        root = plugin_root()
        data = plugin_data()
        marker = data / ".last-rebuild"
        if not should_rebuild(marker):
            return 0
        rebuild_report(root, data)
        try:
            marker.touch()
        except OSError:
            pass
    except Exception:
        # A hook must never break the session.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
