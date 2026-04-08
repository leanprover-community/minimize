"""Job state management with JSON persistence and file locking."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from minimize.config import MINIMIZE_DIR, JOBS_FILE, JOBS_LOCK


@dataclass
class Job:
    id: str
    source_file: str
    project_name: str
    workspace: str
    lean_version: str
    marker: str
    extra_args: list[str]
    status: str  # created | cache_get | building | running | completed | failed | killed | unknown
    created_at: str
    finished_at: str | None = None
    error_summary: str | None = None
    note: str | None = None
    attempt: int = 1
    cross_toolchain: str | None = None

    @staticmethod
    def create(
        source_file: Path,
        project_name: str,
        workspace: Path,
        lean_version: str,
        marker: str,
        extra_args: list[str] | None = None,
        note: str | None = None,
        cross_toolchain: str | None = None,
    ) -> "Job":
        return Job(
            id=uuid4().hex[:8],
            source_file=str(source_file.resolve()),
            project_name=project_name,
            workspace=str(workspace),
            lean_version=lean_version,
            marker=marker,
            extra_args=extra_args or [],
            status="created",
            created_at=datetime.now(timezone.utc).isoformat(),
            note=note,
            cross_toolchain=cross_toolchain,
        )

    def duration_str(self) -> str:
        start = datetime.fromisoformat(self.created_at)
        if self.finished_at:
            end = datetime.fromisoformat(self.finished_at)
        else:
            end = datetime.now(timezone.utc)
        delta = end - start
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours}h {minutes:02d}m"

    def workspace_path(self) -> Path:
        return Path(self.workspace)


class JobStore:
    """JSON job store with atomic read-modify-write under a single lock."""

    def __init__(self) -> None:
        MINIMIZE_DIR.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive lock for the duration of the context."""
        fd = os.open(str(JOBS_LOCK), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load_unlocked(self) -> list[Job]:
        if not JOBS_FILE.exists():
            return []
        with open(JOBS_FILE) as f:
            data = json.load(f)
        return [Job(**d) for d in data]

    def _save_unlocked(self, jobs: list[Job]) -> None:
        fd, tmp = tempfile.mkstemp(dir=MINIMIZE_DIR, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump([asdict(j) for j in jobs], f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(JOBS_FILE))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self) -> list[Job]:
        with self._locked():
            return self._load_unlocked()

    def save(self, jobs: list[Job]) -> None:
        with self._locked():
            self._save_unlocked(jobs)

    def add(self, job: Job) -> None:
        with self._locked():
            jobs = self._load_unlocked()
            jobs.append(job)
            self._save_unlocked(jobs)

    def update(self, job_id: str, **kwargs: object) -> Job | None:
        with self._locked():
            jobs = self._load_unlocked()
            for j in jobs:
                if j.id == job_id:
                    for k, v in kwargs.items():
                        setattr(j, k, v)
                    self._save_unlocked(jobs)
                    return j
        return None

    def get(self, job_id: str) -> Job | None:
        """Get a job by exact ID or unambiguous prefix."""
        jobs = self.load()
        # Exact match first
        for j in jobs:
            if j.id == job_id:
                return j
        # Prefix match — must be unambiguous
        matches = [j for j in jobs if j.id.startswith(job_id)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(m.id for m in matches)
            raise ValueError(f"Ambiguous job prefix '{job_id}' matches: {ids}")
        return None

    def remove(self, job_id: str) -> Job | None:
        with self._locked():
            jobs = self._load_unlocked()
            removed = None
            new_jobs = []
            for j in jobs:
                if j.id == job_id:
                    removed = j
                else:
                    new_jobs.append(j)
            if removed:
                self._save_unlocked(new_jobs)
            return removed
