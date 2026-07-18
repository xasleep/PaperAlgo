import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


CODES_DIR = Path(__file__).resolve().parents[1] / "codes"
sys.path.insert(0, str(CODES_DIR))

import task_manifest  # noqa: E402
from task_manifest import (  # noqa: E402
    DEFAULT_TASK_MANIFEST_POLICY,
    MAX_COMPONENT_LENGTH,
    MAX_PATH_DEPTH,
    MAX_RELATIVE_PATH_LENGTH,
    MAX_TASK_FILES,
    DuplicateTaskPathError,
    InvalidTaskPathError,
    TaskManifestError,
    TaskManifestLimitError,
    TaskFile,
    TaskManifestPolicy,
    UnsafeTaskWriteError,
    load_task_manifest,
    parse_task_manifest,
    parse_task_manifest_mapping,
    read_manifest_text_files,
    safe_join,
    safe_write_text,
    save_task_manifest,
    task_artifact_key,
    validate_repair_paths,
    validate_task_path,
)


TASK_MANIFEST_CONSUMERS = (
    "2_analyzing.py",
    "2_analyzing_llm.py",
    "3_coding.py",
    "3_coding_llm.py",
    "3.1_coding_sh.py",
    "4_debugging.py",
    "eval.py",
)

PLANNING_PRODUCERS = ("1_planning.py", "1_planning_llm.py")


@pytest.mark.parametrize(
    "path",
    [
        "config.yaml",
        "main.R",
        "simulation.R",
        "src/main.py",
        "src/models/model.py",
        "scripts/run.sh",
        "README.md",
    ],
)
def test_validate_task_path_accepts_supported_outputs(path: str) -> None:
    task_file = validate_task_path(path)

    assert task_file.relative_path == path
    assert task_file.parts == tuple(path.split("/"))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../../outside.py",
        "../outside.py",
        "sub/../../outside.py",
        r"..\outside.py",
        r"C:\outside.py",
        "C:/outside.py",
        r"\\server\share\x.py",
        "/tmp/outside.py",
        "a:b.py",
        "a//b.py",
        "a/./b.py",
        "a/../b.py",
        "CON.py",
        "dir/NUL.txt",
        "COM1",
        "name.",
        "name.py ",
        "name\x00.py",
        "name\x1f.py",
        "payload.exe",
        "trailing/",
        "dir/ends-in-space /main.py",
        'bad<name.py',
        'bad>name.py',
        'bad"name.py',
        "bad|name.py",
        "bad?name.py",
        "bad*name.py",
    ],
)
def test_validate_task_path_rejects_dangerous_values(path: str) -> None:
    with pytest.raises(InvalidTaskPathError):
        validate_task_path(path)


@pytest.mark.parametrize("value", [None, 42, {}, ["main.py"]])
def test_validate_task_path_rejects_non_strings(value: object) -> None:
    with pytest.raises(InvalidTaskPathError):
        validate_task_path(value)


def test_manifest_preserves_order_and_exposes_versioned_validated_files() -> None:
    manifest = parse_task_manifest(["config.yaml", "src/main.py", "README.md"])

    assert manifest.version == 1
    assert manifest.policy == DEFAULT_TASK_MANIFEST_POLICY
    assert manifest.paths == ("config.yaml", "src/main.py", "README.md")
    assert tuple(task.relative_path for task in manifest.files) == manifest.paths


def test_versioned_manifest_round_trip_revalidates_paths(tmp_path: Path) -> None:
    manifest = parse_task_manifest(["config.yaml", "src/main.py"])

    saved_path = save_task_manifest(tmp_path, manifest)
    loaded = load_task_manifest(tmp_path)

    assert saved_path.name == "task_manifest.json"
    assert loaded.version == 1
    assert loaded.paths == manifest.paths


def test_versioned_manifest_rejects_unknown_version(tmp_path: Path) -> None:
    (tmp_path / "task_manifest.json").write_text(
        '{"version": 2, "files": [{"path": "main.py"}]}',
        encoding="utf-8",
    )

    with pytest.raises(TaskManifestError, match="version"):
        load_task_manifest(tmp_path)


