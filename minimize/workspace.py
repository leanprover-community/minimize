"""Workspace creation: generate lakefile.toml, copy target file, validate."""

import hashlib
import os
import subprocess
from pathlib import Path

from minimize.config import MINIMIZE_DIR, LEAN_MINIMIZER_REPO, LEAN_MINIMIZER_REV
from minimize.project import ProjectInfo, MinimizeError


def _resolve_git_info(project_root: Path) -> tuple[str, str] | None:
    """Resolve git remote URL and HEAD commit for a project root.

    Uses the current branch's upstream remote, falling back to origin.
    Returns (remote_url, commit_hash) or None if not a git repo or no remote.
    """
    def _git(*args: str) -> str | None:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=project_root, capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip() if r.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    commit = _git("rev-parse", "HEAD")
    if not commit:
        return None

    # Ensure the Lake package root is the git repo root (not a monorepo subdir)
    git_toplevel = _git("rev-parse", "--show-toplevel")
    if git_toplevel and Path(git_toplevel).resolve() != project_root.resolve():
        return None  # subdir package; git dep without subDir would be wrong

    # Strategy 1: upstream remote of the current branch
    remote_name = None
    branch = _git("branch", "--show-current")
    if branch:
        remote_name = _git("config", "--get", f"branch.{branch}.remote")

    # Strategy 2: fall back to 'origin'
    if not remote_name:
        remote_name = "origin"

    url = _git("remote", "get-url", remote_name)
    if url:
        return (url, commit)

    return None


