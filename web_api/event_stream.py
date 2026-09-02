from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from .job_repository import JobRepository


EVENT_STREAM_SCHEMA = "paper2code.job_event.v1"
STREAM_CONTROL_SCHEMA = "paper2code.stream_control.v1"
DEFAULT_REPLAY_LIMIT = 100
MAX_REPLAY_LIMIT = 500
DEFAULT_POLL_SECONDS = 0.25
DEFAULT_HEARTBEAT_SECONDS = 15.0
MAX_SSE_DATA_BYTES = 8192


def _json(data: dict[str, Any]) -> str:
    encoded = json.dumps(
        data,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > MAX_SSE_DATA_BYTES:
        raise ValueError("SSE event data is too large.")
    return encoded


def _format_sse(*, event_type: str, data: dict[str, Any], event_id: int | None = None) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {_json(data)}")
    return "\n".join(lines) + "\n\n"


def format_job_event(event: dict[str, Any]) -> str:
    payload_json = event.get("payload_json")
    payload: object = {}
    if payload_json:
        payload = json.loads(str(payload_json))
    event_id = int(event["id"])
    data = {
        "schema": EVENT_STREAM_SCHEMA,
        "event_id": event_id,
        "job_id": str(event["job_id"]),
        "event_type": str(event["event_type"]),
        "source": str(event["source"]),
        "job_version": int(event["job_version"]),
        "execution_status": event.get("execution_status"),
        "evaluation_status": event.get("evaluation_status"),
        "quality_status": event.get("quality_status"),
        "created_at": str(event["created_at"]),
        "payload": payload,
        "resync_required": False,
    }
    return _format_sse(
        event_id=event_id,
        event_type=str(event["event_type"]),
        data=data,
    )


def format_gap_event(
    *,
    job_id: str,
    last_event_id: int,
    replay_limit: int,
    available_event_count: int,
    latest_event_id: int | None,
) -> str:
    return _format_sse(
        event_type="stream.gap",
        data={
            "schema": STREAM_CONTROL_SCHEMA,
            "job_id": job_id,
            "last_event_id": last_event_id,
            "replay_limit": replay_limit,
            "available_event_count": available_event_count,
            "latest_event_id": latest_event_id,
            "resync_required": True,
            "reason": "replay_limit_exceeded",
        },
    )


async def iter_job_event_stream(
    request: Any,
    *,
    repository: JobRepository,
    job_id: str,
    last_event_id: int,
    replay_limit: int = DEFAULT_REPLAY_LIMIT,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    follow: bool = True,
) -> AsyncIterator[str]:
    cursor = last_event_id
    next_heartbeat = time.monotonic() + heartbeat_seconds
    while True:
        if await request.is_disconnected():
            return
        replay = repository.list_job_events_after(
            job_id,
            cursor,
            limit=replay_limit,
        )
        if replay.gap_detected:
            yield format_gap_event(
                job_id=job_id,
                last_event_id=cursor,
                replay_limit=replay_limit,
                available_event_count=replay.available_event_count,
                latest_event_id=replay.latest_event_id,
            )
            return
        for event in replay.events:
            yield format_job_event(event)
            cursor = int(event["id"])
            next_heartbeat = time.monotonic() + heartbeat_seconds
            if await request.is_disconnected():
                return
        if not follow:
            return
        now = time.monotonic()
        if now >= next_heartbeat:
            yield ": heartbeat\n\n"
            next_heartbeat = now + heartbeat_seconds
            continue
        await asyncio.sleep(max(0.001, min(poll_seconds, next_heartbeat - now)))
