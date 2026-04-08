"""Lean project discovery: find project root, parse lakefile, derive module name."""

from dataclasses import dataclass, field
from pathlib import Path
import json
import tomllib


class MinimizeError(Exception):
    pass


@dataclass
class Dependency:
    name: str
    scope: str = ""
    git: str = ""
    rev: str = ""
    path: str = ""
    inherited: bool = False


@dataclass
class ProjectInfo:
    root: Path
    name: str
    lean_version: str
    dependencies: list[Dependency] = field(default_factory=list)
    lean_options: dict[str, object] = field(default_factory=dict)


def find_project_root(lean_file: Path) -> Path:
    """Walk up from lean_file to find lakefile.toml or lakefile.lean + lean-toolchain."""
    current = lean_file.resolve().parent
    while True:
        has_lakefile = (current / "lakefile.toml").exists() or (current / "lakefile.lean").exists()
        has_toolchain = (current / "lean-toolchain").exists()
        if has_lakefile and has_toolchain:
            return current
        parent = current.parent
        if parent == current:
            raise MinimizeError(
                f"No Lean project found above {lean_file}.\n"
                "Expected to find lakefile.toml (or lakefile.lean) and lean-toolchain."
            )
        current = parent


def _parse_toml_project(root: Path) -> ProjectInfo:
    """Parse a lakefile.toml project."""
    with open(root / "lakefile.toml", "rb") as f:
        data = tomllib.load(f)

    name = data.get("name", root.name)
    lean_version = (root / "lean-toolchain").read_text().strip()

    deps = []
    for req in data.get("require", []):
        deps.append(Dependency(
            name=req.get("name", ""),
            scope=req.get("scope", ""),
            git=req.get("git", ""),
            rev=req.get("rev", ""),
            path=req.get("path", ""),
        ))

    lean_options = {}
    for lib in data.get("lean_lib", []):
        if lib.get("leanOptions"):
            lean_options.update(lib["leanOptions"])

    return ProjectInfo(
        root=root, name=name, lean_version=lean_version,
        dependencies=deps, lean_options=lean_options,
    )


def _parse_manifest_project(root: Path) -> ProjectInfo:
    """Parse project from lake-manifest.json + lean-toolchain, with name from lakefile."""
    lean_version = (root / "lean-toolchain").read_text().strip()

    # Try to extract project name
    name = root.name
    lakefile_lean = root / "lakefile.lean"
    if lakefile_lean.exists():
        import re
        content = lakefile_lean.read_text()
        # Try: package mathlib  or  package "mathlib"
        m = re.search(r'package\s+(?:"([^"]+)"|(\w+))', content)
        if m:
            name = m.group(1) or m.group(2)

    manifest = root / "lake-manifest.json"
    if not manifest.exists():
        raise MinimizeError(
            f"No lake-manifest.json found in {root}.\n"
            "Run `lake build` in the project first to generate the manifest."
        )

    with open(manifest) as f:
        data = json.load(f)

    deps = []
    for pkg in data.get("packages", []):
        dep = Dependency(
            name=pkg.get("name", ""),
            scope=pkg.get("scope", ""),
            inherited=pkg.get("inherited", False),
        )
        pkg_type = pkg.get("type", "")
        if pkg_type == "git":
            dep.git = pkg.get("url", "")
            dep.rev = pkg.get("inputRev", pkg.get("rev", ""))
        elif pkg_type == "path":
            dep.path = pkg.get("dir", "")
        deps.append(dep)

    return ProjectInfo(
        root=root, name=name, lean_version=lean_version,
        dependencies=deps,
    )


def parse_project(root: Path) -> ProjectInfo:
    """Parse project metadata from the project root."""
    if (root / "lakefile.toml").exists():
        return _parse_toml_project(root)
    return _parse_manifest_project(root)


def has_mathlib_dependency(project: ProjectInfo) -> bool:
    """Check if the project depends on Mathlib (directly or transitively)."""
    return any(
        d.name.lower() == "mathlib" or "mathlib" in d.git.lower()
        for d in project.dependencies
    )
