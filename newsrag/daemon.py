from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from newsrag.jobs import Job, claim_next_job, mark_job_done, mark_job_failed
from newsrag.storage import initialize_storage

JobHandler = Callable[[Job], Awaitable[None]]


class UnknownJobKindError(Exception):
    """Raised when no handler exists for a durable job kind."""


@dataclass(frozen=True)
class DaemonConfig:
    """Runtime settings for the NewsRAG daemon loop."""

    data_dir: Path
    poll_interval: float = 0.5
    max_loops: int | None = None


class DaemonRunner:
    """Async worker loop for durable NewsRAG jobs."""

    def __init__(
        self,
        *,
        database_path: Path,
        handlers: Mapping[str, JobHandler] | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        self.database_path = database_path
        self.handlers = dict(handlers or {})
        self.poll_interval = poll_interval

    async def run(self, *, max_loops: int | None = None) -> None:
        loops = 0
        while True:
            await self.run_cycle()
            loops += 1
            if max_loops is not None and loops >= max_loops:
                return
            await asyncio.sleep(self.poll_interval)

    async def run_cycle(self) -> bool:
        job = await asyncio.to_thread(claim_next_job, self.database_path)
        if job is None:
            return False

        try:
            await self._handle_job(job)
        except Exception as exc:
            await asyncio.to_thread(mark_job_failed, self.database_path, job.id, error=str(exc))
        else:
            await asyncio.to_thread(mark_job_done, self.database_path, job.id)
        return True

    async def _handle_job(self, job: Job) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            raise UnknownJobKindError(f"No handler registered for job kind '{job.kind}'")
        await handler(job)


async def run_daemon(
    config: DaemonConfig,
    *,
    handlers: Mapping[str, JobHandler] | None = None,
) -> None:
    """Start the foreground daemon loop."""

    storage_paths = initialize_storage(config.data_dir)
    runner = DaemonRunner(
        database_path=storage_paths.database,
        handlers=handlers,
        poll_interval=config.poll_interval,
    )
    await runner.run(max_loops=config.max_loops)
