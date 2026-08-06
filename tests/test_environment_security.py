import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from web_api import job_service, main as main_module, settings_store
from web_api.schemas import WebSettings


API_PREFIX = "/api/v1"
LOCAL_ORIGIN = "http://localhost"


def _settings(**overrides: object) -> WebSettings:
    data = {
        "reproduce": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "api_key": "reproduce-secret",
            "base_url": "https://reproduce.invalid/v1",
        },
        "evaluation": {
            "provider": "qwen",
            "model": "qwen3.7-max",
            "api_key": "eval-secret",
            "base_url": "https://evaluation.invalid/v1",
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
    assert env["REPRODUCE_BASE_URL"] == "https://reproduce.invalid/v1"
    assert env["EVAL_BASE_URL"] == "https://evaluation.invalid/v1"
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "DEEPSEEK_BASE_URL" not in env
    assert "MOONSHOT_API_KEY" not in env


def test_web_pipeline_env_uses_only_configured_base_urls(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://stale-deepseek.example/v1")
    settings = _settings(
        reproduce={
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "api_key": "reproduce-secret",
            "base_url": "https://configured-repro.example/v1",
        },
        evaluation={
            "provider": "qwen",
            "model": "qwen3.7-max",
            "api_key": "eval-secret",
            "base_url": "https://configured-eval.example/v1",
            "fallback_models": [],
        },
    )

    env = job_service.build_pipeline_env(settings)

    assert env["REPRODUCE_BASE_URL"] == "https://configured-repro.example/v1"
    assert env["EVAL_BASE_URL"] == "https://configured-eval.example/v1"
    assert "DEEPSEEK_BASE_URL" not in env


def test_web_pipeline_env_absolutizes_relative_registry_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "PAPER2CODE_PROVIDER_REGISTRY_PATH",
        str(Path("registries") / "providers.v1.json"),
    )

    env = job_service.build_pipeline_env(_settings())

    assert env["PAPER2CODE_PROVIDER_REGISTRY_PATH"] == str(
        tmp_path / "registries" / "providers.v1.json"
    )


def test_stage_subprocess_env_absolutizes_relative_registry_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_pipeline = _load_run_pipeline()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "PAPER2CODE_PROVIDER_REGISTRY_PATH",
        str(Path("registries") / "providers.v1.json"),
    )

    env = run_pipeline.build_clean_env()

    assert env["PAPER2CODE_PROVIDER_REGISTRY_PATH"] == str(
        tmp_path / "registries" / "providers.v1.json"
    )


def test_pipeline_role_env_maps_role_key_and_selected_provider_base_url(
    monkeypatch,
) -> None:
    run_pipeline = _load_run_pipeline()
    monkeypatch.setenv("OPENAI_API_KEY", "stale-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://stale-openai.example/v1")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://selected-deepseek.example/v1")
    monkeypatch.setenv("MOONSHOT_API_KEY", "stale-kimi-key")
    monkeypatch.setenv("REPRODUCE_API_KEY", "role-reproduce-secret")
    monkeypatch.delenv("REPRODUCE_BASE_URL", raising=False)

    env = run_pipeline.get_role_env("deepseek", "deepseek-v4-pro", "REPRODUCE")

    assert env["DEEPSEEK_API_KEY"] == "role-reproduce-secret"
    assert env["DEEPSEEK_BASE_URL"] == "https://selected-deepseek.example/v1"
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

    env = run_pipeline.get_role_env("qwen", "qwen3.7-max", "EVAL")

    assert env["QWEN_API_KEY"] == "role-eval-secret"
    assert env["QWEN_BASE_URL"] == "https://configured-eval.example/v1"
    assert "EVAL_API_KEY" not in env
    assert "EVAL_BASE_URL" not in env


def test_pipeline_reproduce_env_accepts_selected_provider_native_variables(
    monkeypatch,
) -> None:
    run_pipeline = _load_run_pipeline()
    monkeypatch.delenv("REPRODUCE_API_KEY", raising=False)
    monkeypatch.delenv("REPRODUCE_BASE_URL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "provider-reproduce-secret")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://provider-reproduce.invalid/v1")

    env = run_pipeline.get_role_env(
        "deepseek", "deepseek-v4-pro", "REPRODUCE"
    )

    run_pipeline.validate_provider_env("deepseek", "deepseek-v4-pro", env)
    assert env["DEEPSEEK_API_KEY"] == "provider-reproduce-secret"
    assert env["DEEPSEEK_BASE_URL"] == "https://provider-reproduce.invalid/v1"


def test_pipeline_eval_env_accepts_selected_provider_native_variables(monkeypatch) -> None:
    run_pipeline = _load_run_pipeline()
    monkeypatch.delenv("EVAL_API_KEY", raising=False)
    monkeypatch.delenv("EVAL_BASE_URL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "provider-eval-secret")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://provider-eval.invalid/v1")

    env = run_pipeline.get_role_env("deepseek", "deepseek-v4-flash", "EVAL")

    run_pipeline.validate_provider_env("deepseek", "deepseek-v4-flash", env)
    assert env["DEEPSEEK_API_KEY"] == "provider-eval-secret"
    assert env["DEEPSEEK_BASE_URL"] == "https://provider-eval.invalid/v1"


