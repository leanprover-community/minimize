"""tmux session management for persistent background minimization jobs."""

import os
import shlex
import subprocess
from pathlib import Path

from minimize.job import Job
from minimize.project import find_project_root, has_mathlib_dependency, parse_project


def _session_name(job: Job) -> str:
    return f"minimize-{job.id}"


def _generate_wrapper(job: Job) -> Path:
    """Generate the wrapper shell script that runs inside tmux."""
    ws = job.workspace_path()
    script = ws / "minimize-wrapper.sh"

    # Detect if mathlib dependency by checking the source project
    do_cache_get = False
    try:
        source = Path(job.source_file)
        root = find_project_root(source)
        project = parse_project(root)
        do_cache_get = has_mathlib_dependency(project)
    except Exception:
        pass

    extra = shlex.join(job.extra_args) if job.extra_args else ""
    marker_arg = f"--marker {shlex.quote(job.marker)}" if job.marker != "#guard_msgs" else ""
    cross_arg = f"--cross-toolchain {shlex.quote(job.cross_toolchain)}" if job.cross_toolchain else ""
    args = f"Minimize/Target.lean {marker_arg} {cross_arg} {extra}".strip()

    lines = [
        "#!/usr/bin/env bash",
        "set -o pipefail",
        "",
        f"cd {shlex.quote(str(ws))}",
        "",
    ]

    if do_cache_get:
        lines.extend([
            'echo "---MINIMIZE-PHASE:cache_get---" >> minimize.log',
            "lake exe cache get 2>&1 | tee -a minimize.log",
            "",
        ])

    lines.extend([
        'echo "---MINIMIZE-PHASE:building---" >> minimize.log',
        "lake build 2>&1 | tee -a minimize.log",
        "build_exit=$?",
        'if [ "$build_exit" -ne 0 ]; then',
        '    echo "---MINIMIZE-EXIT:$build_exit---" >> minimize.log',
        "    exit $build_exit",
        "fi",
        "",
        'echo "---MINIMIZE-PHASE:running---" >> minimize.log',
        f"lake exe minimize {args} 2>&1 | tee -a minimize.log",
        "minimize_exit=$?",
        'echo "---MINIMIZE-EXIT:$minimize_exit---" >> minimize.log',
        "exit $minimize_exit",
    ])

    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


def start_job(job: Job) -> None:
    """Start a minimization job in a new tmux session."""
    script = _generate_wrapper(job)
    session = _session_name(job)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "bash", str(script)],
        check=True,
    )


def is_running(job: Job) -> bool:
    """Check if the job's tmux session is still alive."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", _session_name(job)],
        capture_output=True,
    )
    return result.returncode == 0


def kill_job(job: Job) -> bool:
    """Kill the job's tmux session. Returns True if killed."""
    result = subprocess.run(
        ["tmux", "kill-session", "-t", _session_name(job)],
        capture_output=True,
    )
    return result.returncode == 0


def attach_job(job: Job) -> None:
    """Attach to the job's tmux session (replaces current process)."""
    session = _session_name(job)
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])
