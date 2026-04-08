"""Status polling: LOC counting, import counting, phase detection from logs."""

from pathlib import Path


def get_output_loc(workspace: Path) -> int | None:
    """Count lines in the current output file."""
    out_file = workspace / "Minimize" / "Target.out.lean"
    if not out_file.exists():
        return None
    try:
        with open(out_file) as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def count_imports(workspace: Path) -> int:
    """Count import lines in the current output (or source if no output yet)."""
    out_file = workspace / "Minimize" / "Target.out.lean"
    target = out_file if out_file.exists() else workspace / "Minimize" / "Target.lean"
    try:
        with open(target) as f:
            return sum(1 for line in f if line.strip().startswith("import "))
    except OSError:
        return 0


def detect_phase(workspace: Path) -> str:
    """Detect the current phase from log markers. Returns a status string."""
    log = workspace / "minimize.log"
    if not log.exists():
        return "created"
    try:
        with open(log, "rb") as f:
            # Read last 8KB — enough to find the latest markers
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return "created"

    # Check for exit marker (most specific)
    if "---MINIMIZE-EXIT:" in tail:
        # Find the last exit marker
        idx = tail.rfind("---MINIMIZE-EXIT:")
        rest = tail[idx + len("---MINIMIZE-EXIT:"):]
        code_str = rest.split("---")[0].strip()
        try:
            code = int(code_str)
            return "completed" if code == 0 else "failed"
        except ValueError:
            return "failed"

    # Check phase markers (find the last one)
    last_phase = None
    for phase in ("cache_get", "building", "building_cross", "running"):
        marker = f"---MINIMIZE-PHASE:{phase}---"
        if marker in tail:
            idx = tail.rfind(marker)
            if last_phase is None or idx > last_phase[1]:
                last_phase = (phase, idx)

    if last_phase:
        return last_phase[0]

    return "created"


def get_error_summary(workspace: Path) -> str | None:
    """Extract a brief error summary from the log tail."""
    log = workspace / "minimize.log"
    if not log.exists():
        return None
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    lines = tail.strip().splitlines()
    # Look for common error patterns
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("---MINIMIZE-"):
            continue
        if not line:
            continue
        if "error" in line.lower() or "Error" in line:
            return line[:200]
    # Return last non-marker line
    for line in reversed(lines):
        line = line.strip()
        if line and not line.startswith("---MINIMIZE-"):
            return line[:200]
    return None


