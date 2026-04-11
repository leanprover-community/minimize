"""Click CLI for the minimize tool."""

import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape as markup_escape
from rich.table import Table

from minimize.config import MINIMIZE_DIR
from minimize.job import Job, JobStore
from minimize.monitor import count_imports, detect_phase, get_error_summary, get_output_loc
from minimize.process import attach_job, is_running, kill_job, start_job
from minimize.project import MinimizeError, find_project_root, has_mathlib_dependency, parse_project
from minimize.workspace import create_workspace

console = Console()

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "minimize", "GIT_AUTHOR_EMAIL": "minimize@localhost",
    "GIT_COMMITTER_NAME": "minimize", "GIT_COMMITTER_EMAIL": "minimize@localhost",
}


def _next_cycle_number(input_file: str) -> int:
    """Minimize/Target.lean → 2, Minimize/Target.2.lean → 3"""
    stem = Path(input_file).stem
    parts = stem.rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1]) + 1
    return 2


def prepare_resume(job: Job, store: JobStore) -> str | None:
    """Prepare a job for resume: rotate log, detect human edits, update job state.

    Returns the new input_file if a human edit was detected, or None.
    """
    ws = job.workspace_path()

    # Rotate log
    log_file = ws / "minimize.log"
    if log_file.exists():
        n = 1
        while (ws / f"minimize.log.{n}").exists():
            n += 1
        log_file.rename(ws / f"minimize.log.{n}")

    # Detect human edits to the output file
    input_file = getattr(job, "input_file", "Minimize/Target.lean")
    out_file = Path(input_file).with_suffix(".out.lean")
    out_path = ws / out_file
    cycled = None

    if out_path.exists():
        # Check for uncommitted changes (staged or unstaged) vs HEAD.
        # Only detect edits if: git is available, workspace is a git repo, and the
        # output file is tracked (otherwise it's a pre-upgrade workspace where the
        # minimizer never committed on exit).
        has_edit = False
        try:
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(out_file)],
                cwd=ws, capture_output=True,
            )
            if tracked.returncode == 0:
                r = subprocess.run(
                    ["git", "status", "--porcelain", "--", str(out_file)],
                    cwd=ws, capture_output=True, text=True,
                )
                has_edit = r.returncode == 0 and bool(r.stdout.strip())
        except FileNotFoundError:
            pass  # git not available — skip edit detection

        if has_edit:
            # Human edited the output — cycle to next numbered file
            next_num = _next_cycle_number(input_file)
            new_input = f"Minimize/Target.{next_num}.lean"
            while (ws / new_input).exists():
                next_num += 1
                new_input = f"Minimize/Target.{next_num}.lean"
            shutil.copy2(out_path, ws / new_input)
            env = {**os.environ, **_GIT_ENV}
            subprocess.run(
                ["git", "add", str(out_file), new_input], cwd=ws, capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Human edit → {new_input}"],
                cwd=ws, capture_output=True, env=env,
            )
            input_file = new_input
            cycled = new_input

    new_args = list(job.extra_args)
    if input_file != getattr(job, "input_file", "Minimize/Target.lean"):
        # New input file — start fresh (no --resume)
        new_args = [a for a in new_args if a != "--resume"]
    else:
        if "--resume" not in new_args:
            new_args.append("--resume")

    store.update(
        job.id,
        status="created",
        finished_at=None,
        error_summary=None,
        attempt=job.attempt + 1,
        extra_args=new_args,
        input_file=input_file,
    )
    return cycled


def _short_toolchain(tc: str) -> str:
    """Shorten a toolchain name for display (e.g. 'leanprover/lean4:v4.27.0' -> 'v4.27.0')."""
    if ":" in tc:
        return tc.split(":")[-1]
    return tc


def _get_job(store: JobStore, job_id: str) -> Job:
    """Get a job by ID, handling ambiguous prefixes and missing jobs."""
    try:
        job = store.get(job_id)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    if not job:
        console.print(f"[red]Job not found: {job_id}[/red]")
        sys.exit(1)
    return job


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """minimize - Lean 4 code minimization job manager."""
    if ctx.invoked_subcommand is None:
        from minimize.tui import run_tui
        run_tui()


