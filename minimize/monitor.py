"""Status polling: LOC counting, import counting, phase detection from logs."""

import subprocess
import threading
from pathlib import Path


def get_output_loc(workspace: Path, input_file: str = "Minimize/Target.lean") -> int | None:
    """Count lines in the current output file."""
    out_file = workspace / Path(input_file).with_suffix(".out.lean")
    if not out_file.exists():
        return None
    try:
        with open(out_file) as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def _direct_imports(target: Path) -> frozenset[str]:
    """Extract the set of direct import module names from a Lean file."""
    imports: set[str] = set()
    try:
        with open(target) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("--") or stripped.startswith("/-"):
                    continue
                if "import " in stripped:
                    mod = stripped.split()[-1]
                    imports.add(mod)
                elif not stripped.startswith("public") and not stripped.startswith("meta"):
                    break
    except OSError:
        pass
    return frozenset(imports)


_LEAN_SCRIPT = '''\
import Lean
open Lean in
unsafe def main (args : List String) : IO Unit := do
  let file := args.head!
  initSearchPath (← findSysroot)
  let input ← IO.FS.readFile file
  let (imports, _, _) ← Elab.parseImports input file
  let env ← importModules imports {} 0
  let mut count := 0
  for name in env.header.moduleNames do
    let root := name.getRoot
    if root != `Init && root != `Lean && root != `Std then
      count := count + 1
  IO.println s!"{count}"
'''


def _count_transitive_imports(workspace: Path, target: Path) -> int | None:
    """Run a Lean subprocess to count transitive imports excluding Init/Lean/Std."""
    script = workspace / ".lake" / "_count_imports.lean"
    try:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(_LEAN_SCRIPT)
        r = subprocess.run(
            ["lake", "env", "lean", "--run", str(script), str(target)],
            cwd=workspace, capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return int(r.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, OSError):
        pass
    return None


# Cache: frozenset of direct imports → transitive count
_import_cache: dict[frozenset[str], int] = {}
# Track in-flight background computations
_import_pending: set[frozenset[str]] = set()
_import_lock = threading.Lock()


def count_imports(workspace: Path, input_file: str = "Minimize/Target.lean") -> int:
    """Count transitive imports (excluding Init/Lean/Std) with caching.

    Returns cached transitive count if available, otherwise returns direct count
    immediately and kicks off a background computation for the transitive count.
    """
    out_file = workspace / Path(input_file).with_suffix(".out.lean")
    target = out_file if out_file.exists() else workspace / input_file

    imports = _direct_imports(target)
    if not imports:
        return 0

    with _import_lock:
        if imports in _import_cache:
            return _import_cache[imports]
        if imports in _import_pending:
            return len(imports)  # background computation in flight

    # Return direct count now, compute transitive in background
    def _compute() -> None:
        with _import_lock:
            _import_pending.add(imports)
        try:
            count = _count_transitive_imports(workspace, target)
            with _import_lock:
                if count is not None:
                    _import_cache[imports] = count
                else:
                    _import_cache[imports] = len(imports)
        finally:
            with _import_lock:
                _import_pending.discard(imports)

    threading.Thread(target=_compute, daemon=True).start()
    return len(imports)


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
    for phase in ("cache_get", "building", "running"):
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