def _git_init(ws: Path) -> None:
    """Initialize the workspace as a git repo with an initial commit."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "minimize",
        "GIT_AUTHOR_EMAIL": "minimize@localhost",
        "GIT_COMMITTER_NAME": "minimize",
        "GIT_COMMITTER_EMAIL": "minimize@localhost",
    }
    try:
        subprocess.run(["git", "init"], cwd=ws, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=ws, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial workspace"],
            cwd=ws, capture_output=True, check=True, env=env,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass  # git not available or failed; not critical


def workspace_dir(project_name: str, filename: str, source_file: Path) -> Path:
    """Compute workspace directory path, using a short hash for uniqueness."""
    h = hashlib.sha256(str(source_file.resolve()).encode()).hexdigest()[:8]
    return MINIMIZE_DIR / project_name / f"{filename}-{h}"


def generate_lakefile_toml(
    project: ProjectInfo,
    use_path_dep: bool = True,
    *,
    include_lean_minimizer: bool = True,
) -> str:
    """Generate lakefile.toml content for a minimization workspace.

    When `include_lean_minimizer` is False, the `LeanMinimizer` require is
    omitted. Cross workspaces only need to build the project's dependency
    closure under an alternate toolchain — they never run `lake exe minimize`
    themselves — so pulling in `LeanMinimizer` there is unnecessary (and may
    even fail if it doesn't compile under the older toolchain).
    """
    lines = [
        'name = "minimize-workspace"',
        'version = "0.1.0"',
        'defaultTargets = ["Minimize"]',
        "",
    ]
    if include_lean_minimizer:
        lines.extend([
            "[[require]]",
            'name = "LeanMinimizer"',
            f"git = {_toml_str(LEAN_MINIMIZER_REPO)}",
            f"rev = {_toml_str(LEAN_MINIMIZER_REV)}",
            "",
        ])

    if use_path_dep:
        # Try to resolve the source project to a git dependency for reproducibility
        # and compatibility with import inlining (which searches .lake/packages/).
        git_info = _resolve_git_info(project.root)
        if git_info is not None:
            git_url, git_rev = git_info
            lines.extend([
                "[[require]]",
                f"name = {_toml_str(project.name)}",
                f"git = {_toml_str(git_url)}",
                f"rev = {_toml_str(git_rev)}",
                "",
            ])
        else:
            # Fallback: not a git repo, no remote, or monorepo subdir
            lines.extend([
                "[[require]]",
                f"name = {_toml_str(project.name)}",
                f"path = {_toml_str(str(project.root))}",
                "",
            ])
    else:
        # Strategy B: mirror direct dependencies
        for dep in project.dependencies:
            if dep.inherited:
                continue
            lines.append("[[require]]")
            lines.append(f"name = {_toml_str(dep.name)}")
            if dep.scope:
                lines.append(f"scope = {_toml_str(dep.scope)}")
            if dep.git:
                lines.append(f"git = {_toml_str(dep.git)}")
            if dep.rev:
                lines.append(f"rev = {_toml_str(dep.rev)}")
            if dep.path:
                resolved = (project.root / dep.path).resolve()
                lines.append(f"path = {_toml_str(str(resolved))}")
            lines.append("")

    # lean_lib for the target
    lines.append("[[lean_lib]]")
    lines.append('name = "Minimize"')
    if project.lean_options:
        opts = ", ".join(f"{k} = {_toml_value(v)}" for k, v in project.lean_options.items())
        lines.append(f"leanOptions = {{{opts}}}")
    lines.append("")

    return "\n".join(lines)


def _toml_escape(s: str) -> str:
    """Escape a string for TOML double-quoted strings."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _toml_str(s: str) -> str:
    """Format a string as a TOML quoted string."""
    return f'"{_toml_escape(s)}"'


def _toml_value(v: object) -> str:
    """Format a value for inline TOML."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    return _toml_str(str(v))


def validate_target(content: str, marker: str = "#guard_msgs") -> str | None:
    """Check if the file contains the marker. Returns error message or None."""
    if marker not in content:
        return (
            f"File does not contain '{marker}'.\n"
            "\n"
            "The minimizer needs a marker to identify which commands to preserve.\n"
            "Add a #guard_msgs block around the command you want to minimize:\n"
            "\n"
            "  /-- error: your expected error message --/\n"
            "  #guard_msgs in\n"
            "  #check yourThing\n"
            "\n"
            "Or use --force to skip this check, or --marker to use a different marker."
        )
    return None


def create_workspace(
    project: ProjectInfo,
    lean_file: Path,
    marker: str = "#guard_msgs",
    force: bool = False,
    use_path_dep: bool = True,
) -> Path:
    """Create a minimization workspace. Returns the workspace path."""
    filename = lean_file.stem
    ws = workspace_dir(project.name, filename, lean_file)

    if ws.exists():
        raise MinimizeError(
            f"Workspace already exists: {ws}\n"
            "Use 'minimize list' to see existing jobs, or delete the workspace first."
        )

    # Read and validate source file
    content = lean_file.read_text()
    error = validate_target(content, marker)
    if error and not force:
        raise MinimizeError(error)

    # Create directory structure
    ws.mkdir(parents=True)
    (ws / "Minimize").mkdir()

    # Write lean-toolchain
    (ws / "lean-toolchain").write_text(project.lean_version + "\n")

    # Write lakefile.toml
    lakefile = generate_lakefile_toml(project, use_path_dep=use_path_dep)
    (ws / "lakefile.toml").write_text(lakefile)

    # Write the target file
    (ws / "Minimize" / "Target.lean").write_text(content)

    # Write root module
    (ws / "Minimize.lean").write_text("import Minimize.Target\n")

    # Write .gitignore for Lake build artifacts
    (ws / ".gitignore").write_text(".lake/\nlake-manifest.json\nminimize.log\n")

    # Initialize as a git repo so changes from the minimizer can be tracked
    _git_init(ws)

    return ws


def cross_workspace_path(primary_ws: Path) -> Path:
    """Sibling directory that holds the cross-toolchain twin workspace."""
    return primary_ws.parent / (primary_ws.name + ".cross")


def create_cross_workspace(
    project: ProjectInfo,
    lean_file: Path,
    primary_ws: Path,
    cross_toolchain: str,
    marker: str = "#guard_msgs",
    use_path_dep: bool = True,
) -> Path:
    """Create a sibling Lake workspace pinned to `cross_toolchain`.

    The cross workspace has the same dependency closure as the primary
    workspace so that the target file's imports resolve identically — only
    the `lean-toolchain` differs. The `LeanMinimizer` require is omitted
    since the cross workspace never runs `lake exe minimize` itself; the
    minimizer only uses it to compile the target under the alternate
    toolchain via `lake env lean`.

    Idempotent: if the directory already exists, it is reused as-is. The
    wrapper script `lake build`s both workspaces on every run anyway, so a
    stale directory is self-healing.
    """
    ws = cross_workspace_path(primary_ws)
    content = lean_file.read_text()

    if ws.exists():
        # Trust but refresh: rewrite lean-toolchain and the target file so we
        # don't accidentally reuse stale state from a previous run.
        (ws / "lean-toolchain").write_text(cross_toolchain + "\n")
        (ws / "Minimize" / "Target.lean").write_text(content)
        return ws

    ws.mkdir(parents=True)
    (ws / "Minimize").mkdir()

    (ws / "lean-toolchain").write_text(cross_toolchain + "\n")

    lakefile = generate_lakefile_toml(
        project, use_path_dep=use_path_dep, include_lean_minimizer=False,
    )
    (ws / "lakefile.toml").write_text(lakefile)

    (ws / "Minimize" / "Target.lean").write_text(content)
    (ws / "Minimize.lean").write_text("import Minimize.Target\n")
    (ws / ".gitignore").write_text(".lake/\nlake-manifest.json\n")

    return ws
