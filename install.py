#!/usr/bin/env python3
"""Install codex-context-insight into a Codex installation.

Two install routes, matching how Codex loads plugins:

  personal  ~/.codex/plugins/codex-context-insight  +  a personal marketplace
            entry at ~/.agents/plugins/marketplace.json.  This is the route the
            official docs describe and the one the Codex desktop app's Plugin
            Directory reads.

  copy      copy the plugin somewhere and print what to do next, for people who
            keep their own layout.

Both routes only copy files. Nothing is downloaded and nothing runs at install
time. Use --dry-run to see every path before anything is written.

Usage:
    python3 install.py                 # personal install into ~/.codex
    python3 install.py --dry-run       # show what would happen
    python3 install.py --codex-home D:/.codex
    python3 install.py --uninstall
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PLUGIN_NAME = "codex-context-insight"
MARKETPLACE_NAME = "local-personal"

# Everything that ships. Anything not listed here stays out of the install.
PAYLOAD = [
    "plugin.json",
    "mcp.json",
    "assets",
    "hooks",
    "scripts",
    "skills",
    "README.md",
]


def plugin_source() -> Path:
    return Path(__file__).resolve().parent


def default_codex_home() -> Path:
    import os

    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def marketplace_path() -> Path:
    return Path.home() / ".agents" / "plugins" / "marketplace.json"


def _ignore_python_cache(directory: str, names: list[str]) -> set[str]:
    """Keep bytecode caches from the author's machine out of the install."""
    return {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}


def copy_payload(src: Path, dest: Path, *, dry_run: bool) -> list[Path]:
    written: list[Path] = []
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    for entry in PAYLOAD:
        source = src / entry
        if not source.exists():
            print(f"  跳过（源缺失）: {entry}", file=sys.stderr)
            continue
        target = dest / entry
        if not dry_run:
            if source.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target, ignore=_ignore_python_cache)
            else:
                shutil.copy2(source, target)
        written.append(target)
    return written


def read_marketplace(path: Path, *, dry_run: bool = False) -> dict:
    if not path.is_file():
        return {
            "name": MARKETPLACE_NAME,
            "interface": {"displayName": "本机插件"},
            "plugins": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        data.setdefault("name", MARKETPLACE_NAME)
        data.setdefault("interface", {"displayName": "本机插件"})
        data.setdefault("plugins", [])
        if not isinstance(data["plugins"], list):
            data["plugins"] = []
        return data
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"  ! 现有 marketplace.json 无法解析（{exc}）", file=sys.stderr)
        if not dry_run:
            backup = path.with_suffix(".json.bak-codex-context")
            shutil.copy2(path, backup)
            print(f"    备份: {backup}", file=sys.stderr)
        return {
            "name": MARKETPLACE_NAME,
            "interface": {"displayName": "本机插件"},
            "plugins": [],
        }


def upsert_marketplace_entry(data: dict, plugin_dir: Path) -> None:
    plugins = [p for p in data["plugins"] if p.get("name") != PLUGIN_NAME]
    plugins.append(
        {
            "name": PLUGIN_NAME,
            "source": {"path": str(plugin_dir)},
        }
    )
    data["plugins"] = plugins


def install(codex_home: Path, *, dry_run: bool) -> int:
    src = plugin_source()
    manifest = src / "plugin.json"
    if not manifest.is_file():
        print(f"错误：{manifest} 不存在，请在插件目录内运行本脚本。", file=sys.stderr)
        return 2

    target = codex_home / "plugins" / PLUGIN_NAME
    market = marketplace_path()

    print("将安装到:")
    print(f"  插件   {target}")
    print(f" 市场   {market}")

    written = copy_payload(src, target, dry_run=dry_run)
    print(f" 复制 {len(written)} 项")

    data = read_marketplace(market, dry_run=dry_run)
    upsert_marketplace_entry(data, target)
    if not dry_run:
        market.parent.mkdir(parents=True, exist_ok=True)
        market.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f" 市场条目已写入（共 {len(data['plugins'])} 个插件）")

    if dry_run:
        print("\n[dry-run] 没有写入任何文件。")
        return 0

    print(
        "\n完成。（作者：凪梦尘）接下来：\n"
        "  1. 完全退出并重新打开 Codex 桌面应用（重启后才会扫描 marketplace）\n"
        "  2. 打开 Plugin Directory → 选择「本机插件」→ 安装 codex-context-insight\n"
        "  3. 首次运行钩子时 Codex 会请求信任，请审阅并允许\n"
        "  4. 报告默认生成在插件数据目录，也可随时手动运行：\n"
        f"     python3 \"{target}/scripts/report.py\" --out ~/codex-context-report.html"
    )
    return 0


def uninstall(codex_home: Path, *, dry_run: bool) -> int:
    target = codex_home / "plugins" / PLUGIN_NAME
    market = marketplace_path()

    if target.exists():
        print(f"删除插件目录: {target}")
        if not dry_run:
            shutil.rmtree(target)
    else:
        print(f"插件目录不存在: {target}")

    if market.is_file():
        data = read_marketplace(market, dry_run=dry_run)
        before = len(data["plugins"])
        data["plugins"] = [p for p in data["plugins"] if p.get("name") != PLUGIN_NAME]
        if len(data["plugins"]) != before:
            print(f"从 marketplace 移除条目: {market}")
            if not dry_run:
                market.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        else:
            print("marketplace 中无该插件条目")

    if dry_run:
        print("\n[dry-run] 没有删除任何文件。")
    else:
        print("\n卸载完成。重启 Codex 生效。报告文件与钩子信任记录需手动清理。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Install {PLUGIN_NAME} into Codex.")
    parser.add_argument("--codex-home", default=None, help="CODEX_HOME (default ~/.codex)")
    parser.add_argument("--dry-run", action="store_true", help="show paths, write nothing")
    parser.add_argument("--uninstall", action="store_true", help="remove the plugin")
    args = parser.parse_args(argv)

    home = Path(args.codex_home).expanduser() if args.codex_home else default_codex_home()
    if args.uninstall:
        return uninstall(home, dry_run=args.dry_run)
    return install(home, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
