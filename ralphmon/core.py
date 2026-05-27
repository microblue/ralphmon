"""Discovery, status, and control of ralph-claude-code projects."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psutil

HOME = Path.home()
SEARCH_ROOTS = [HOME]
SEARCH_MAX_DEPTH = 3
# Skip these path components entirely — upstream ralph repo's bundled examples
# show up as .ralph projects too, but they're not user projects.
SKIP_PATH_COMPONENTS = {"ralph-claude-code", "0-archives", "node_modules", ".venv", "venv"}
RALPH_CMD = shutil.which("ralph") or str(HOME / ".local/bin/ralph")


@dataclass
class RalphProject:
    path: Path
    ralph_dir: Path
    status: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    pid: int | None = None
    pgid: int | None = None
    paused: bool = False
    tmux_session: str | None = None
    last_log_mtime: float | None = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def running(self) -> bool:
        return self.pid is not None

    @property
    def loop_count(self) -> int:
        return int(self.status.get("loop_count") or 0)

    @property
    def state_label(self) -> str:
        if self.paused:
            return "paused"
        if self.running:
            prog = (self.progress.get("status") or "").lower()
            if prog == "executing":
                return "executing"
            return "running"
        return "idle"

    @property
    def last_action(self) -> str:
        return self.status.get("last_action") or ""

    @property
    def calls_used(self) -> str:
        s = self.status
        if "calls_made_this_hour" in s and "max_calls_per_hour" in s:
            return f"{s['calls_made_this_hour']}/{s['max_calls_per_hour']}"
        return ""

    @property
    def live_log(self) -> Path:
        return self.ralph_dir / "live.log"

    @property
    def last_seen(self) -> str:
        ts = self.status.get("timestamp")
        if not ts:
            return ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - dt
            secs = int(delta.total_seconds())
            if secs < 60:
                return f"{secs}s ago"
            if secs < 3600:
                return f"{secs // 60}m ago"
            if secs < 86400:
                return f"{secs // 3600}h ago"
            return f"{secs // 86400}d ago"
        except Exception:
            return ts


def discover_projects() -> list[RalphProject]:
    """Find every <dir>/.ralph/PROMPT.md under SEARCH_ROOTS."""
    found: dict[Path, RalphProject] = {}
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for ralph_dir in _walk_ralph_dirs(root, SEARCH_MAX_DEPTH):
            project = ralph_dir.parent
            # The home dir itself is ralph install root, not a project.
            if project == HOME:
                continue
            if not (ralph_dir / "PROMPT.md").exists():
                continue
            if project in found:
                continue
            if any(part in SKIP_PATH_COMPONENTS for part in project.parts):
                continue
            found[project] = RalphProject(path=project, ralph_dir=ralph_dir)
    projects = list(found.values())
    _attach_runtime_state(projects)
    return sorted(projects, key=lambda p: (not p.running, p.name.lower()))


def _walk_ralph_dirs(root: Path, max_depth: int):
    """Depth-limited walk yielding any directory named '.ralph'."""
    root = root.resolve()
    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    name = entry.name
                    if name == ".ralph":
                        yield Path(entry.path)
                        continue
                    if name.startswith(".") or name in {"node_modules", "__pycache__", "venv", ".venv", "dist", "build"}:
                        continue
                    if depth < max_depth:
                        stack.append((Path(entry.path), depth + 1))
        except (PermissionError, FileNotFoundError):
            continue


def _attach_runtime_state(projects: list[RalphProject]) -> None:
    by_path: dict[Path, RalphProject] = {p.path: p for p in projects}

    # Load files.
    for p in projects:
        p.status = _read_json(p.ralph_dir / "status.json")
        p.progress = _read_json(p.ralph_dir / "progress.json")
        try:
            p.last_log_mtime = (p.ralph_dir / "live.log").stat().st_mtime
        except OSError:
            p.last_log_mtime = None

    # Scan for running ralph_loop.sh processes; bind them to projects via cwd.
    for proc in psutil.process_iter(["pid", "cmdline", "status"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if not _is_ralph_loop(cmdline):
                continue
            cwd = Path(proc.cwd()).resolve()
        except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
            continue
        # Walk up from cwd to find a matching project dir.
        cand = cwd
        for _ in range(6):
            if cand in by_path:
                proj = by_path[cand]
                proj.pid = proc.info["pid"]
                try:
                    proj.pgid = os.getpgid(proj.pid)
                except (ProcessLookupError, PermissionError):
                    proj.pgid = None
                proj.paused = proc.info.get("status") == psutil.STATUS_STOPPED
                break
            if cand.parent == cand:
                break
            cand = cand.parent

    # Detect tmux sessions launched by ralphmon for these projects.
    sessions = _list_tmux_sessions()
    for p in projects:
        marker = p.ralph_dir / ".ralphmon_session"
        if marker.exists():
            name = marker.read_text().strip()
            if name in sessions:
                p.tmux_session = name
            else:
                # Stale marker.
                try:
                    marker.unlink()
                except OSError:
                    pass


def _is_ralph_loop(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    joined = " ".join(cmdline)
    return "ralph_loop.sh" in joined


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _list_tmux_sessions() -> set[str]:
    try:
        out = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if out.returncode != 0:
        return set()
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


# ───────────────────────── control ─────────────────────────

def start(project: RalphProject) -> tuple[bool, str]:
    if project.running:
        return False, f"already running (pid {project.pid})"
    if not Path(RALPH_CMD).exists():
        return False, f"ralph CLI not found at {RALPH_CMD}"
    if shutil.which("tmux") is None:
        return False, "tmux is required to launch ralph headlessly"

    session = f"ralphmon-{project.name}-{int(datetime.now().timestamp())}"
    cmd = [
        "tmux", "new-session", "-d",
        "-s", session,
        "-c", str(project.path),
        f"{RALPH_CMD} -v --auto-reset-circuit",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return False, f"tmux failed: {proc.stderr.strip() or proc.stdout.strip()}"

    marker = project.ralph_dir / ".ralphmon_session"
    try:
        marker.write_text(session)
    except OSError:
        pass
    return True, f"started in tmux session {session}"


def pause(project: RalphProject) -> tuple[bool, str]:
    if not project.running:
        return False, "not running"
    if project.paused:
        return False, "already paused"
    try:
        _signal_group(project, signal.SIGSTOP)
    except ProcessLookupError:
        return False, "process gone"
    return True, "paused (SIGSTOP)"


def resume(project: RalphProject) -> tuple[bool, str]:
    if not project.running:
        return False, "not running"
    if not project.paused:
        return False, "not paused"
    try:
        _signal_group(project, signal.SIGCONT)
    except ProcessLookupError:
        return False, "process gone"
    return True, "resumed (SIGCONT)"


def stop(project: RalphProject) -> tuple[bool, str]:
    msgs = []
    killed_any = False

    if project.running:
        try:
            # If paused, must SIGCONT first or SIGTERM never delivers.
            if project.paused:
                _signal_group(project, signal.SIGCONT)
            _signal_group(project, signal.SIGTERM)
            killed_any = True
            msgs.append(f"SIGTERM → pid {project.pid}")
            # Give it 3s to exit, then SIGKILL.
            try:
                psutil.Process(project.pid).wait(timeout=3)
            except psutil.TimeoutExpired:
                _signal_group(project, signal.SIGKILL)
                msgs.append("SIGKILL after timeout")
            except psutil.NoSuchProcess:
                pass
        except ProcessLookupError:
            pass

    # Kill any tmux session that owns this project.
    for sess in _list_tmux_sessions():
        if sess == project.tmux_session or sess.startswith(f"ralphmon-{project.name}-"):
            subprocess.run(["tmux", "kill-session", "-t", sess],
                           capture_output=True)
            msgs.append(f"killed tmux session {sess}")
            killed_any = True

    marker = project.ralph_dir / ".ralphmon_session"
    if marker.exists():
        try:
            marker.unlink()
        except OSError:
            pass

    if not killed_any:
        return False, "nothing to stop"
    return True, "; ".join(msgs)


def restart(project: RalphProject) -> tuple[bool, str]:
    ok, msg = stop(project) if project.running else (True, "skip stop")
    if not ok and project.running:
        return False, f"stop failed: {msg}"
    return start(project)


def delete(project: RalphProject) -> tuple[bool, str]:
    """Remove the .ralph dir. Stops first if running."""
    if project.running:
        ok, msg = stop(project)
        if not ok:
            return False, f"could not stop first: {msg}"
    try:
        shutil.rmtree(project.ralph_dir)
    except OSError as e:
        return False, f"rmtree failed: {e}"
    return True, f"deleted {project.ralph_dir}"


def _signal_group(project: RalphProject, sig: int) -> None:
    """Signal the whole process group if we have a pgid, else the pid."""
    if project.pgid:
        os.killpg(project.pgid, sig)
    elif project.pid:
        os.kill(project.pid, sig)