def test_pipeline_role_variables_override_provider_variables_without_cross_leak(
    monkeypatch,
) -> None:
    run_pipeline = _load_run_pipeline()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "provider-reproduce-secret")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://provider-reproduce.invalid/v1")
    monkeypatch.setenv("QWEN_API_KEY", "provider-eval-secret")
    monkeypatch.setenv("QWEN_BASE_URL", "https://provider-eval.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "unselected-openai-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unselected-openai.invalid/v1")
    monkeypatch.setenv("REPRODUCE_API_KEY", "role-reproduce-secret")
    monkeypatch.setenv("REPRODUCE_BASE_URL", "https://role-reproduce.invalid/v1")
    monkeypatch.setenv("EVAL_API_KEY", "role-eval-secret")
    monkeypatch.setenv("EVAL_BASE_URL", "https://role-eval.invalid/v1")

    reproduce_env = run_pipeline.get_role_env(
        "deepseek", "deepseek-v4-pro", "REPRODUCE"
    )
    eval_env = run_pipeline.get_role_env(
        "qwen", "qwen3.7-max", "EVAL", ("qwen3.7-plus",)
    )

    assert reproduce_env["DEEPSEEK_API_KEY"] == "role-reproduce-secret"
    assert reproduce_env["DEEPSEEK_BASE_URL"] == "https://role-reproduce.invalid/v1"
    assert "QWEN_API_KEY" not in reproduce_env
    assert "QWEN_BASE_URL" not in reproduce_env
    assert "EVAL_API_KEY" not in reproduce_env
    assert "EVAL_BASE_URL" not in reproduce_env
    assert eval_env["QWEN_API_KEY"] == "role-eval-secret"
    assert eval_env["QWEN_BASE_URL"] == "https://role-eval.invalid/v1"
    assert "DEEPSEEK_API_KEY" not in eval_env
    assert "DEEPSEEK_BASE_URL" not in eval_env
    assert "REPRODUCE_API_KEY" not in eval_env
    assert "REPRODUCE_BASE_URL" not in eval_env
    for env in (reproduce_env, eval_env):
        assert "OPENAI_API_KEY" not in env
        assert "OPENAI_BASE_URL" not in env

    run_pipeline.validate_provider_env("deepseek", "deepseek-v4-pro", reproduce_env)
    run_pipeline.validate_provider_env("qwen", "qwen3.7-max", eval_env)
    run_pipeline.validate_provider_env("qwen", "qwen3.7-plus", eval_env)


def test_pipeline_role_env_fails_closed_without_role_or_provider_credentials(
    monkeypatch,
) -> None:
    run_pipeline = _load_run_pipeline()
    for name in (
        "REPRODUCE_API_KEY",
        "REPRODUCE_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    env = run_pipeline.get_role_env(
        "deepseek", "deepseek-v4-pro", "REPRODUCE"
    )

    with pytest.raises(Exception) as exc_info:
        run_pipeline.validate_provider_env("deepseek", "deepseek-v4-pro", env)
    assert getattr(exc_info.value, "code", None) == "provider_api_key_missing"


def test_pipeline_role_env_uses_registry_fixed_base_url_as_final_fallback(
    monkeypatch,
) -> None:
    run_pipeline = _load_run_pipeline()
    contract = SimpleNamespace(
        provider_id="fake",
        model_id="fake-chat",
        api_key_env="FAKE_API_KEY",
        base_url_env="FAKE_BASE_URL",
        base_url="https://registry-fixed.invalid/v1",
    )

    class FakeRegistry:
        def get(self, provider_id, model_id):
            assert (provider_id, model_id) == ("fake", "fake-chat")
            return contract

        def resolve(self, provider_id, model_id, *, environ):
            assert environ["FAKE_API_KEY"] == "provider-native-secret"
            assert environ["FAKE_BASE_URL"] == "https://registry-fixed.invalid/v1"
            return object()

    monkeypatch.setattr(run_pipeline, "PROVIDER_REGISTRY", FakeRegistry())
    monkeypatch.setenv("FAKE_API_KEY", "provider-native-secret")
    monkeypatch.delenv("FAKE_BASE_URL", raising=False)
    monkeypatch.delenv("REPRODUCE_API_KEY", raising=False)
    monkeypatch.delenv("REPRODUCE_BASE_URL", raising=False)

    env = run_pipeline.get_role_env("fake", "fake-chat", "REPRODUCE")

    run_pipeline.validate_provider_env("fake", "fake-chat", env)
    assert env["FAKE_BASE_URL"] == "https://registry-fixed.invalid/v1"


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
            "model": "deepseek-v4-pro",
            "api_key": "super-secret-reproduce",
            "base_url": "https://reproduce.invalid/v1",
        },
        "evaluation": {
            "provider": "qwen",
            "model": "qwen3.7-max",
            "api_key": "super-secret-eval",
            "base_url": "https://evaluation.invalid/v1",
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
            "model": "deepseek-v4-pro",
            "api_key": "status-secret-reproduce",
            "base_url": "https://reproduce.invalid/v1",
        },
        evaluation={
            "provider": "qwen",
            "model": "qwen3.7-max",
            "api_key": "status-secret-eval",
            "base_url": "https://evaluation.invalid/v1",
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
