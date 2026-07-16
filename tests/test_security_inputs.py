import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_api import artifact_service, job_service, log_service, main as main_module
from web_api.path_security import validate_job_id
from web_api.schemas import WebSettings


def _settings() -> WebSettings:
    return WebSettings(
        reproduce={"provider": "openai", "model": "test-model", "api_key": "test-key"},
        evaluation={
            "provider": "openai",
            "model": "test-model",
            "api_key": "test-key",
        },
    )


def _job_form(**overrides: str) -> dict[str, str]:
    form = {
        "paper_name": "paper",
        "domain": "statistics",
        "eval_type": "ref_free",
        "generated_n": "1",
        "auto_refine": "false",
        "max_repair_rounds": "0",
        "console_output": "quiet",
        "skip_mineru": "false",
        "pdf_markdown_path": "",
    }
    form.update(overrides)
    return form


@pytest.fixture()
def client_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runs_dir = tmp_path / "runs"
    uploads_dir = tmp_path / ".local" / "uploads"
    runs_dir.mkdir()

    monkeypatch.setattr(artifact_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(log_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(job_service, "UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr(job_service, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(main_module, "load_settings", _settings)

    return TestClient(main_module.app), runs_dir, tmp_path


@pytest.mark.parametrize(
    "job_id",
    [
        "..",
        r"..\evil",
        "../evil",
        "..%5Cevil",
        "/tmp/evil",
        r"C:\Windows",
        "C:Windows",
    ],
)
def test_validate_job_id_rejects_traversal_and_absolute_paths(job_id: str) -> None:
    with pytest.raises(ValueError):
        validate_job_id(job_id)


@pytest.mark.parametrize(
    "encoded_job_id",
    [
        "%2E%2E",
        "..%5Cevil",
        "..%2Fevil",
        "%2Ftmp%2Fevil",
        "C%3A%5CWindows",
    ],
)
@pytest.mark.parametrize(
    "url_template",
    [
        "/jobs/{job_id}/repo/tree",
        "/jobs/{job_id}/repo/file?path=secret.txt",
        "/jobs/{job_id}/repo/download",
        "/jobs/{job_id}/logs",
    ],
)
def test_job_id_traversal_requests_are_rejected(
    client_context: tuple[TestClient, Path, Path],
    encoded_job_id: str,
    url_template: str,
) -> None:
    client, _, tmp_path = client_context
    escaped_run = tmp_path / "evil"
    escaped_repo = escaped_run / "repo"
    escaped_logs = escaped_run / "logs"
    escaped_repo.mkdir(parents=True, exist_ok=True)
    escaped_logs.mkdir(parents=True, exist_ok=True)
    (escaped_repo / "secret.txt").write_text("secret", encoding="utf-8")
    (escaped_logs / "secret.log").write_text("secret", encoding="utf-8")

    response = client.get(url_template.format(job_id=encoded_job_id))

    assert response.status_code in {400, 404}


def test_create_job_rejects_fake_pdf_content(
    client_context: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = client_context

    response = client.post(
        "/jobs",
        data=_job_form(),
        files={"file": ("paper.pdf", b"not actually a pdf", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_upload"


def test_create_job_rejects_external_markdown_path(
    client_context: tuple[TestClient, Path, Path],
) -> None:
    client, _, tmp_path = client_context
    external_markdown = tmp_path / "outside.md"
    external_markdown.write_text("# external", encoding="utf-8")

    response = client.post(
        "/jobs",
        data=_job_form(
            skip_mineru="true",
            pdf_markdown_path=str(external_markdown),
        ),
        files={"file": ("paper.pdf", b"%PDF-1.7\nbody", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_job_service_command_rejects_external_markdown_path(
    client_context: tuple[TestClient, Path, Path],
) -> None:
    _, _, tmp_path = client_context
    external_markdown = tmp_path / "outside.md"
    external_markdown.write_text("# external", encoding="utf-8")

    with pytest.raises(ValueError, match="runs directory"):
        job_service.build_pipeline_command(
            pdf_path=tmp_path / "paper.pdf",
            job_id="safe_job",
            paper_name="paper",
            settings=_settings(),
            domain="statistics",
            eval_type="ref_free",
            generated_n=1,
            auto_refine=False,
            max_repair_rounds=0,
            console_output="quiet",
            skip_mineru=True,
            pdf_markdown_path=str(external_markdown),
        )


def test_pipeline_rejects_external_skip_mineru_markdown(tmp_path: Path) -> None:
    codes_dir = Path(__file__).resolve().parents[1] / "codes"
    sys.path.insert(0, str(codes_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "run_pipeline_for_security_test",
            codes_dir / "run_pipeline.py",
        )
        assert spec is not None
        assert spec.loader is not None
        run_pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_pipeline)
    finally:
        sys.path.remove(str(codes_dir))

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    external_markdown = tmp_path / "outside.md"
    external_markdown.write_text("# external", encoding="utf-8")

    with pytest.raises(ValueError, match="runs directory"):
        run_pipeline.validate_markdown_path(str(external_markdown), str(runs_dir))
