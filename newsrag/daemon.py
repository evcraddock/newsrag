from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from watchfiles import awatch

from newsrag.config import EmbeddingConfig
from newsrag.ingest import INGEST_JOB_KIND, build_ingest_handler
from newsrag.jobs import Job, claim_next_job, mark_job_done, mark_job_failed
from newsrag.storage import initialize_storage
from newsrag.watches import DEFAULT_WATCH_STABILITY_SECONDS, WatchDebouncer, list_watches

JobResult = dict[str, object]
JobHandler = Callable[[Job], Awaitable[JobResult | None]]
LOGGER = logging.getLogger(__name__)


class UnknownJobKindError(Exception):
    """Raised when no handler exists for a durable job kind."""


@dataclass(frozen=True)
class DaemonConfig:
    """Runtime settings for the NewsRAG daemon loop."""

    data_dir: Path
    embedding_config: EmbeddingConfig = EmbeddingConfig()
    poll_interval: float = 0.5
    max_loops: int | None = None
    watch_stability_seconds: float = DEFAULT_WATCH_STABILITY_SECONDS


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

        started_at = perf_counter()
        log_context = _job_log_context(job)
        LOGGER.info("job_started %s", log_context)
        try:
            result = await self._handle_job(job)
        except Exception as exc:
            await asyncio.to_thread(mark_job_failed, self.database_path, job.id, error=str(exc))
            LOGGER.error(
                "job_failed %s elapsed_ms=%d error=%r",
                log_context,
                _elapsed_milliseconds(started_at),
                str(exc),
            )
        else:
            await asyncio.to_thread(
                mark_job_done,
                self.database_path,
                job.id,
                result=result,
            )
            LOGGER.info(
                "job_completed %s elapsed_ms=%d",
                log_context,
                _elapsed_milliseconds(started_at),
            )
        return True

    async def _handle_job(self, job: Job) -> JobResult | None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            raise UnknownJobKindError(f"No handler registered for job kind '{job.kind}'")
        return await handler(job)


WatchStreamFactory = Callable[[tuple[str, ...]], AsyncIterator[set[tuple[object, str]]]]


async def run_daemon(
    config: DaemonConfig,
    *,
    handlers: Mapping[str, JobHandler] | None = None,
    watch_stream_factory: WatchStreamFactory | None = None,
) -> None:
    """Start the foreground daemon loop."""

    storage_paths = initialize_storage(config.data_dir)
    resolved_handlers = dict(handlers or {})
    if INGEST_JOB_KIND not in resolved_handlers:
        resolved_handlers[INGEST_JOB_KIND] = build_ingest_handler(
            data_dir=config.data_dir,
            embedding_config=config.embedding_config,
        )

    runner = DaemonRunner(
        database_path=storage_paths.database,
        handlers=resolved_handlers,
        poll_interval=config.poll_interval,
    )
    watches = list_watches(storage_paths.database)
    LOGGER.info(
        "daemon_started data_dir=%r poll_interval_seconds=%s watches=%d",
        str(config.data_dir),
        config.poll_interval,
        len(watches),
    )
    if not watches:
        await runner.run(max_loops=config.max_loops)
        return

    watch_paths = tuple(str(watch.path) for watch in watches)
    watch_task = _run_watch_loop(
        storage_paths.database,
        watch_paths,
        watch_stream_factory=watch_stream_factory or _default_watch_stream,
        max_batches=config.max_loops,
        stability_seconds=config.watch_stability_seconds,
    )
    await asyncio.gather(runner.run(max_loops=config.max_loops), watch_task)


async def _run_watch_loop(
    database_path: Path,
    watch_paths: tuple[str, ...],
    *,
    watch_stream_factory: WatchStreamFactory,
    max_batches: int | None,
    stability_seconds: float,
) -> None:
    batches = 0
    debouncer = WatchDebouncer(
        database_path=database_path,
        stability_seconds=stability_seconds,
    )
    async for changes in watch_stream_factory(watch_paths):
        await asyncio.to_thread(debouncer.consider_changes, changes)
        if stability_seconds > 0:
            await asyncio.sleep(stability_seconds)
        await asyncio.to_thread(debouncer.flush_ready)
        batches += 1
        if max_batches is not None and batches >= max_batches:
            return


async def _default_watch_stream(paths: tuple[str, ...]) -> AsyncIterator[set[tuple[object, str]]]:
    async for changes in awatch(*paths):
        yield {(change, str(path)) for change, path in changes}


def _job_log_context(job: Job) -> str:
    context = f"job_id={job.id} kind={job.kind}"
    source_path = job.payload.get("path")
    if isinstance(source_path, str):
        context += f" source_path={source_path!r}"
    return context


def _elapsed_milliseconds(started_at: float) -> int:
    return round((perf_counter() - started_at) * 1000)
