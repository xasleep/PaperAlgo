import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import job_service, main as main_module
from web_api.database import connect_database
from web_api.job_repository import JobRepository


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"
EVIL_ORIGIN = "http://evil.example"


def _request() -> dict[str, object]:
    return {
        "upload_id": "0" * 32,
        "paper_name": "paper",
        "domain": "statistics",
        "eval_type": "ref_free",
        "generated_n": 1,
        "auto_refine": False,
        "max_repair_rounds": 0,
        "console_output": "quiet",
        "skip_mineru": False,
        "pdf_markdown_path": "",
    }


def _event_ids(text: str) -> list[int]:
    ids: list[int] = []
    for line in text.splitlines():
        if line.startswith("id: "):
            ids.append(int(line.removeprefix("id: ")))
    return ids


@pytest.fixture()
def sse_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_path = tmp_path / ".local" / "paper2code.db"
    monkeypatch.setenv("JOB_RUNTIME", "sqlite")
    monkeypatch.setenv("PAPER2CODE_DB_PATH", str(db_path))
    monkeypatch.setattr(job_service, "RUNS_DIR", tmp_path / "runs")
    main_module._SQLITE_REPOSITORIES.clear()
    repository = JobRepository(db_path)
    repository.create_job(job_id="sse_job", request=_request(), paper_name="paper")
    with TestClient(
        main_module.app,
        base_url=LOCAL_ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        yield client, repository, db_path
    main_module._SQLITE_REPOSITORIES.clear()


def test_initial_sse_connection_returns_persisted_event_and_headers(
    sse_client: tuple[TestClient, JobRepository, Path],
) -> None:
    client, _, _ = sse_client

    response = client.get(
        f"{API_PREFIX}/jobs/sse_job/events",
        headers={"Origin": LOCAL_ORIGIN, "Accept": "text/event-stream"},
        params={"follow": "false"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["connection"].lower() == "keep-alive"
    assert "event: job.created" in response.text
    assert '"schema":"paper2code.job_event.v1"' in response.text
    assert '"resync_required":false' in response.text


def test_last_event_id_replays_only_newer_persisted_events(
    sse_client: tuple[TestClient, JobRepository, Path],
) -> None:
    client, repository, _ = sse_client
    first_id = repository.list_job_events_after("sse_job", 0, limit=10).events[0][
        "id"
    ]
    repository.transition_job("sse_job", expected_version=1, execution_status="running")

    response = client.get(
        f"{API_PREFIX}/jobs/sse_job/events",
        headers={"Origin": LOCAL_ORIGIN, "Last-Event-ID": str(first_id)},
        params={"follow": "false"},
    )

    assert response.status_code == 200
    assert _event_ids(response.text) == [first_id + 1]
    assert "event: job.status_changed" in response.text
    assert "job.created" not in response.text


def test_sse_replays_from_sqlite_after_api_repository_cache_is_recreated(
    sse_client: tuple[TestClient, JobRepository, Path],
) -> None:
    client, repository, db_path = sse_client
    repository.transition_job("sse_job", expected_version=1, execution_status="running")
    main_module._SQLITE_REPOSITORIES.clear()

    response = client.get(
        f"{API_PREFIX}/jobs/sse_job/events",
        headers={"Origin": LOCAL_ORIGIN},
        params={"follow": "false"},
    )

    assert response.status_code == 200
    assert _event_ids(response.text) == [1, 2]
    with closing(connect_database(db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_events").fetchone()[0] == 2


def test_sse_replay_limit_reports_gap_without_silently_skipping_events(
    sse_client: tuple[TestClient, JobRepository, Path],
) -> None:
    client, repository, _ = sse_client
    repository.transition_job("sse_job", expected_version=1, execution_status="running")
    repository.record_job_event(
        "sse_job",
        event_type="job.synthetic_progress",
        source="worker",
        job_version=2,
        execution_status="running",
        evaluation_status="pending",
        quality_status="pending",
    )

    response = client.get(
        f"{API_PREFIX}/jobs/sse_job/events",
        headers={"Origin": LOCAL_ORIGIN},
        params={"follow": "false", "replay_limit": "1"},
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: stream.gap" in response.text
    assert '"resync_required":true' in response.text
    assert "job.created" not in response.text
    assert "job.status_changed" not in response.text


def test_sse_heartbeat_has_no_persisted_event_id(
    sse_client: tuple[TestClient, JobRepository, Path],
) -> None:
    _, repository, _ = sse_client

    async def consume_one() -> str:
        from web_api.event_stream import iter_job_event_stream

        class DisconnectAfterHeartbeat:
            calls = 0

            async def is_disconnected(self) -> bool:
                self.calls += 1
                return self.calls > 2

        stream = iter_job_event_stream(
            DisconnectAfterHeartbeat(),
            repository=repository,
            job_id="sse_job",
            last_event_id=1,
            replay_limit=10,
            poll_seconds=0.001,
            heartbeat_seconds=0.001,
        )
        return await anext(stream)

    event = asyncio.run(consume_one())

    assert event == ": heartbeat\n\n"


def test_sse_slow_consumer_and_disconnect_do_not_prefetch_unbounded_events(
    sse_client: tuple[TestClient, JobRepository, Path],
) -> None:
    _, repository, _ = sse_client
    for index in range(5):
        repository.record_job_event(
            "sse_job",
            event_type=f"job.progress_{index}",
            source="worker",
            job_version=1,
            execution_status="queued",
            evaluation_status="pending",
            quality_status="pending",
        )

    class CountingRepository(JobRepository):
        calls = 0

        def list_job_events_after(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return super().list_job_events_after(*args, **kwargs)

    counting = CountingRepository(repository.database_path)

    async def consume() -> tuple[str, bool, int]:
        from web_api.event_stream import iter_job_event_stream

        class DisconnectAfterFirstEvent:
            calls = 0

            async def is_disconnected(self) -> bool:
                self.calls += 1
                return self.calls > 1

        stream = iter_job_event_stream(
            DisconnectAfterFirstEvent(),
            repository=counting,
            job_id="sse_job",
            last_event_id=0,
            replay_limit=10,
            poll_seconds=0.001,
            heartbeat_seconds=60,
        )
        first = await anext(stream)
        try:
            await anext(stream)
        except StopAsyncIteration:
            stopped = True
        else:
            stopped = False
        return first, stopped, counting.calls

    first_event, stopped, calls = asyncio.run(consume())

    assert first_event.startswith("id: 1\n")
    assert stopped is True
    assert calls == 1


def test_repository_rejects_illegal_or_sensitive_oversized_events(
    sse_client: tuple[TestClient, JobRepository, Path],
) -> None:
    _, repository, db_path = sse_client

    with pytest.raises(ValueError, match="event_type"):
        repository.record_job_event(
            "sse_job",
            event_type="job.bad\nname",
            source="worker",
            job_version=1,
        )
    with pytest.raises(ValueError, match="sensitive"):
        repository.record_job_event(
            "sse_job",
            event_type="job.secret",
            source="worker",
            job_version=1,
            payload={"api_key": "SECRET"},
        )
    with pytest.raises(ValueError, match="local path"):
        repository.record_job_event(
            "sse_job",
            event_type="job.local_path",
            source="worker",
            job_version=1,
            payload={"note": "C:\\Users\\12572\\secret.txt"},
        )
    with pytest.raises(ValueError, match="too large"):
        repository.record_job_event(
            "sse_job",
            event_type="job.too_large",
            source="worker",
            job_version=1,
            payload={"text": "x" * 5000},
        )
    with closing(connect_database(db_path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, event_type, source, job_version,
                    execution_status, evaluation_status, quality_status,
                    created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sse_job",
                    "job.too_large",
                    "worker",
                    1,
                    "queued",
                    "pending",
                    "pending",
                    "2026-09-02T00:00:00.000Z",
                    "x" * 5000,
                ),
            )


def test_sse_source_protection_rejects_cross_site_before_streaming(
    sse_client: tuple[TestClient, JobRepository, Path],
) -> None:
    client, _, _ = sse_client

    response = client.get(
        f"{API_PREFIX}/jobs/sse_job/events",
        headers={"Origin": EVIL_ORIGIN, "Accept": "text/event-stream"},
        params={"follow": "false"},
    )

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "origin_not_allowed"


def test_sse_stream_does_not_expose_sensitive_control_plane_fields(
    sse_client: tuple[TestClient, JobRepository, Path],
) -> None:
    client, repository, _ = sse_client
    repository.record_job_event(
        "sse_job",
        event_type="job.safe_progress",
        source="worker",
        job_version=1,
        execution_status="queued",
        evaluation_status="pending",
        quality_status="pending",
        payload={"stage": "planning"},
    )

    response = client.get(
        f"{API_PREFIX}/jobs/sse_job/events",
        headers={"Origin": LOCAL_ORIGIN},
        params={"follow": "false"},
    )

    assert response.status_code == 200
    lowered = response.text.lower()
    for forbidden in (
        "api_key",
        "authorization",
        "prompt",
        "model_response",
        "base_url",
        "launch_token",
        "instance_token",
        "process_create_time",
        "process_group_id",
        "command_summary",
        "run_dir",
        "status_path",
    ):
        assert forbidden not in lowered