@main.command()
@click.argument("lean_file", type=click.Path(exists=True))
@click.option("--marker", default="#guard_msgs", help="Marker pattern for the invariant")
@click.option("--force", is_flag=True, help="Skip validation checks")
@click.option("--note", default=None, help="Note to attach to the job")
@click.option("--cross-toolchain", default=None, help="Second toolchain for cross-version minimization (e.g. leanprover/lean4:v4.27.0)")
@click.option("--mirror-deps", is_flag=True, help="Mirror dependencies instead of using path dependency")
@click.option("--extra-args", default="", help="Extra args for lake exe minimize (quote the whole string)")
def run(lean_file: str, marker: str, force: bool, note: str | None, cross_toolchain: str | None, mirror_deps: bool, extra_args: str) -> None:
    """Start a minimization job for LEAN_FILE."""
    lean_path = Path(lean_file).resolve()

    if lean_path.suffix != ".lean":
        console.print("[red]Error: File must be a .lean file[/red]")
        sys.exit(1)

    # Parse extra args early, before creating workspace
    parsed_extra = shlex.split(extra_args) if extra_args else []

    try:
        root = find_project_root(lean_path)
        project = parse_project(root)
    except MinimizeError as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    console.print(f"Project: [bold]{project.name}[/bold] ({root})")
    console.print(f"Lean: {project.lean_version}")
    if cross_toolchain:
        console.print(f"Cross toolchain: {cross_toolchain}")
    if has_mathlib_dependency(project):
        console.print("Mathlib dependency detected (will run cache get)")

    try:
        ws = create_workspace(
            project, lean_path, marker=marker, force=force,
            use_path_dep=not mirror_deps,
        )
    except MinimizeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    console.print(f"Workspace: {ws}")

    store = JobStore()
    job = Job.create(
        source_file=lean_path,
        project_name=project.name,
        workspace=ws,
        lean_version=project.lean_version,
        marker=marker,
        extra_args=parsed_extra,
        note=note,
        cross_toolchain=cross_toolchain,
    )

    try:
        start_job(job)
    except Exception as e:
        # Clean up workspace on tmux failure
        console.print(f"[red]Failed to start job: {e}[/red]")
        shutil.rmtree(ws, ignore_errors=True)
        sys.exit(1)

    store.add(job)

    console.print(f"[green]Job started:[/green] {job.id}")
    console.print(f"  Attach: minimize attach {job.id}")
    console.print(f"  Log:    minimize log {job.id}")
    console.print(f"  TUI:    minimize")


@main.command(name="list")
def list_jobs() -> None:
    """Show all jobs in a table."""
    store = JobStore()
    jobs = store.load()

    if not jobs:
        console.print("[dim]No jobs. Start one with: minimize run <file.lean>[/dim]")
        return

    any_cross = any(j.cross_toolchain for j in jobs)

    table = Table(show_header=True)
    table.add_column("ID", style="bold")
    table.add_column("Status")
    table.add_column("File")
    table.add_column("Project")
    if any_cross:
        table.add_column("Cross TC")
    table.add_column("Duration")
    table.add_column("LOC")
    table.add_column("Imports")
    table.add_column("Note")

    for job in jobs:
        _reconcile_job(store, job)

        style = {
            "created": "dim", "cache_get": "blue", "building": "blue",
            "running": "blue", "completed": "green", "failed": "red",
            "killed": "dim", "unknown": "yellow",
        }.get(job.status, "white")

        ws = job.workspace_path()
        inf = getattr(job, "input_file", "Minimize/Target.lean")
        loc = get_output_loc(ws, inf)
        imports = count_imports(ws, inf)

        row: list[str] = [
            job.id,
            f"[{style}]{markup_escape(job.status.upper())}[/{style}]",
            markup_escape(Path(job.source_file).stem),
            markup_escape(job.project_name),
        ]
        if any_cross:
            row.append(markup_escape(_short_toolchain(job.cross_toolchain)) if job.cross_toolchain else "")
        row.extend([
            job.duration_str(),
            str(loc) if loc is not None else "-",
            str(imports),
            markup_escape((job.note or "")[:40]),
        ])
        table.add_row(*row)

    console.print(table)