def test_versioned_manifest_rejects_tampered_path(tmp_path: Path) -> None:
    (tmp_path / "task_manifest.json").write_text(
        '{"version": 1, "files": [{"path": "../../outside.py"}]}',
        encoding="utf-8",
    )

    with pytest.raises(InvalidTaskPathError):
        load_task_manifest(tmp_path)


def test_manifest_rejects_empty_task_list() -> None:
    with pytest.raises(TaskManifestError, match="at least one"):
        parse_task_manifest([])


def test_manifest_mapping_rejects_empty_task_list() -> None:
    with pytest.raises(TaskManifestError, match="at least one"):
        parse_task_manifest_mapping({"Task list": []})


def test_versioned_manifest_rejects_empty_files(tmp_path: Path) -> None:
    (tmp_path / "task_manifest.json").write_text(
        '{"version": 1, "files": []}',
        encoding="utf-8",
    )

    with pytest.raises(TaskManifestError, match="at least one"):
        load_task_manifest(tmp_path)


def test_manifest_requires_a_list_of_strings() -> None:
    with pytest.raises(TaskManifestError):
        parse_task_manifest("main.py")


@pytest.mark.parametrize("key", ["Task list", "task_list", "task list"])
def test_manifest_mapping_accepts_supported_task_list_keys(key: str) -> None:
    manifest = parse_task_manifest_mapping({key: ["src/main.py"]})

    assert manifest.paths == ("src/main.py",)


def test_manifest_mapping_rejects_missing_task_list_instead_of_becoming_empty() -> None:
    with pytest.raises(TaskManifestError, match="Task list"):
        parse_task_manifest_mapping({"Logic Analysis": []})


def test_manifest_rejects_case_insensitive_duplicates() -> None:
    with pytest.raises(DuplicateTaskPathError):
        parse_task_manifest(["A.py", "a.py"])


def test_manifest_rejects_unicode_normalization_duplicates() -> None:
    with pytest.raises(DuplicateTaskPathError):
        parse_task_manifest(["caf\u00e9.py", "cafe\u0301.py"])


def test_manifest_rejects_too_many_files() -> None:
    paths = [f"file_{index}.py" for index in range(MAX_TASK_FILES + 1)]

    with pytest.raises(TaskManifestLimitError):
        parse_task_manifest(paths)


def test_manifest_rejects_excessive_depth() -> None:
    path = "/".join(["dir"] * MAX_PATH_DEPTH + ["main.py"])

    with pytest.raises(TaskManifestLimitError):
        parse_task_manifest([path])


def test_manifest_rejects_excessive_component_length() -> None:
    path = f"{'a' * (MAX_COMPONENT_LENGTH + 1)}.py"

    with pytest.raises(TaskManifestLimitError):
        parse_task_manifest([path])


def test_manifest_rejects_excessive_total_path_length() -> None:
    first = "a" * 90
    second = "b" * (MAX_RELATIVE_PATH_LENGTH - len(first) - 1 - 2)
    path = f"{first}/{second}.py"
    assert len(path) > MAX_RELATIVE_PATH_LENGTH

    with pytest.raises(TaskManifestLimitError):
        parse_task_manifest([path])


def test_manifest_limits_and_extensions_are_configurable() -> None:
    policy = TaskManifestPolicy(
        max_files=1,
        max_relative_path_length=20,
        max_path_depth=2,
        max_component_length=12,
        allowed_extensions=frozenset({".jl"}),
    )

    assert parse_task_manifest(["src/main.jl"], policy=policy).paths == (
        "src/main.jl",
    )
    with pytest.raises(TaskManifestLimitError):
        parse_task_manifest(["a.jl", "b.jl"], policy=policy)
    with pytest.raises(InvalidTaskPathError, match="extension"):
        parse_task_manifest(["main.py"], policy=policy)

    assert DEFAULT_TASK_MANIFEST_POLICY.max_files == MAX_TASK_FILES


