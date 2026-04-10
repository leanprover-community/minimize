"""Version checking: detect uv tool install, compare with remote."""

import subprocess
from pathlib import Path

from minimize.config import LEAN_MINIMIZER_REPO

REPO_URL = "https://github.com/leanprover-community/minimize"


def get_installed_commit() -> str | None:
    """Get the git commit hash of the installed package, if installed from git.

    Returns None if not installed from git (e.g. editable install).
    """
    try:
        import importlib.metadata
        dist = importlib.metadata.distribution("minimize")
        for f in dist.files or []:
            if f.name == "direct_url.json":
                import json
                content = (Path(str(dist._path)).parent / f).read_text()
                data = json.loads(content)
                vcs = data.get("vcs_info", {})
                if vcs.get("vcs") == "git":
                    return vcs.get("commit_id")
    except Exception:
        pass
    return None


def get_remote_commit() -> str | None:
    """Get the latest commit on main from GitHub. May be slow (~0.5s)."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", REPO_URL, "refs/heads/main"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
    except Exception:
        pass
    return None


def is_outdated() -> tuple[bool, str | None, str | None]:
    """Check if the installed version is outdated.

    Returns (is_outdated, installed_commit, remote_commit).
    Returns (False, None, None) if not a git install or check fails.
    """
    installed = get_installed_commit()
    if not installed:
        return False, None, None
    remote = get_remote_commit()
    if not remote:
        return False, installed, None
    return installed != remote, installed, remote
