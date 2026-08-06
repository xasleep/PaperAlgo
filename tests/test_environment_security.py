import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from web_api import job_service, main as main_module, settings_store
from web_api.schemas import WebSettings


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"


def _settings(**overrides: object) -> WebSettings:
    data = {
        "reproduce": {
            "provider": "deepseek",
            "model": "deepseek-test",
            "api_key": "reproduce-secret",
            "base_url": "",
        },
        "evaluation": {
            "provider": "qwen",
            "model": "qwen-test",
            "api_key": "eval-secret",
            "base_url": "",
            "fallback_models": [],
        },
    }
    data.update(overrides)
    return WebSettings(**data)


def _load_run_pipeline():
    codes_dir = Path(__file__).resolve().parents[1] / "codes"
    sys.path.insert(0, str(codes_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "run_pipeline_for_env_test",
            codes_dir / "run_pipeline.py",
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(codes_dir))


def _contains_key(data: object, key_name: str) -> bool:
    if isinstance(data, dict):
        return key_name in data or any(_contains_key(value, key_name) for value in data.values())
    if isinstance(data, list):
        return any(_contains_key(item, key_name) for item in data)
    return False


def test_web_pipeline_env_does_not_inherit_stale_provider_variables(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "stale-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://stale-openai.example/v1")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://stale-deepseek.example/v1")
    monkeypatch.setenv("MOONSHOT_API_KEY", "stale-kimi-key")

    env = job_service.build_pipeline_env(_settings())

    assert env["REPRODUCE_API_KEY"] == "reproduce-secret"
    assert env["EVAL_API_KEY"] == "eval-secret"
    assert "REPRODUCE_BASE_URL" not in env
    assert "EVAL_BASE_URL" not in env
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "DEEPSEEK_BASE_URL" not in env
    assert "MOONSHOT_API_KEY" not in env


def test_web_pipeline_env_uses_only_configured_base_urls(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://stale-deepseek.example/v1")
    settings = _settings(
        reproduce={
            "provider": "deepseek",
            "model": "deepseek-test",
            "api_key": "reproduce-secret",
            "base_url": "https://configured-repro.example/v1",
        },
        evaluation={
            "provider": "qwen",
            "model": "qwen-test",
            "api_key": "eval-secret",
            "base_url": "https://configured-eval.example/v1",
            "fallback_models": [],
        },
    )

    env = job_service.build_pipeline_env(settings)

    assert env["REPRODUCE_BASE_URL"] == "https://configured-repro.example/v1"
    assert env["EVAL_BASE_URL"] == "https://configured-eval.example/v1"
    assert "DEEPSEEK_BASE_URL" not in env


def test_pipeline_role_env_cleans_provider_variables_and_maps_role_only(
    monkeypatch,
) -> None:
    run_pipeline = _load_run_pipeline()
    monkeypatch.setenv("OPENAI_API_KEY", "stale-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://stale-openai.example/v1")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://stale-deepseek.example/v1")
    monkeypatch.setenv("MOONSHOT_API_KEY", "stale-kimi-key")
    monkeypatch.setenv("REPRODUCE_API_KEY", "role-reproduce-secret")
    monkeypatch.delenv("REPRODUCE_BASE_URL", raising=False)

    env = run_pipeline.get_role_env("deepseek", "REPRODUCE")

    assert env["DEEPSEEK_API_KEY"] == "role-reproduce-secret"
    assert "DEEPSEEK_BASE_URL" not in env
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "MOONSHOT_API_KEY" not in env
    assert "REPRODUCE_API_KEY" not in env


def test_pipeline_default_stage_env_cleans_provider_variables(monkeypatch) -> None:
    run_pipeline = _load_run_pipeline()
    monkeypatch.setenv("OPENAI_API_KEY", "stale-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://stale-openai.example/v1")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://stale-deepseek.example/v1")
    monkeypatch.setenv("REPRODUCE_API_KEY", "role-reproduce-secret")

    env = run_pipeline.build_clean_env()

    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "DEEPSEEK_BASE_URL" not in env
    assert "REPRODUCE_API_KEY" not in env


def test_pipeline_role_env_uses_configured_role_base_url(monkeypatch) -> None:
    run_pipeline = _load_run_pipeline()
    monkeypatch.setenv("OPENAI_BASE_URL", "https://stale-openai.example/v1")
    monkeypatch.setenv("EVAL_API_KEY", "role-eval-secret")
    monkeypatch.setenv("EVAL_BASE_URL", "https://configured-eval.example/v1")

    env = run_pipeline.get_role_env("qwen", "EVAL")

    assert env["OPENAI_API_KEY"] == "role-eval-secret"
    assert env["OPENAI_BASE_URL"] == "https://configured-eval.example/v1"
    assert "EVAL_API_KEY" not in env
    assert "EVAL_BASE_URL" not in env


def test_settings_endpoints_do_not_echo_api_keys(monkeypatch, tmp_path: Path) -> None:
    settings_path = tmp_path / "web_settings.json"
    monkeypatch.setattr(settings_store, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", settings_path)
    client = TestClient(main_module.app, base_url=LOCAL_ORIGIN)
    session = client.get(f"{API_PREFIX}/session", headers={"Origin": LOCAL_ORIGIN})
    client.headers.update(
        {"Origin": LOCAL_ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]}
    )
    payload = {
        "reproduce": {
            "provider": "deepseek",
            "model": "deepseek-test",
            "api_key": "super-secret-reproduce",
            "base_url": "",
        },
        "evaluation": {
            "provider": "qwen",
            "model": "qwen-test",
            "api_key": "super-secret-eval",
            "base_url": "",
            "fallback_models": [],
        },
    }

    post_response = client.post(f"{API_PREFIX}/settings", json=payload)
    status_response = client.get(f"{API_PREFIX}/settings/status")

    assert post_response.status_code == 200
    assert status_response.status_code == 200
    for response in [post_response, status_response]:
        response_text = response.text
        assert "super-secret-reproduce" not in response_text
        assert "super-secret-eval" not in response_text
        body = response.json()
        assert body["reproduce"]["has_api_key"] is True
        assert body["evaluation"]["has_api_key"] is True
        assert not _contains_key(body, "api_key")


def test_start_job_does_not_persist_api_keys_to_status_command_or_log(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}

    class FakePopen:
        pid = 123456

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]

        def poll(self):
            return None

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(job_service, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(job_service.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(job_service.threading, "Thread", FakeThread)
    settings = _settings(
        reproduce={
            "provider": "deepseek",
            "model": "deepseek-test",
            "api_key": "status-secret-reproduce",
            "base_url": "",
        },
        evaluation={
            "provider": "qwen",
            "model": "qwen-test",
            "api_key": "status-secret-eval",
            "base_url": "",
            "fallback_models": [],
        },
    )

    try:
        job_service.start_job(
            job_id="secret_job",
            pdf_path=tmp_path / "paper.pdf",
            paper_name="paper",
            settings=settings,
            domain="statistics",
            eval_type="ref_free",
            generated_n=1,
            auto_refine=False,
            max_repair_rounds=0,
            console_output="quiet",
        )

        status_text = (runs_dir / "secret_job" / "run_status.json").read_text(
            encoding="utf-8"
        )
        command_text = " ".join(str(part) for part in captured["cmd"])
        log_text = (
            runs_dir / "secret_job" / "logs" / "00_web_api_pipeline.log"
        ).read_text(encoding="utf-8")

        for text in [status_text, command_text, log_text]:
            assert "status-secret-reproduce" not in text
            assert "status-secret-eval" not in text
        assert captured["env"]["REPRODUCE_API_KEY"] == "status-secret-reproduce"
        assert captured["env"]["EVAL_API_KEY"] == "status-secret-eval"
    finally:
        log_file = job_service.ACTIVE_LOG_FILES.pop("secret_job", None)
        if log_file is not None:
            log_file.close()
        job_service.ACTIVE_PROCESSES.pop("secret_job", None)
