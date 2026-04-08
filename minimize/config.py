from pathlib import Path

MINIMIZE_DIR = Path.home() / ".minimize"
JOBS_FILE = MINIMIZE_DIR / "jobs.json"
JOBS_LOCK = MINIMIZE_DIR / ".jobs.lock"

LEAN_MINIMIZER_REPO = "https://github.com/kim-em/lean-minimizer"
LEAN_MINIMIZER_REV = "master"
