"""In-memory job registry for tracking background tasks (collection / analysis).

Single-user app: a process-local dict is sufficient. If the process restarts,
in-flight jobs are lost — which is acceptable since jobs are user-triggered.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Literal, Optional

JobKind = Literal["collection", "analysis"]
JobStatus = Literal["pending", "running", "succeeded", "failed"]


@dataclass
class Job:
    id: str
    kind: JobKind
    status: JobStatus = "pending"
    message: str = ""
    progress: int = 0  # 0-100
    total: int = 0
    processed: int = 0
    new_count: int = 0
    failed_count: int = 0
    error: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    db_id: Optional[int] = None  # related CollectionJob.id or AnalysisJob.id

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "progress": self.progress,
            "total": self.total,
            "processed": self.processed,
            "new_count": self.new_count,
            "failed_count": self.failed_count,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "db_id": self.db_id,
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()

    def create(self, kind: JobKind) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def all(self) -> Dict[str, Job]:
        return dict(self._jobs)

    def prune(self, keep: int = 50) -> None:
        if len(self._jobs) <= keep:
            return
        items = sorted(self._jobs.values(), key=lambda j: j.started_at)
        for j in items[: len(items) - keep]:
            self._jobs.pop(j.id, None)


registry = JobRegistry()
