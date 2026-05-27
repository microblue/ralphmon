"""CLI entry: `python -m ralphmon` opens the TUI; `--list` prints status."""
from __future__ import annotations

import argparse
import sys

from . import core


def _print_list() -> int:
    projects = core.discover_projects()
    if not projects:
        print("no ralph projects found", file=sys.stderr)
        return 1
    fmt = "{:<30} {:<10} {:>7} {:<12} {:<10} {:<12} {:>7}"
    print(fmt.format("project", "state", "loops", "action", "calls", "last seen", "pid"))
    print("─" * 96)
    for p in projects:
        print(fmt.format(
            p.name[:30], p.state_label, p.loop_count or "—",
            (p.last_action or "—")[:12], p.calls_used or "—",
            p.last_seen or "—", p.pid or "—",
        ))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ralphmon",
                                     description="Monitor and control ralph-claude-code projects")
    parser.add_argument("--list", action="store_true",
                        help="print one-shot status table and exit (no TUI)")
    args = parser.parse_args(argv)

    if args.list:
        return _print_list()

    from . import ui
    ui.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