def test_custom_policy_round_trip_and_repair_validation_are_consistent(
    tmp_path: Path,
) -> None:
    policy = TaskManifestPolicy(allowed_extensions=frozenset({".jl"}))
    manifest = parse_task_manifest(["src/main.jl"], policy=policy)

    assert manifest.policy == policy
    saved_path = save_task_manifest(tmp_path, manifest)
    assert '"policy"' not in saved_path.read_text(encoding="utf-8")
    loaded = load_task_manifest(tmp_path, policy=policy)

    assert loaded.policy == policy
    assert loaded.paths == ("src/main.jl",)
    selected = validate_repair_paths(["src/main.jl"], loaded)
    assert tuple(task.relative_path for task in selected) == ("src/main.jl",)

    with pytest.raises(InvalidTaskPathError, match="not present"):
        validate_repair_paths(["src/other.jl"], loaded)
    with pytest.raises(InvalidTaskPathError, match="extension"):
        load_task_manifest(tmp_path)


def test_artifact_keys_are_stable_safe_and_collision_resistant() -> None:
    nested = validate_task_path("a/b.py")
    flat = validate_task_path("a_b.py")

    nested_key = task_artifact_key(nested)
    assert nested_key == task_artifact_key(nested)
    assert nested_key != task_artifact_key(flat)
    assert re.fullmatch(r"[A-Za-z0-9._-]+", nested_key)


def test_artifact_key_leaves_room_for_longest_stage_suffix() -> None:
    task_file = validate_task_path(f"{'a' * 97}.py")
    artifact_name = (
        f"{task_artifact_key(task_file)}_simple_analysis_trajectories.json"
    )

    assert len(artifact_name) <= MAX_COMPONENT_LENGTH
    assert validate_task_path(artifact_name).relative_path == artifact_name


def test_task_file_cannot_be_forged_without_validation() -> None:
    with pytest.raises(InvalidTaskPathError, match="validate_task_path"):
        TaskFile(
            relative_path="../../outside.py",
            parts=("..", "..", "outside.py"),
            canonical_key="../../outside.py",
            _validation_token=object(),
        )


def test_analysis_and_coding_use_the_same_artifact_key_function() -> None:
    task_file = parse_task_manifest(["src/models/model.py"]).files[0]

    analysis_key = task_artifact_key(task_file)
    coding_key = task_artifact_key(task_file)

    assert analysis_key == coding_key