@main.command()
@click.argument("job_id")
def kill_cmd(job_id: str) -> None:
    """Kill a running job."""
    store = JobStore()
    job = _get_job(store, job_id)
    if not is_running(job):
        console.print(f"[yellow]Job {job.id} is not running (status: {job.status})[/yellow]")
        return
    killed = kill_job(job)
    if killed:
        store.update(job.id, status="killed", finished_at=datetime.now(timezone.utc).isoformat())
        console.print(f"[green]Killed job {job.id}[/green]")
    else:
        console.print(f"[yellow]Session already ended for job {job.id}[/yellow]")


kill_cmd.name = "kill"


@main.command()
@click.argument("job_id")
def attach(job_id: str) -> None:
    """Attach to a job's tmux session to see live output."""
    store = JobStore()
    job = _get_job(store, job_id)
    if not is_running(job):
        console.print(f"[yellow]Job {job.id} is not running. Use 'minimize log {job.id}' to see the log.[/yellow]")
        sys.exit(1)
    attach_job(job)


@main.command()
@click.argument("job_id")
def log(job_id: str) -> None:
    """Tail the log of a job."""
    store = JobStore()
    job = _get_job(store, job_id)
    log_file = job.workspace_path() / "minimize.log"
    if not log_file.exists():
        console.print("[dim]No log file yet[/dim]")
        return
    os.execvp("tail", ["tail", "-f", str(log_file)])


