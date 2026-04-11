"""Textual TUI dashboard for minimization jobs."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from rich.markup import escape as markup_escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog

from minimize.job import Job, JobStore
from minimize.monitor import count_imports, detect_phase, get_error_summary, get_output_loc
from minimize.process import is_running, kill_job


STATUS_STYLES = {
    "created": "dim",
    "cache_get": "blue",
    "building": "blue",
    "running": "blue",
    "completed": "green",
    "failed": "red",
    "killed": "dim",
    "unknown": "yellow",
}


def _short_toolchain(tc: str) -> str:
    """Shorten a toolchain name for display."""
    if ":" in tc:
        return tc.split(":")[-1]
    return tc


class ConfirmDialog(ModalScreen[bool]):
    """Simple yes/no confirmation dialog."""

    CSS = """
    ConfirmDialog {
        align: center middle;
    }
    #confirm-dialog {
        width: 50;
        height: auto;
        max-height: 10;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #confirm-message {
        text-align: center;
        margin-bottom: 1;
    }
    #confirm-hint {
        text-align: center;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self.message, id="confirm-message"),
            Label("[y] Yes  [n] No", id="confirm-hint"),
            id="confirm-dialog",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class NoteDialog(ModalScreen[str | None]):
    """Dialog for editing a job note."""

    CSS = """
    NoteDialog {
        align: center middle;
    }
    #note-dialog {
        width: 60;
        height: auto;
        max-height: 10;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #note-label {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, current_note: str = "") -> None:
        super().__init__()
        self.current_note = current_note

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Edit note (Enter to save, Escape to cancel):", id="note-label"),
            Input(value=self.current_note, id="note-input"),
            id="note-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#note-input", Input).focus()

    @on(Input.Submitted, "#note-input")
    def on_submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MinimizeDashboard(App):
    """TUI dashboard for minimization jobs."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #job-table {
        height: 1fr;
    }
    #log-pane {
        height: 12;
        border-top: solid $accent;
        display: none;
    }
    #log-pane.visible {
        display: block;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("enter", "open_output", "Open output"),
        Binding("o", "open_source", "Open source"),
        Binding("a", "attach", "Attach (Ctrl-b d to detach)"),
        Binding("l", "toggle_log", "Log"),
        Binding("k", "kill", "Kill"),
        Binding("d", "delete", "Delete"),
        Binding("r", "resume", "Resume"),
        Binding("e", "edit_note", "Note"),
    ]

    TITLE = "minimize"

    def __init__(self) -> None:
        super().__init__()
        self.store = JobStore()
        self.jobs: list[Job] = []
        self.log_job_id: str | None = None
        self._update_available = False

    def compose(self) -> ComposeResult:
        yield Header()
        table = DataTable(id="job-table", cursor_type="row")
        table.add_columns("Status", "File", "Project", "Cross TC", "Duration", "LOC", "Imports", "Note")
        yield table
        yield RichLog(id="log-pane", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_jobs()
        self.set_interval(5, self._refresh_jobs)
        self._check_for_updates()

    @work(thread=True)
    def _check_for_updates(self) -> None:
        """Check for updates in a background thread (non-blocking)."""
        from minimize.version import is_outdated
        outdated, _, _ = is_outdated()
        if outdated:
            self._update_available = True
            self.call_from_thread(self._show_update_binding)

    def _show_update_binding(self) -> None:
        self.notify("Update available! Press [bold]u[/bold] to update.", severity="information")
        # Dynamically bind 'u'
        self.bind("u", "update", description="Update available!")

    def action_update(self) -> None:
        if not self._update_available:
            self.notify("Already up to date", severity="information")
            return
        self.notify("Updating... (this may take a moment)")
        self._run_update()

    @work(thread=True)
    def _run_update(self) -> None:
        from minimize.version import REPO_URL
        result = subprocess.run(
            ["uv", "tool", "install", "--force", f"git+{REPO_URL}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            self.call_from_thread(
                self.notify, "Updated! Restart minimize to use the new version.", severity="information"
            )
        else:
            msg = result.stderr.strip().splitlines()[-1] if result.stderr else "Update failed"
            self.call_from_thread(self.notify, msg, severity="error")

    def _refresh_jobs(self) -> None:
        """Reload jobs and reconcile status with tmux.

        Uses per-job atomic updates instead of saving the entire snapshot.
        """
        self.jobs = self.store.load()

        for job in self.jobs:
            if job.status not in ("created", "cache_get", "building", "running"):
                continue
            try:
                running = is_running(job)
            except Exception:
                continue

            if not running:
                phase = detect_phase(job.workspace_path())
                if phase in ("completed", "failed"):
                    kwargs: dict[str, object] = {
                        "status": phase,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    }
                    if phase == "failed":
                        kwargs["error_summary"] = get_error_summary(job.workspace_path())
                    self.store.update(job.id, **kwargs)
                else:
                    self.store.update(
                        job.id,
                        status="unknown",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        error_summary="Session disappeared before exit marker",
                    )
            else:
                phase = detect_phase(job.workspace_path())
                if phase in ("cache_get", "building", "running") and phase != job.status:
                    self.store.update(job.id, status=phase)

        # Reload after updates
        self.jobs = self.store.load()
        self._update_table()

        if self.log_job_id:
            self._update_log_pane()

    def _update_table(self) -> None:
        table = self.query_one("#job-table", DataTable)
        try:
            cursor_row = table.cursor_row
        except Exception:
            cursor_row = 0

        table.clear()
        for job in self.jobs:
            style = STATUS_STYLES.get(job.status, "white")
            status = job.status.upper()

            ws = job.workspace_path()
            inf = getattr(job, "input_file", "Minimize/Target.lean")
            loc = get_output_loc(ws, inf)
            imp_count, imp_transitive = count_imports(ws, inf)

            loc_str = str(loc) if loc is not None else "-"
            imp_str = str(imp_count) if imp_transitive else f"{imp_count} direct"
            note_str = markup_escape(job.note or "")
            filename = markup_escape(Path(job.source_file).stem)
            project = markup_escape(job.project_name)
            cross_str = markup_escape(_short_toolchain(job.cross_toolchain)) if job.cross_toolchain else ""

            table.add_row(
                f"[{style}]{markup_escape(status)}[/{style}]",
                filename,
                project,
                cross_str,
                job.duration_str(),
                loc_str,
                imp_str,
                note_str,
                key=job.id,
            )

        if self.jobs and cursor_row < len(self.jobs):
            table.move_cursor(row=cursor_row)

    def _selected_job(self) -> Job | None:
        table = self.query_one("#job-table", DataTable)
        if not self.jobs:
            return None
        try:
            row = table.cursor_row
            if 0 <= row < len(self.jobs):
                return self.jobs[row]
        except Exception:
            pass
        return None

    def action_open_output(self) -> None:
        """Open the minimized output (or source if no output yet) in VS Code."""
        job = self._selected_job()
        if not job:
            return
        ws = job.workspace_path()
        inf = getattr(job, "input_file", "Minimize/Target.lean")
        out_file = ws / Path(inf).with_suffix(".out.lean")
        target = out_file if out_file.exists() else ws / inf
        try:
            subprocess.Popen(
                ["code", str(ws), str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.notify("VS Code ('code') not found", severity="error")

    def action_open_source(self) -> None:
        """Open the workspace's source file in VS Code."""
        job = self._selected_job()
        if not job:
            return
        ws = job.workspace_path()
        inf = getattr(job, "input_file", "Minimize/Target.lean")
        target = ws / inf
        try:
            subprocess.Popen(
                ["code", str(ws), str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.notify("VS Code ('code') not found", severity="error")

    def action_attach(self) -> None:
        job = self._selected_job()
        if not job:
            return
        if not is_running(job):
            self.notify("Job is not running", severity="warning")
            return
        with self.suspend():
            session = f"minimize-{job.id}"
            subprocess.run(["tmux", "attach-session", "-t", session])

    def action_toggle_log(self) -> None:
        job = self._selected_job()
        if not job:
            return
        log_pane = self.query_one("#log-pane", RichLog)
        if self.log_job_id == job.id and log_pane.has_class("visible"):
            log_pane.remove_class("visible")
            self.log_job_id = None
        else:
            self.log_job_id = job.id
            log_pane.add_class("visible")
            self._update_log_pane()

    def _update_log_pane(self) -> None:
        if not self.log_job_id:
            return
        job = self.store.get(self.log_job_id)
        if not job:
            return
        log_pane = self.query_one("#log-pane", RichLog)
        log_file = job.workspace_path() / "minimize.log"
        log_pane.clear()
        if log_file.exists():
            try:
                # Bounded tail read — read last 8KB instead of entire file
                with open(log_file, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 8192))
                    tail = f.read().decode("utf-8", errors="replace")
                for line in tail.splitlines()[-50:]:
                    log_pane.write(line.rstrip())
            except OSError:
                log_pane.write("[red]Could not read log file[/red]")
        else:
            log_pane.write("[dim]No log file yet[/dim]")

    def action_kill(self) -> None:
        job = self._selected_job()
        if not job:
            return
        if not is_running(job):
            self.notify("Job is not running", severity="warning")
            return

        def on_confirm(confirmed: bool) -> None:
            if not confirmed or not job:
                return
            killed = kill_job(job)
            if killed:
                self.store.update(
                    job.id,
                    status="killed",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                self.notify(f"Killed job {job.id}")
            else:
                # Session already gone — let reconciliation handle status
                self.notify("Session already ended", severity="warning")
            self._refresh_jobs()

        self.push_screen(ConfirmDialog(f"Kill job {job.id} ({Path(job.source_file).stem})?"), on_confirm)

    def action_delete(self) -> None:
        job = self._selected_job()
        if not job:
            return
        if is_running(job):
            self.notify("Cannot delete a running job. Kill it first.", severity="error")
            return

        def on_confirm(confirmed: bool) -> None:
            if not confirmed or not job:
                return
            import shutil
            ws = job.workspace_path()
            if ws.exists():
                shutil.rmtree(ws)
            self.store.remove(job.id)
            self._refresh_jobs()
            self.notify(f"Deleted job {job.id}")

        self.push_screen(ConfirmDialog(f"Delete job {job.id} and its workspace?"), on_confirm)

    def action_resume(self) -> None:
        job = self._selected_job()
        if not job:
            return
        if is_running(job):
            self.notify("Job is still running", severity="warning")
            return
        if job.status not in ("failed", "killed", "unknown", "completed"):
            self.notify(f"Cannot resume job in state: {job.status}", severity="warning")
            return

        from minimize.cli import prepare_resume
        from minimize.process import start_job

        cycled = prepare_resume(job, self.store)
        if cycled:
            self.notify(f"Detected edit → {cycled}")

        # Reload fresh state and start
        job = self.store.get(job.id)
        if job:
            try:
                start_job(job)
                self._refresh_jobs()
                self.notify(f"Resumed job {job.id} (attempt {job.attempt})")
            except Exception as e:
                self.store.update(job.id, status="failed", error_summary=str(e),
                                  finished_at=datetime.now(timezone.utc).isoformat())
                self.notify(f"Failed to resume: {e}", severity="error")
                self._refresh_jobs()

    def action_edit_note(self) -> None:
        job = self._selected_job()
        if not job:
            return

        def on_note(note: str | None) -> None:
            if note is not None and job:
                self.store.update(job.id, note=note if note else None)
                self._refresh_jobs()

        self.push_screen(NoteDialog(job.note or ""), on_note)


def run_tui() -> None:
    app = MinimizeDashboard()
    app.run()
