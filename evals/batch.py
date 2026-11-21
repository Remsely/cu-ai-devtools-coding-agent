"""Матрица исходных наблюдений или заранее заданных 12 сравнительных попыток."""

from __future__ import annotations

import argparse
from argparse import Namespace
import hashlib
import json
from pathlib import Path
import sys

from evals.runner import ROOT, run_trial, tree_hashes, write_json


def schedule(phase: str) -> list[dict]:
    if phase == "discovery":
        return [{"label": f"d{i:02d}-{task}", "task": task, "variant": "baseline"}
                for i, task in enumerate(("discount", "shipping", "discount"), 1)]
    planned = []
    for repetition in range(1, 4):
        for task_index, task in enumerate(("shipping", "quantity")):
            variants = ("baseline", "changed") if (repetition + task_index) % 2 else ("changed", "baseline")
            for variant in variants:
                planned.append({"label": f"r{repetition}-{task}-{variant}",
                                "task": task, "variant": variant})
    return planned


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Запуск 3 исходных или 12 сравнительных реальных сессий.")
    parser.add_argument("phase", choices=("discovery", "compare"))
    parser.add_argument("--output", required=True, help="Новый каталог партии; перезапись запрещена.")
    parser.add_argument("--instruction-file", help="Для compare: файл единственного изменения инструкции.")
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout должен быть положительным")
    if args.phase == "compare" and not args.instruction_file:
        parser.error("compare требует --instruction-file с зафиксированным изменением")
    if args.phase == "discovery" and args.instruction_file:
        parser.error("discovery использует исходный агент без --instruction-file")
    output = Path(args.output).resolve()
    if output.exists():
        parser.error(f"Каталог уже существует: {output}. Выберите новый путь.")
    instruction = Path(args.instruction_file).read_text(encoding="utf-8") if args.instruction_file else None
    if args.phase == "compare" and not instruction.strip():
        parser.error("Файл изменения инструкции пуст")
    planned = schedule(args.phase)
    output.mkdir(parents=True)
    frozen_hashes = tree_hashes(ROOT / "agent")
    manifest = {"phase": args.phase, "schedule": planned, "image": args.image,
                "timeout_s": args.timeout, "instruction": instruction,
                "harness_hashes": frozen_hashes,
                "batch_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "status": "running", "completed_trials": 0}
    write_json(output / "batch.json", manifest)
    results = []
    try:
        for index, item in enumerate(planned, 1):
            if tree_hashes(ROOT / "agent") != frozen_hashes:
                raise RuntimeError("Код агента изменился между попытками; партия остановлена.")
            print(f"[{index}/{len(planned)}] {item['label']}", flush=True)
            trial_args = Namespace(task=item["task"], variant=item["variant"],
                                   output=str(output / item["label"]), image=args.image,
                                   timeout=args.timeout,
                                   instruction_file=args.instruction_file if item["variant"] == "changed" else None)
            # Нельзя незаметно изменить контекст между попытками B.
            if args.instruction_file and Path(args.instruction_file).read_text(encoding="utf-8") != instruction:
                raise RuntimeError("Инструкция изменилась между попытками; партия остановлена.")
            result = run_trial(trial_args)
            results.append(result)
            write_json(output / "results.json", results)
            manifest["completed_trials"] = len(results)
            write_json(output / "batch.json", manifest)
            print(f"  проверка={result['grade']['status']}, вызовов={result['tool_calls']}, "
                  f"время={result['full_time_s']} с", flush=True)
            if result["externally_interrupted"]:
                raise RuntimeError("Попытка прервана пользователем; продолжение требует нового запуска с сохранением этой записи.")
    except Exception as err:
        manifest["status"] = "interrupted"
        manifest["reason"] = f"{type(err).__name__}: {err}"
        write_json(output / "batch.json", manifest)
        print(f"Партия остановлена: {manifest['reason']}. Все полученные записи сохранены.", file=sys.stderr)
        return 2
    manifest["status"] = "completed"
    write_json(output / "batch.json", manifest)
    print(f"Сохранено попыток: {len(results)}; успешных: {sum(r['success'] for r in results)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
