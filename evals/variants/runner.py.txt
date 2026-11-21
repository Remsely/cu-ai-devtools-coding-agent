"""Запуск реальной попытки: модель на хосте, исполняемый код в Docker.

Пример: python -m evals.runner --task discount --variant baseline \
    --output evals/sessions/discovery/d01
Контейнер не получает сеть, секреты, домашний каталог или Docker socket.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from dataclasses import replace
from datetime import datetime, timezone

from agent import cli as agent_cli, tasks
from agent.cli import start_run
from agent.config import Config, Limits, load_env_file
from agent.llm import LLMAPIError, OpenAICompatModel
from agent.loop import Orchestrator
from agent.tools import make_tools
from agent.tools.base import TIMEOUT, ToolError
from agent.tools.fs import safe_path

ROOT = Path(__file__).resolve().parent.parent
TASKS = Path(__file__).resolve().parent / "tasks"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()
            and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")}


class DockerExecutor:
    """Один изолированный контейнер на команду; отказ закрывает выполнение."""

    def __init__(self, workspace: Path, image: str, deadline: float):
        self.workspace = workspace
        self.image = image
        self.deadline = deadline
        self.docker = shutil.which("docker")
        if not self.docker:
            raise RuntimeError("Docker CLI не найден. Установите и запустите Docker Desktop.")
        info = subprocess.run([self.docker, "info", "--format", "{{.OSType}}"],
                              capture_output=True, text=True, timeout=30)
        if info.returncode or info.stdout.strip() != "linux":
            raise RuntimeError("Нужен работающий Docker с Linux-контейнерами: "
                               + info.stderr.strip()[-500:])
        inspected = subprocess.run([self.docker, "image", "inspect", image,
                                    "--format", "{{.Id}}"],
                                   capture_output=True, text=True, timeout=30)
        if inspected.returncode:
            raise RuntimeError(f"Образ {image!r} не найден. Сначала выполните docker pull {image}.")
        self.image_id = inspected.stdout.strip()

    def execute(self, command: str, cwd: str, timeout: float, cap: int,
                *, grader: bool = False, task_id: str = "") -> dict:
        name = "agentlens-" + uuid.uuid4().hex
        mount = f"type=bind,source={self.workspace},target=/workspace"
        if grader:
            mount += ",readonly"
        args = [self.docker, "run", "--rm", "--name", name, "--network", "none",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "64", "--memory", "512m", "--cpus", "1",
                "--user", "65534:65534", "--tmpfs", "/tmp:rw,nosuid,noexec,size=64m",
                "--env", "PYTHONDONTWRITEBYTECODE=1", "--mount", mount,
                "--workdir", cwd]
        if grader:
            args += ["--mount", f"type=bind,source={ROOT / 'evals' / 'grader.py'},target=/grader.py,readonly",
                     "--entrypoint", "python", self.image_id, "-B", "/grader.py", task_id, "/workspace"]
        else:
            args += ["--mount", f"type=bind,source={self.workspace / 'test_order.py'},target=/workspace/test_order.py,readonly",
                     "--entrypoint", "/bin/bash", self.image_id, "-c", command]
        start = time.monotonic()
        timed_out = False
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=max(0.1, timeout))
            stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as err:
            timed_out = True
            stdout, stderr, exit_code = err.stdout or "", err.stderr or "", None
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
        finally:
            # Остановка docker CLI сама по себе не останавливает контейнер.
            subprocess.run([self.docker, "rm", "--force", name], capture_output=True, timeout=30)
        duration = round(time.monotonic() - start, 3)
        output = stdout + stderr
        truncated = len(output.encode("utf-8")) > cap
        if truncated:
            stdout = stdout.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
            remaining = max(0, cap - len(stdout.encode("utf-8")))
            stderr = stderr.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
        return {"command": command, "cwd": cwd.removeprefix("/workspace").lstrip("/") or ".",
                "exit_code": exit_code, "timed_out": timed_out, "duration_s": duration,
                "stdout": stdout, "stderr": stderr, "truncated": truncated}

    def bash(self, arguments: dict, ctx) -> dict:
        if not isinstance(arguments.get("command"), str) or not arguments["command"].strip():
            raise ToolError("INVALID_ARGUMENTS", "Команда должна быть непустой строкой.")
        target = safe_path(ctx, arguments.get("cwd", "."))
        if not target.is_dir():
            raise ToolError("FILE_NOT_FOUND", "Рабочая директория не существует.")
        relative = target.relative_to(ctx.workspace.resolve()).as_posix()
        cwd = "/workspace" if relative == "." else "/workspace/" + relative
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ToolError(TIMEOUT, "Исчерпан общий лимит времени попытки.")
        payload = self.execute(arguments["command"], cwd,
                               min(ctx.bash_timeout, max(0.1, remaining)), ctx.max_output_bytes)
        if payload["timed_out"]:
            raise ToolError(TIMEOUT, "Команда остановлена по таймауту контейнера.", data=payload)
        return payload


class DeadlineModel:
    def __init__(self, model, deadline):
        self.model, self.deadline = model, deadline

    def chat(self, messages, tools):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise LLMAPIError("Исчерпан общий лимит времени попытки.")
        self.model.timeout = min(300.0, remaining)
        return self.model.chat(messages, tools)


def run_trial(args) -> dict:
    task_dir = TASKS / args.task
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError(f"Каталог результата уже существует: {output}. Выберите новый путь.")
    load_env_file()
    model = OpenAICompatModel.from_env()
    workspace = output / "workspace"
    start = time.monotonic()
    deadline = start + args.timeout
    executor = DockerExecutor(workspace, args.image, deadline)
    output.mkdir(parents=True)
    workspace.mkdir()
    for name in ("order.py", "test_order.py"):
        shutil.copyfile(task_dir / name, workspace / name)
    before = tree_hashes(workspace)
    write_json(output / "task.json", task)
    started_at = datetime.now(timezone.utc).isoformat()
    # Название trace/ сохраняет материалы сдачи: runs/ игнорируется .gitignore.
    cfg = Config(workspace=workspace, runs_dir=output / "trace", limits=Limits(),
                 bash_timeout=60, bash_path="docker-isolated", protected_paths=("test_order.py",))
    instruction = Path(args.instruction_file).read_text(encoding="utf-8") if args.instruction_file else ""
    original_messages = agent_cli.initial_messages

    def initial_messages(task_text, plan):
        messages = original_messages(task_text, plan)
        if instruction:
            messages[0]["content"] += "\n\n" + instruction
        return messages

    # Контекст сохраняется исходным run_started; журнал остаётся append-only.
    try:
        agent_cli.initial_messages = initial_messages
        state, journal = start_run(cfg, task["task"], plan_mode=True)
    finally:
        agent_cli.initial_messages = original_messages
    with zipfile.ZipFile(output / "harness.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "agent").rglob("*.py")):
            archive.write(path, path.relative_to(ROOT).as_posix())
    metadata = {"task_id": args.task, "variant": args.variant, "run_id": state.run_id,
                "started_at": started_at, "model": model.model, "base_url": model.base_url,
                "model_parameters": model.extra_payload, "limits": cfg.limits.as_dict(),
                "wall_timeout_s": args.timeout, "image": args.image, "image_id": executor.image_id,
                "isolation": "docker-network-none-readonly-root-protected-tests",
                "instruction": instruction, "initial_hashes": before,
                "harness_hashes": tree_hashes(ROOT / "agent"),
                "grader_sha256": hashlib.sha256((ROOT / "evals" / "grader.py").read_bytes()).hexdigest(),
                "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "task_sha256": hashlib.sha256((task_dir / "task.json").read_bytes()).hexdigest()}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    metadata["git_commit"] = commit.stdout.strip() if commit.returncode == 0 else None
    metadata["initial_messages"] = state.messages
    write_json(output / "manifest.json", metadata)
    registry = make_tools()
    registry["Bash"] = replace(registry["Bash"], run_fn=executor.bash)
    old_verification_executor = tasks.run_bash
    failure = None
    externally_interrupted = False
    try:
        tasks.run_bash = executor.bash
        with (output / "agent.log").open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log):
            orch = Orchestrator(state, journal, DeadlineModel(model, deadline), cfg, registry=registry)
            status = orch.run()
            if status == "awaiting_approval":
                orch.approve_plan()
                status = orch.run()
    except KeyboardInterrupt:
        externally_interrupted = True
        failure = "KeyboardInterrupt: попытка прервана пользователем"
        status = "interrupted"
    except Exception as err:
        failure = f"{type(err).__name__}: {err}"
        status = "error"
    finally:
        tasks.run_bash = old_verification_executor
        journal.close()
    agent_elapsed = time.monotonic() - start
    try:
        if externally_interrupted:
            grader_payload = {"stdout": "", "stderr": "Проверка пропущена после внешнего прерывания.",
                              "exit_code": None, "timed_out": False}
        else:
            grader_payload = executor.execute("independent_grader", "/workspace", 60, 64_000,
                                              grader=True, task_id=args.task)
    except (OSError, subprocess.SubprocessError) as err:
        grader_payload = {"stdout": "", "stderr": f"{type(err).__name__}: {err}",
                          "exit_code": None, "timed_out": False}
    write_json(output / "grader-process.json", grader_payload)
    try:
        grade = json.loads(grader_payload["stdout"])
    except ValueError:
        grade = {"status": "error", "reason": "Независимая проверка не вернула корректный JSON."}
    after = tree_hashes(workspace)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    violations = [p for p in changed if p not in task["allowed_changes"]]
    result = {"task_id": args.task, "variant": args.variant, "run_id": state.run_id,
              "agent_status": status, "error": failure, "grade": grade,
              "success": grade.get("status") == "pass" and not violations,
              "success_definition": "Независимая проверка артефакта пройдена; ограничений не нарушено.",
              "agent_completed": status == "completed", "externally_interrupted": externally_interrupted,
              "agent_elapsed_s": round(agent_elapsed, 3),
              "full_time_s": round(time.monotonic() - start, 3),
              "model_requests": state.counters.model_requests, "tool_calls": state.counters.tool_calls,
              "tokens": None, "tokens_unknown_reason": "Текущий harness не сохраняет usage модели.",
              "changed_paths": changed, "restriction_violations": violations}
    write_json(output / "result.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Реальная попытка агента с независимой проверкой в Docker.")
    parser.add_argument("--task", choices=("discount", "shipping", "quantity"), required=True)
    parser.add_argument("--variant", required=True, help="Метка варианта, например baseline или changed.")
    parser.add_argument("--output", required=True, help="Новый каталог результата; существующий не перезаписывается.")
    parser.add_argument("--instruction-file", help="Точная добавка к системной инструкции для этого варианта.")
    parser.add_argument("--image", default="python:3.12-slim", help="Заранее загруженный Docker-образ с Python и Bash.")
    parser.add_argument("--timeout", type=float, default=600, help="Общий лимит времени работы агента, секунд.")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout должен быть положительным")
    try:
        result = run_trial(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as err:
        print(f"Не удалось выполнить эксперимент: {err}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
