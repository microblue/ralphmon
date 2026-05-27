"""Textual TUI for ralphmon."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Label, ListItem, ListView, RichLog, Static

from . import core


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no modal."""

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "Cancel"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self.prompt, id="confirm-prompt")
            yield Label("[y] yes   [n] no   [esc] cancel", id="confirm-hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ConfigScreen(ModalScreen[Path | None]):
    """File picker for a ralph project's editable config files."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
    ]

    # Extensions treated as editable text.
    _TEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".toml", ".sh", ".json", ".template"}

    def __init__(self, project: core.RalphProject) -> None:
        super().__init__()
        self.project = project
        self.files: list[Path] = self._collect_files()

    def _collect_files(self) -> list[Path]:
        ralph_dir = self.project.ralph_dir
        found: list[Path] = []
        for name in ("PROMPT.md", "AGENT.md"):
            f = ralph_dir / name
            if f.exists():
                found.append(f)
        specs_dir = ralph_dir / "specs"
        if specs_dir.is_dir():
            for f in sorted(specs_dir.iterdir()):
                if f.is_file() and f.suffix in self._TEXT_SUFFIXES and f not in found:
                    found.append(f)
        _skip = {"status.json", "progress.json", "live.log", "watchdog.log",
                 ".call_count", ".token_count", ".last_reset", ".loop_start_sha",
                 ".circuit_breaker_state", ".circuit_breaker_history",
                 ".claude_session_id", ".exit_signals", ".response_analysis",
                 ".ralph_session", "ralphmon_session"}
        for f in sorted(ralph_dir.iterdir()):
            if f.name.startswith(".") and f.name not in (".ralphrc",):
                continue
            if f.name in _skip:
                continue
            if f.is_file() and f.suffix in self._TEXT_SUFFIXES and f not in found:
                found.append(f)
        return found

    def compose(self) -> ComposeResult:
        items = [
            ListItem(Label(
                f"{'  (PROMPT)' if f.name == 'PROMPT.md' else '  (AGENT) ' if f.name == 'AGENT.md' else '          '}"
                f"  {f.relative_to(self.project.path)}"
            ), id=f"file-{i}")
            for i, f in enumerate(self.files)
        ]
        with Vertical(id="config-box"):
            yield Label(f"config files — {self.project.name}", id="config-title")
            if items:
                yield ListView(*items, id="config-list")
                yield Label("[enter / click] open in $EDITOR   [esc] cancel", id="config-hint")
            else:
                yield Label("no editable config files found", id="config-hint")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Fires on Enter keypress AND mouse click — the reliable way in Textual.
        idx = event.list_view.index
        if idx is not None and idx < len(self.files):
            self.dismiss(self.files[idx])

    def action_cancel(self) -> None:
        self.dismiss(None)


class RalphMonApp(App):
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #table-pane { width: 60%; border-right: solid $accent; }
    #log-pane { width: 1fr; }
    #log-title { dock: top; padding: 0 1; background: $boost; color: $text; }
    DataTable { height: 1fr; }
    RichLog { height: 1fr; background: $surface; }
    #confirm-box {
        align: center middle;
        width: 60;
        height: 7;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #confirm-prompt { text-align: center; }
    #confirm-hint { text-align: center; color: $text-muted; }
    #config-box {
        align: center middle;
        width: 72;
        height: auto;
        max-height: 24;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #config-title { text-align: center; padding-bottom: 1; color: $accent; }
    #config-list { height: auto; max-height: 16; }
    #config-hint { text-align: center; color: $text-muted; padding-top: 1; }
    #status-bar { dock: bottom; height: 1; padding: 0 1; background: $boost; color: $text; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh", "refresh now"),
        Binding("s", "start", "start"),
        Binding("p", "pause_resume", "pause/resume"),
        Binding("shift+r", "restart", "restart"),
        Binding("x", "stop", "stop"),
        Binding("d", "delete", "delete"),
        Binding("a", "attach", "attach tmux"),
        Binding("c", "config", "config files"),
        Binding("l", "cycle_log", "cycle log"),
    ]

    REFRESH_INTERVAL = 2.0
    LOG_TAIL_INTERVAL = 0.5
    LOG_INITIAL_BYTES = 16 * 1024  # show more context by default

    def __init__(self) -> None:
        super().__init__()
        self.projects: list[core.RalphProject] = []
        self.current_path: Path | None = None
        self._log_pos = 0
        self._log_path: Path | None = None
        self._log_label: str = ""
        self._log_sources: list[tuple[str, Path]] = []
        self._log_source_idx: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="table-pane"):
                yield DataTable(id="projects", cursor_type="row", zebra_stripes=True)
            with Vertical(id="log-pane"):
                yield Static("live log: (select a project)", id="log-title")
                yield RichLog(id="log", highlight=True, markup=False, max_lines=2000, wrap=False)
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("project", "state", "loops", "action", "calls", "last seen", "pid")
        self.refresh_projects()
        self.set_interval(self.REFRESH_INTERVAL, self.refresh_projects)
        self.set_interval(self.LOG_TAIL_INTERVAL, self._tail_log)

    # ───────── project list ─────────

    def refresh_projects(self) -> None:
        self.projects = core.discover_projects()
        table = self.query_one(DataTable)
        # Preserve selection by project path.
        prev_path = self._selected_path()
        table.clear()
        new_row_index = 0
        for i, p in enumerate(self.projects):
            state = p.state_label
            state_cell = self._color_state(state)
            table.add_row(
                p.name,
                state_cell,
                str(p.loop_count) if p.loop_count else "—",
                p.last_action or "—",
                p.calls_used or "—",
                p.last_seen or "—",
                str(p.pid) if p.pid else "—",
                key=str(p.path),
            )
            if prev_path and Path(prev_path) == p.path:
                new_row_index = i
        if self.projects:
            table.move_cursor(row=new_row_index)
            p = self.projects[new_row_index]
            if p.path != self.current_path:
                self._on_select(p)
            else:
                # Same project — refresh log sources without resetting which one we're viewing.
                self._load_log_sources(p, reset_idx=False)
        else:
            self._set_log_title("no ralph projects found")

    def _color_state(self, state: str) -> str:
        colors = {
            "executing": "[bold yellow]executing[/]",
            "running":   "[green]running[/]",
            "paused":    "[magenta]paused[/]",
            "idle":      "[dim]idle[/]",
        }
        return colors.get(state, state)

    def _selected_path(self) -> str | None:
        table = self.query_one(DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return row_key.value
        except Exception:
            return None

    def _selected_project(self) -> core.RalphProject | None:
        sel = self._selected_path()
        if not sel:
            return None
        for p in self.projects:
            if str(p.path) == sel:
                return p
        return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        path = event.row_key.value
        if not path:
            return
        for p in self.projects:
            if str(p.path) == path:
                self._on_select(p)
                break

    def _on_select(self, project: core.RalphProject) -> None:
        if self.current_path == project.path:
            return
        self.current_path = project.path
        self._load_log_sources(project, reset_idx=True)

    def _load_log_sources(self, project: core.RalphProject, reset_idx: bool = False) -> None:
        self._log_sources = project.available_logs()
        if reset_idx:
            self._log_source_idx = 0
        else:
            self._log_source_idx = min(self._log_source_idx, len(self._log_sources) - 1)
        label, path = self._log_sources[self._log_source_idx]
        self._switch_log(label, path, project)

    def _switch_log(self, label: str, path: Path, project: core.RalphProject) -> None:
        self._log_label = label
        self._log_path = path
        self._log_pos = 0
        log = self.query_one(RichLog)
        log.clear()
        total = len(self._log_sources)
        idx = self._log_source_idx + 1
        self._set_log_title(
            f"[{idx}/{total}] {label}: {project.name}  "
            f"[tab=cycle]  ({path})"
        )
        self._tail_log(initial=True)

    def _set_log_title(self, text: str) -> None:
        self.query_one("#log-title", Static).update(text)

    def _set_status(self, text: str) -> None:
        self.query_one("#status-bar", Static).update(text)

    # ───────── live log tail ─────────

    def _tail_log(self, initial: bool = False) -> None:
        if self._log_path is None:
            return
        path = self._log_path
        if not path.exists():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        log = self.query_one(RichLog)
        if initial:
            start = max(0, size - self.LOG_INITIAL_BYTES)
            self._log_pos = start
        elif size < self._log_pos:
            log.write("─── (log rotated) ───")
            self._log_pos = 0
        if size == self._log_pos:
            return
        try:
            with path.open("rb") as f:
                f.seek(self._log_pos)
                chunk = f.read(size - self._log_pos)
            self._log_pos = size
        except OSError:
            return
        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            log.write(line)

    # ───────── actions ─────────

    def action_cycle_log(self) -> None:
        p = self._selected_project()
        if not p or not self._log_sources:
            return
        self._log_source_idx = (self._log_source_idx + 1) % len(self._log_sources)
        label, path = self._log_sources[self._log_source_idx]
        self._switch_log(label, path, p)

    def action_refresh(self) -> None:
        self.refresh_projects()
        self._set_status("refreshed")

    def action_start(self) -> None:
        p = self._selected_project()
        if not p: return
        ok, msg = core.start(p)
        self._notify_op("start", p, ok, msg)

    def action_pause_resume(self) -> None:
        p = self._selected_project()
        if not p: return
        if p.paused:
            ok, msg = core.resume(p)
            op = "resume"
        else:
            ok, msg = core.pause(p)
            op = "pause"
        self._notify_op(op, p, ok, msg)

    def action_restart(self) -> None:
        p = self._selected_project()
        if not p: return

        def _maybe(confirmed: bool) -> None:
            if not confirmed: return
            ok, msg = core.restart(p)
            self._notify_op("restart", p, ok, msg)

        self.push_screen(ConfirmScreen(f"restart ralph in {p.name}?"), _maybe)

    def action_stop(self) -> None:
        p = self._selected_project()
        if not p: return

        def _maybe(confirmed: bool) -> None:
            if not confirmed: return
            ok, msg = core.stop(p)
            self._notify_op("stop", p, ok, msg)

        self.push_screen(ConfirmScreen(f"stop ralph in {p.name}?"), _maybe)

    def action_delete(self) -> None:
        p = self._selected_project()
        if not p: return

        def _maybe(confirmed: bool) -> None:
            if not confirmed: return
            ok, msg = core.delete(p)
            self._notify_op("delete", p, ok, msg)

        self.push_screen(
            ConfirmScreen(f"DELETE {p.ralph_dir}? this removes all ralph state."),
            _maybe,
        )

    def action_attach(self) -> None:
        p = self._selected_project()
        if not p or not p.tmux_session:
            self._set_status("no tmux session to attach")
            return
        with self.suspend():
            subprocess.run(["tmux", "attach", "-t", p.tmux_session])

    def action_config(self) -> None:
        p = self._selected_project()
        if not p:
            return

        def _open(file_path: Path | None) -> None:
            if not file_path:
                return
            editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
            with self.suspend():
                subprocess.run([editor, str(file_path)])
            self._set_status(f"saved {file_path.relative_to(p.path)}")

        self.push_screen(ConfigScreen(p), _open)

    def _notify_op(self, op: str, project: core.RalphProject,
                   ok: bool, msg: str) -> None:
        prefix = "✓" if ok else "✗"
        self._set_status(f"{prefix} {op} [{project.name}]: {msg}")
        self.refresh_projects()


def run() -> None:
    RalphMonApp().run()