def test_safe_write_preserves_nested_directory_structure(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    task_file = validate_task_path("src/models/model.py")

    target = safe_write_text(repo_root, task_file, "print('safe')\n")

    assert target == (repo_root / "src" / "models" / "model.py").resolve()
    assert target.read_text(encoding="utf-8") == "print('safe')\n"


def test_manifest_reader_only_reads_validated_manifest_members(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    safe_write_text(
        repo_root,
        validate_task_path("src/main.py"),
        "print('manifest')\n",
    )
    safe_write_text(
        repo_root,
        validate_task_path("src/unlisted.py"),
        "print('unlisted')\n",
    )
    manifest = parse_task_manifest(["src/main.py", "README.md"])

    files = read_manifest_text_files(repo_root, manifest, allowed_extensions={".py"})

    assert files == {"src/main.py": "print('manifest')\n"}


def test_task_manifest_consumers_do_not_use_bulk_repo_readers() -> None:
    for file_name in TASK_MANIFEST_CONSUMERS:
        source = (CODES_DIR / file_name).read_text(encoding="utf-8")
        assert "read_python_files(" not in source
        if file_name == "eval.py":
            papercoder_source = source.split("    if is_papercoder:\n", 1)[1]
            papercoder_source = papercoder_source.split("\n    else:\n", 1)[0]
            assert "read_all_files(" not in papercoder_source


def test_planning_persists_manifest_and_all_stages_load_the_same_boundary() -> None:
    for file_name in PLANNING_PRODUCERS:
        source = (CODES_DIR / file_name).read_text(encoding="utf-8")
        assert "parse_task_manifest_mapping(" in source
        assert "save_task_manifest(" in source

    for file_name in TASK_MANIFEST_CONSUMERS:
        source = (CODES_DIR / file_name).read_text(encoding="utf-8")
        assert "load_task_manifest(" in source
        assert "parse_task_manifest_mapping(" not in source


def test_debug_backup_does_not_move_manifest_file_or_swallow_io_errors() -> None:
    source = (CODES_DIR / "4_debugging.py").read_text(encoding="utf-8")

    assert "os.replace(filepath, backup_path)" not in source
    assert "except (OSError, UnicodeError)" not in source


def test_traversal_write_does_not_touch_outside_sentinel(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    before_stat = sentinel.stat()
    outside_before = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
        if repo_root not in path.parents and path != repo_root
    }

    with pytest.raises(InvalidTaskPathError):
        safe_write_text(repo_root, "../../sentinel.txt", "attacker controlled")

    outside_after = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
        if repo_root not in path.parents and path != repo_root
    }
    after_stat = sentinel.stat()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert outside_after == outside_before


def test_atomic_write_failure_preserves_existing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    task_file = validate_task_path("main.py")
    target = safe_write_text(repo_root, task_file, "old content")

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(task_manifest.os, "replace", fail_replace)

    with pytest.raises(UnsafeTaskWriteError, match="simulated replace failure"):
        safe_write_text(repo_root, task_file, "new content")

    assert target.read_text(encoding="utf-8") == "old content"
    assert list(target.parent.glob(".*.tmp")) == []


def test_repair_paths_must_belong_to_manifest() -> None:
    manifest = parse_task_manifest(["src/main.py", "README.md"])

    selected = validate_repair_paths(["src/main.py"], manifest)
    assert tuple(task.relative_path for task in selected) == ("src/main.py",)

    with pytest.raises(InvalidTaskPathError, match="not present"):
        validate_repair_paths(["other.py"], manifest)

    with pytest.raises(InvalidTaskPathError):
        validate_repair_paths(["../../outside.py"], manifest)


@pytest.mark.parametrize("raw_paths", ["", {}, 0, False])
def test_repair_paths_reject_falsey_non_list_values(raw_paths: object) -> None:
    manifest = parse_task_manifest(["src/main.py"])

    with pytest.raises(TaskManifestError, match="files_to_repair"):
        validate_repair_paths(raw_paths, manifest)


def test_repair_consumer_does_not_coerce_invalid_paths_to_empty_list() -> None:
    source = (CODES_DIR / "3_coding.py").read_text(encoding="utf-8")

    assert 'repair_feedback.get("files_to_repair") or []' not in source


def test_safe_join_rejects_symlink_parent_and_target_when_supported(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo_root.mkdir()
    outside.mkdir()
    outside_file = outside / "outside.py"
    outside_file.write_text("outside", encoding="utf-8")
    parent_link = repo_root / "linked"
    target_link = repo_root / "main.py"
    try:
        parent_link.symlink_to(outside, target_is_directory=True)
        target_link.symlink_to(outside_file)
    except OSError as exc:
        pytest.skip(f"symlink creation is not permitted on this Windows host: {exc}")

    with pytest.raises(UnsafeTaskWriteError, match="link|reparse"):
        safe_join(repo_root, validate_task_path("linked/new.py"))
    with pytest.raises(UnsafeTaskWriteError, match="link|reparse"):
        safe_join(repo_root, validate_task_path("main.py"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction test")
def test_safe_join_rejects_junction_parent(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo_root.mkdir()
    outside.mkdir()
    junction = repo_root / "linked"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation failed: {result.stderr or result.stdout}")
    try:
        with pytest.raises(UnsafeTaskWriteError, match="link|reparse"):
            safe_join(repo_root, validate_task_path("linked/new.py"))
    finally:
        junction.rmdir()


def test_limit_constants_match_the_security_contract() -> None:
    assert MAX_TASK_FILES == 64
    assert MAX_RELATIVE_PATH_LENGTH == 180
    assert MAX_PATH_DEPTH == 8
    assert MAX_COMPONENT_LENGTH == 100
