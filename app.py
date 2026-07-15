from __future__ import annotations

import errno
import shutil
import threading
import time
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse

LOG_DIRECTORY = Path("/var/log/storage-breaker")
LOG_FILE = LOG_DIRECTORY / "application.log"

# Long-term fix: cap the log file by size and keep a bounded number of
# rotated backups. This makes disk usage self-limiting regardless of the
# write rate, so the volume can no longer be filled to failure.
#
# Worst-case footprint = LOG_MAX_BYTES * (LOG_BACKUP_COUNT + 1).
LOG_MAX_BYTES = 200 * 1024**2
LOG_BACKUP_COUNT = 5

TARGET_LOG_BYTES = 10 * 1024**3

DURATION_HOURS = 2.5
DURATION_SECONDS = DURATION_HOURS * 60 * 60

# Each JSON log record is 64 KiB.
LOG_RECORD_BYTES = 64 * 1024

TOTAL_RECORDS = TARGET_LOG_BYTES / LOG_RECORD_BYTES
WRITE_INTERVAL_SECONDS = DURATION_SECONDS / TOTAL_RECORDS

HEALTH_FAILURE_THRESHOLD_PERCENT = 95.0

stop_event = threading.Event()
state_lock = threading.Lock()

writer_state: dict[str, Any] = {
    "status": "starting",
    "records_written": 0,
    "bytes_written_since_start": 0,
    "last_write_at": None,
    "error": None,
}


def update_writer_state(**values: Any) -> None:
    with state_lock:
        writer_state.update(values)


def gib(value: int) -> float:
    return round(value / 1024**3, 3)


def get_filesystem_status() -> dict[str, float]:
    usage = shutil.disk_usage(LOG_DIRECTORY)

    used_percent = (
        round((usage.used / usage.total) * 100, 2) if usage.total > 0 else 0.0
    )

    return {
        "total_gib": gib(usage.total),
        "used_gib": gib(usage.used),
        "free_gib": gib(usage.free),
        "used_percent": used_percent,
    }


def get_current_log_size() -> int:
    try:
        return LOG_FILE.stat().st_size
    except FileNotFoundError:
        return 0


def rotate_logs() -> None:
    """
    Rotate the log file when it reaches the size cap.

    Renames application.log -> application.log.1 and shifts existing
    backups up by one, discarding the oldest beyond LOG_BACKUP_COUNT.
    Recent history is preserved instead of being truncated away, and the
    writer creates a fresh application.log on its next open ("ab").
    """

    oldest = LOG_FILE.with_name(f"{LOG_FILE.name}.{LOG_BACKUP_COUNT}")
    if oldest.exists():
        oldest.unlink()

    for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
        source = LOG_FILE.with_name(f"{LOG_FILE.name}.{index}")
        if source.exists():
            source.rename(LOG_FILE.with_name(f"{LOG_FILE.name}.{index + 1}"))

    if LOG_FILE.exists():
        LOG_FILE.rename(LOG_FILE.with_name(f"{LOG_FILE.name}.1"))


def build_log_record(sequence: int) -> bytes:
    """
    Build one valid JSON log record with a fixed size.

    Each record is written on a single line, making the file compatible
    with standard JSON log collectors and command-line tools such as jq.
    """

    timestamp = datetime.now(timezone.utc).isoformat()

    log_entry = {
        "timestamp": timestamp,
        "level": "INFO",
        "service": "storage-breaker",
        "sequence": sequence,
        "event": "background_processing",
        "status": "completed",
        "message": "Background processing completed successfully",
    }

    # Serialize the meaningful fields first.
    serialized = json.dumps(
        log_entry,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    # Remove the final closing brace so a large payload field can be added.
    prefix = serialized[:-1] + ',"payload":"'
    suffix = '"}\n'

    prefix_bytes = prefix.encode("utf-8")
    suffix_bytes = suffix.encode("utf-8")

    payload_size = (
        LOG_RECORD_BYTES
        - len(prefix_bytes)
        - len(suffix_bytes)
    )

    if payload_size < 0:
        raise ValueError(
            "Configured log record size is too small for the JSON fields"
        )

    record = (
        prefix_bytes
        + (b"x" * payload_size)
        + suffix_bytes
    )

    if len(record) != LOG_RECORD_BYTES:
        raise RuntimeError(
            f"Unexpected record size: {len(record)} bytes"
        )

    return record


def log_writer() -> None:
    """
    Continuously generate application logs.

    The log file is opened for every record. This means that when logrotate
    renames the current file and creates a new application.log, subsequent
    writes naturally follow the new path instead of holding the old inode.
    """

    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

    sequence = 0
    bytes_written = 0
    next_write_at = time.monotonic()

    update_writer_state(
        status="writing",
        records_written=0,
        bytes_written_since_start=0,
        error=None,
    )

    while not stop_event.is_set():
        sequence += 1

        try:
            record = build_log_record(sequence)

            # Size-based rotation: cap the file before it can grow without
            # bound. This is the long-term fix for the disk-fill failure.
            if get_current_log_size() >= LOG_MAX_BYTES:
                rotate_logs()

            # Reopen on every write so log rotation works cleanly.
            with LOG_FILE.open("ab", buffering=0) as log_file:
                log_file.write(record)

            bytes_written += len(record)

            update_writer_state(
                status="writing",
                records_written=sequence,
                bytes_written_since_start=bytes_written,
                last_write_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )

        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                status = "disk_full"
                error_message = "No space left on device"
            else:
                status = "write_failed"
                error_message = f"{type(exc).__name__}: {exc}"

            update_writer_state(
                status=status,
                error=error_message,
            )

            # Keep the thread alive so it can recover after an operator
            # frees space or rotates/removes old logs.
            stop_event.wait(5)
            continue

        except Exception as exc:
            update_writer_state(
                status="write_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

            stop_event.wait(5)
            continue

        # Use a monotonic schedule to reduce timing drift.
        next_write_at += WRITE_INTERVAL_SECONDS
        sleep_seconds = max(0.0, next_write_at - time.monotonic())

        stop_event.wait(sleep_seconds)

    update_writer_state(status="stopped")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    stop_event.clear()

    writer_thread = threading.Thread(
        target=log_writer,
        name="storage-breaker-log-writer",
        daemon=True,
    )
    writer_thread.start()

    yield

    stop_event.set()
    writer_thread.join(timeout=15)


app = FastAPI(
    title="Storage Breaker",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
def health() -> JSONResponse:
    filesystem = get_filesystem_status()

    with state_lock:
        current_writer_state = dict(writer_state)

    writer_failed = current_writer_state["status"] in {
        "disk_full",
        "write_failed",
    }

    disk_critical = filesystem["used_percent"] >= HEALTH_FAILURE_THRESHOLD_PERCENT

    healthy = not writer_failed and not disk_critical

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "healthy" if healthy else "unhealthy",
        },
    )
