"""Тесты инструментов: слои валидации, границы Bash, защита путей."""

from __future__ import annotations

import pytest

from agent.tools.base import (
    DENIED_BY_POLICY,
    FILE_NOT_FOUND,
    PATH_OUTSIDE_WORKSPACE,
    ToolContext,
    execute,
)
from agent.tools.bash import BASH
from agent.tools.fs import LIST_FILES, READ_FILE, WRITE_FILE

from conftest import make_cfg


def make_ctx(cfg, cap=10_000, bash_timeout=None):
    return ToolContext(
        workspace=cfg.workspace,
        max_output_bytes=cap,
        bash_timeout=bash_timeout if bash_timeout is not None else cfg.bash_timeout,
        bash_path=cfg.bash_path,
        protected_paths=cfg.protected_paths,
    )


# ------------------------------------------------------------------- fs

def test_fs_roundtrip(workspace, runs_dir):
    cfg = make_cfg(workspace, runs_dir)
    ctx = make_ctx(cfg)
    result = execute(WRITE_FILE, {"path": "src/new.py", "content": "x = 1"}, ctx)
    assert result["ok"]
    assert (workspace / "src" / "new.py").read_text(encoding="utf-8") == "x = 1"
    result = execute(READ_FILE, {"path": "src/new.py"}, ctx)
    assert result["ok"] and result["data"]["content"] == "x = 1"


def test_read_missing_file(workspace, runs_dir):
    ctx = make_ctx(make_cfg(workspace, runs_dir))
    result = execute(READ_FILE, {"path": "nope.py"}, ctx)
    assert not result["ok"] and result["error"]["code"] == FILE_NOT_FOUND


def test_write_outside_workspace(workspace, runs_dir):
    ctx = make_ctx(make_cfg(workspace, runs_dir))
    result = execute(WRITE_FILE, {"path": "../escape.txt", "content": "x"}, ctx)
    assert not result["ok"] and result["error"]["code"] == PATH_OUTSIDE_WORKSPACE
    assert not (workspace.parent / "escape.txt").exists()


def test_write_into_nested_escape(workspace, runs_dir):
    ctx = make_ctx(make_cfg(workspace, runs_dir))
    result = execute(WRITE_FILE, {"path": "sub/../../escape.txt", "content": "x"}, ctx)
    assert not result["ok"] and result["error"]["code"] == PATH_OUTSIDE_WORKSPACE


def test_write_protected_test_file(workspace, runs_dir):
    ctx = make_ctx(make_cfg(workspace, runs_dir))
    result = execute(WRITE_FILE, {"path": "test_order.py", "content": "hacked"}, ctx)
    assert not result["ok"] and result["error"]["code"] == DENIED_BY_POLICY
    assert "hacked" not in (workspace / "test_order.py").read_text(encoding="utf-8")


def test_list_files_skips_junk(workspace, runs_dir):
    (workspace / "sub").mkdir()
    (workspace / "sub" / "a.py").write_text("", encoding="utf-8")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "m.pyc").write_text("", encoding="utf-8")
    result = execute(LIST_FILES, {}, make_ctx(make_cfg(workspace, runs_dir)))
    assert result["ok"]
    assert result["data"]["files"] == ["order.py", "sub/a.py", "test_order.py"]


# ---------------------------------------------------------- схема аргументов

def test_validate_schema_errors():
    assert "content" in WRITE_FILE.validate({"path": "a"})
    assert "тип string" in WRITE_FILE.validate({"path": 1, "content": "x"})
    assert "неизвестный аргумент" in WRITE_FILE.validate({"path": "a", "content": "x", "extra": 1})
    assert WRITE_FILE.validate({"path": "a", "content": "x"}) is None
    assert LIST_FILES.validate({}) is None  # path необязателен


def test_non_dict_arguments():
    assert "JSON-объектом" in WRITE_FILE.validate("not-a-dict")


# ------------------------------------------------------------------- bash

def test_bash_echo(workspace, runs_dir):
    ctx = make_ctx(make_cfg(workspace, runs_dir))
    result = execute(BASH, {"command": "echo hello"}, ctx)
    assert result["ok"]
    assert result["data"]["stdout"].strip() == "hello"
    assert result["data"]["exit_code"] == 0


def test_bash_nonzero_exit_is_observation(workspace, runs_dir):
    ctx = make_ctx(make_cfg(workspace, runs_dir))
    result = execute(BASH, {"command": "echo boom >&2; exit 3"}, ctx)
    assert result["ok"]  # команда отработала — это наблюдение, не ошибка runtime
    assert result["data"]["exit_code"] == 3
    assert "boom" in result["data"]["stderr"]


def test_bash_cwd(workspace, runs_dir):
    ctx = make_ctx(make_cfg(workspace, runs_dir))
    result = execute(BASH, {"command": "ls", "cwd": "."}, ctx)
    assert result["ok"] and "order.py" in result["data"]["stdout"]


def test_bash_timeout_kills(workspace, runs_dir):
    ctx = make_ctx(make_cfg(workspace, runs_dir), bash_timeout=1.0)
    result = execute(BASH, {"command": "sleep 30"}, ctx)
    assert not result["ok"]
    assert result["error"]["code"] == "TIMEOUT"
    assert result["error"]["recoverable"] is True


def test_bash_env_is_clean(monkeypatch, workspace, runs_dir):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-123")
    ctx = make_ctx(make_cfg(workspace, runs_dir))
    result = execute(BASH, {"command": 'echo "k=$OPENAI_API_KEY"'}, ctx)
    assert result["ok"]
    assert result["data"]["stdout"].strip() == "k="  # секрет не наследуется


def test_bash_output_truncated(workspace, runs_dir):
    ctx = make_ctx(make_cfg(workspace, runs_dir), cap=20)
    result = execute(BASH, {"command": "printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'"}, ctx)
    assert result["ok"]
    assert result["data"]["truncated"] is True
    assert len(result["data"]["stdout"]) < 60
    assert "truncated" in result["data"]["stdout"]
