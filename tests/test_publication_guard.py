import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "publication_secrets",
    Path(__file__).resolve().parents[1] / "scripts/check_secrets.py",
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def git(directory, *arguments):
    subprocess.run(["git", *arguments], cwd=directory, check=True, capture_output=True)


def test_index_secret_is_detected_even_after_worktree_is_clean(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(guard, "ROOT", tmp_path)
    git(tmp_path, "init")
    path = tmp_path / "example.txt"
    fake = "sk-" + "publication-fixture-not-a-real-key-00000"
    path.write_text(fake)
    git(tmp_path, "add", "example.txt")
    path.write_text("sanitized working copy")
    assert guard.main(["--staged"]) == 1
    result = capsys.readouterr().out
    assert fake not in result and json.loads(result)["findings"] == ["example.txt"]


def test_index_keeps_public_examples_and_rejects_private_paths(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(guard, "ROOT", tmp_path)
    git(tmp_path, "init")
    (tmp_path / ".env.example").write_text("MODEL_API_KEY=replace-me")
    git(tmp_path, "add", ".env.example")
    assert guard.main(["--staged"]) == 0
    capsys.readouterr()
    (tmp_path / "postgres.env").write_text("synthetic local fixture")
    git(tmp_path, "add", "postgres.env")
    assert guard.main(["--staged"]) == 1
    assert json.loads(capsys.readouterr().out)["findings"] == ["postgres.env"]


@pytest.mark.parametrize(
    "name",
    [
        "artifacts/demo/report.json",
        "test.sqlite3-wal",
        "x-private/config.json",
        "service-tokens.json",
        ".env.local",
    ],
)
def test_private_paths_are_not_publication_candidates(name):
    assert guard.forbidden_public_path(name)


def test_no_repository_is_not_reported_as_a_clean_index(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(guard, "ROOT", tmp_path)
    assert guard.main(["--staged"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "git_index_unavailable"