@main.command(name="open")
@click.argument("job_id")
def open_vscode(job_id: str) -> None:
    """Open a job's workspace in VS Code."""
    store = JobStore()
    job = _get_job(store, job_id)
    ws = job.workspace_path()
    inf = getattr(job, "input_file", "Minimize/Target.lean")
    out_file = ws / Path(inf).with_suffix(".out.lean")
    target = out_file if out_file.exists() else ws / inf
    try:
        subprocess.Popen(
            ["code", str(ws), str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        console.print(f"Opened VS Code at {ws}")
    except FileNotFoundError:
        console.print("[red]Error: 'code' command not found[/red]")
        sys.exit(1)


@main.command()
@click.argument("job_id")
def resume(job_id: str) -> None:
    """Resume a stopped job with --resume flag."""
    store = JobStore()
    job = _get_job(store, job_id)
    if is_running(job):
        console.print(f"[yellow]Job {job.id} is still running[/yellow]")
        sys.exit(1)
    if job.status not in ("failed", "killed", "unknown", "completed"):
        console.print(f"[yellow]Cannot resume job in state: {job.status}[/yellow]")
        sys.exit(1)

    cycled = prepare_resume(job, store)
    if cycled:
        console.print(f"[green]Detected edit → {cycled}[/green]")

    job = store.get(job.id)
    try:
        start_job(job)
    except Exception as e:
        store.update(job.id, status="failed", error_summary=str(e))
        console.print(f"[red]Failed to resume: {e}[/red]")
        sys.exit(1)
    console.print(f"[green]Resumed job {job.id} (attempt {job.attempt})[/green]")


@main.command()
@click.argument("job_id")
@click.argument("text", required=False)
def note(job_id: str, text: str | None) -> None:
    """View or set a note on a job."""
    store = JobStore()
    job = _get_job(store, job_id)
    if text is None:
        if job.note:
            console.print(job.note)
        else:
            console.print("[dim]No note set[/dim]")
    else:
        store.update(job.id, note=text if text else None)
        console.print(f"Note updated for job {job.id}")


@main.command()
@click.argument("job_id", required=False)
def result(job_id: str | None) -> None:
    """Print the minimized output of a job."""
    store = JobStore()
    if job_id:
        job = _get_job(store, job_id)
        jobs = [job]
    else:
        # Reconcile first so completed jobs are found
        all_jobs = store.load()
        for j in all_jobs:
            _reconcile_job(store, j)
        jobs = [j for j in store.load() if j.status == "completed"]
        if not jobs:
            console.print("[dim]No completed jobs[/dim]")
            return

    for job in jobs:
        inf = getattr(job, "input_file", "Minimize/Target.lean")
        out = job.workspace_path() / Path(inf).with_suffix(".out.lean")
        if out.exists():
            if len(jobs) > 1:
                console.print(f"\n[bold]--- {Path(job.source_file).stem} ({job.id}) ---[/bold]")
            console.print(out.read_text())
        else:
            console.print(f"[dim]No output yet for job {job.id}[/dim]")


@main.command()
def doctor() -> None:
    """Validate environment: check for required tools."""
    all_ok = True

    for tool, check_cmd in [
        ("tmux", ["tmux", "-V"]),
        ("lake", ["lake", "--version"]),
        ("elan", ["elan", "--version"]),
        ("code", ["code", "--version"]),
    ]:
        try:
            result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=5)
            version = result.stdout.strip().splitlines()[0] if result.stdout else "found"
            console.print(f"  [green]OK[/green]  {tool}: {version}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            console.print(f"  [red]MISSING[/red]  {tool}")
            all_ok = False

    if MINIMIZE_DIR.exists():
        console.print(f"  [green]OK[/green]  data dir: {MINIMIZE_DIR}")
    else:
        console.print(f"  [dim]INFO[/dim]  data dir will be created at {MINIMIZE_DIR}")

    if all_ok:
        console.print("\n[green]Environment looks good![/green]")
    else:
        console.print("\n[yellow]Some tools are missing. Install them for full functionality.[/yellow]")


@main.command()
@click.option("--older-than", default=None, help="Remove jobs older than N days (e.g., 7d)")
@click.option("--force", is_flag=True, help="Skip confirmation")
def clean(older_than: str | None, force: bool) -> None:
    """Remove completed/failed job workspaces."""
    store = JobStore()
    jobs = store.load()

    if older_than:
        try:
            days = int(older_than.rstrip("d"))
        except ValueError:
            console.print(f"[red]Invalid format: '{older_than}'. Use e.g. '7d'[/red]")
            sys.exit(1)
    else:
        days = None

    removable = []
    for job in jobs:
        try:
            if is_running(job):
                continue
        except Exception:
            pass
        if job.status not in ("completed", "failed", "killed", "unknown"):
            continue
        if days is not None:
            created = datetime.fromisoformat(job.created_at)
            age = (datetime.now(timezone.utc) - created).days
            if age < days:
                continue
        removable.append(job)

    if not removable:
        console.print("[dim]Nothing to clean[/dim]")
        return

    console.print(f"Will remove {len(removable)} job(s):")
    for job in removable:
        console.print(f"  {job.id}  {Path(job.source_file).stem}  ({job.status})")

    if not force:
        if not click.confirm("Proceed?"):
            return

    for job in removable:
        ws = job.workspace_path()
        if ws.exists():
            shutil.rmtree(ws)
        store.remove(job.id)
        console.print(f"  Removed {job.id}")


def _reconcile_job(store: JobStore, job: Job) -> None:
    """Reconcile job status with tmux and log state."""
    if job.status not in ("created", "cache_get", "building", "running"):
        return
    try:
        running = is_running(job)
    except Exception:
        return
    if running:
        phase = detect_phase(job.workspace_path())
        if phase in ("cache_get", "building", "running") and phase != job.status:
            store.update(job.id, status=phase)
            job.status = phase
    else:
        phase = detect_phase(job.workspace_path())
        if phase in ("completed", "failed"):
            kwargs: dict[str, object] = {
                "status": phase,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            if phase == "failed":
                kwargs["error_summary"] = get_error_summary(job.workspace_path())
            store.update(job.id, **kwargs)
            job.status = phase
        else:
            store.update(
                job.id, status="unknown",
                finished_at=datetime.now(timezone.utc).isoformat(),
                error_summary="Session disappeared before exit marker",
            )
            job.status = "unknown"
