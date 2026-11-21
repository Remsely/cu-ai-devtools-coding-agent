"""Калибровка независимых проверок и детерминированной границы эксперимента."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evals.batch import schedule
from evals.runner import DockerExecutor, ROOT


CORRECT = {
    "discount": """def calculate_total(prices, discount_percent):
    if not 0 <= discount_percent <= 100:
        raise ValueError('discount')
    return sum(prices) * (1 - discount_percent / 100)
""",
    "shipping": """def calculate_shipping(subtotal, free_from=100, fee=10):
    if min(subtotal, free_from, fee) < 0:
        raise ValueError('negative')
    return 0 if subtotal >= free_from else fee
""",
    "quantity": """def calculate_line_total(unit_price, quantity):
    if unit_price < 0 or type(quantity) is not int or quantity < 0:
        raise ValueError('invalid')
    return unit_price * quantity
""",
}


def grade(task, workspace):
    proc = subprocess.run([sys.executable, "-B", str(ROOT / "evals" / "grader.py"),
                           task, str(workspace)], capture_output=True, text=True,
                          encoding="utf-8", timeout=10)
    return proc.returncode, json.loads(proc.stdout)


@pytest.mark.parametrize("task", ("discount", "shipping", "quantity"))
def test_grader_rejects_original_bug_and_accepts_correct_result(task, tmp_path):
    original = (ROOT / "evals" / "tasks" / task / "order.py").read_text(encoding="utf-8")
    (tmp_path / "order.py").write_text(original, encoding="utf-8")
    code, result = grade(task, tmp_path)
    assert code == 1 and result["status"] == "fail"
    (tmp_path / "order.py").write_text(CORRECT[task], encoding="utf-8")
    code, result = grade(task, tmp_path)
    assert code == 0 and result["status"] == "pass"
    assert all(check["passed"] for check in result["checks"])


def test_grader_rejects_happy_path_only_mutant(tmp_path):
    (tmp_path / "order.py").write_text(
        "def calculate_total(prices, discount_percent):\n"
        "    return sum(prices) * (1 - discount_percent / 100)\n", encoding="utf-8")
    _, result = grade("discount", tmp_path)
    failed = [c["name"] for c in result["checks"] if not c["passed"]]
    assert failed == ["reject_discount:-0.01", "reject_discount:100.01"]


def test_grader_import_failure_is_failed_outcome(tmp_path):
    (tmp_path / "order.py").write_text("invalid Python syntax !", encoding="utf-8")
    _, result = grade("discount", tmp_path)
    assert result["status"] == "fail"
    assert result["checks"][0]["name"] == "import"


def test_comparison_has_fresh_balanced_schedule_and_heldout_task():
    discovery, comparison = schedule("discovery"), schedule("compare")
    assert len(discovery) == 3 and {r["task"] for r in discovery} == {"discount", "shipping"}
    assert len(comparison) == 12 and len({r["label"] for r in comparison}) == 12
    assert "quantity" not in {r["task"] for r in discovery}
    for task in ("shipping", "quantity"):
        for variant in ("baseline", "changed"):
            assert sum(r["task"] == task and r["variant"] == variant for r in comparison) == 3


def fake_executor(tmp_path, monkeypatch):
    executor = object.__new__(DockerExecutor)
    executor.workspace = tmp_path
    executor.image_id = "sha256:test-image"
    executor.docker = "docker"
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return executor, commands


def test_tool_container_has_no_network_secrets_or_writable_tests(tmp_path, monkeypatch):
    executor, commands = fake_executor(tmp_path, monkeypatch)
    executor.execute("python -m unittest -v", "/workspace", 5, 1000)
    command = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
    assert len(mounts) == 2
    assert mounts[0] == f"type=bind,source={tmp_path},target=/workspace"
    assert mounts[1] == f"type=bind,source={tmp_path / 'test_order.py'},target=/workspace/test_order.py,readonly"
    assert command[-1] == "python -m unittest -v"
    assert not any("OPENAI" in part or "docker.sock" in part or ".env" in part for part in command)


def test_independent_grader_mounts_only_readonly_inputs(tmp_path, monkeypatch):
    executor, commands = fake_executor(tmp_path, monkeypatch)
    executor.execute("independent_grader", "/workspace", 5, 1000, grader=True, task_id="discount")
    command = commands[0]
    mounts = [command[i + 1] for i, value in enumerate(command) if value == "--mount"]
    assert len(mounts) == 2 and all(m.endswith(",readonly") for m in mounts)
    assert command[-4:] == ["-B", "/grader.py", "discount", "/workspace"]


def test_timeout_stops_container_and_preserves_unknown_exit_code(tmp_path, monkeypatch):
    executor, commands = fake_executor(tmp_path, monkeypatch)

    def timeout_run(command, **kwargs):
        commands.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 1, output=b"partial")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    result = executor.execute("sleep 10", "/workspace", 1, 1000)
    assert result["timed_out"] and result["exit_code"] is None
    assert result["stdout"] == "partial"
    assert commands[-1][1:3] == ["rm", "--force"]
