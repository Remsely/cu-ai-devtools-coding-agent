"""Проверки отчётов на искусственных артефактах; реальные опыты не изменяются."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evals import report as reports


class SavedTrials:
    """Создаёт явно синтетические сохранённые попытки, без модели и Docker."""

    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.metrics: dict[str, dict] = {}
        self.directories: list[Path] = []
        monkeypatch.setattr(reports, "analyze_path", self.analyze)

    def analyze(self, path: Path) -> dict:
        return copy.deepcopy(self.metrics[str(path)])

    def add(
        self,
        task: str = "shipping",
        variant: str = "baseline",
        attempt: int = 1,
        *,
        grade: str = "pass",
        status: str = "completed",
        repeated: int | None = 0,
        seconds: float | None = 10.0,
    ) -> Path:
        run_id = f"synthetic-{task}-{variant}-{attempt}"
        directory = self.root / f"{task}-{variant}-{attempt}"
        directory.mkdir(parents=True)
        instruction = "Use reproducible verification commands." if variant == "changed" else ""
        messages = [
            {"role": "system", "content": "Original instruction" + ("\n\n" + instruction if instruction else "")},
            {"role": "user", "content": f"Synthetic task {task}"},
        ]
        manifest = {
            "task_id": task, "variant": variant, "run_id": run_id,
            "model": "synthetic-model", "base_url": "http://example.invalid/v1",
            "model_parameters": {"temperature": 0.2}, "limits": {"model_requests": 40},
            "wall_timeout_s": 600, "image_id": "sha256:synthetic-image",
            "isolation": "synthetic-test-isolation", "harness_hashes": {"cli.py": "synthetic-hash"},
            "grader_sha256": "synthetic-grader", "runner_sha256": "synthetic-runner",
            "initial_hashes": {"order.py": f"synthetic-original-{task}", "test_order.py": "test-hash"},
            "task_sha256": f"synthetic-task-{task}", "instruction": instruction,
            "initial_messages": messages,
        }
        result = {
            "task_id": task, "variant": variant, "run_id": run_id,
            "grade": {"status": grade}, "agent_status": status,
            "restriction_violations": [], "full_time_s": seconds,
            "tool_calls": 0, "model_requests": 0, "tokens": None,
            "tokens_unknown_reason": "Synthetic fixture has no token measurement.",
        }
        self.write(directory / "manifest.json", manifest)
        self.write(directory / "result.json", result)
        journal = directory / "trace" / run_id / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        events = [
            {"seq": 1, "event": "run_started", "run_id": run_id,
             "initial_messages": messages, "task": f"Synthetic task {task}", "workspace": "/synthetic"},
            {"seq": 2, "event": "run_finished", "status": status},
        ]
        journal.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        self.metrics[str(journal)] = {
            "complete": True,
            "summary": {"repeated_failures": repeated, "repeat_episodes": int(bool(repeated)),
                        "failed_executions": repeated, "unknown_executions": 0},
            "sessions": [{"session_id": run_id}], "errors": [],
        }
        self.directories.append(directory)
        return directory

    @staticmethod
    def write(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def update(path: Path, **changes) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        value.update(changes)
        SavedTrials.write(path, value)

    def balanced(self) -> None:
        for task in ("shipping", "quantity"):
            for variant in ("baseline", "changed"):
                for attempt in (1, 2, 3):
                    self.add(task, variant, attempt)


@pytest.fixture
def trials(tmp_path, monkeypatch):
    return SavedTrials(tmp_path / "synthetic trials", monkeypatch)


def issue_codes(report: dict) -> set[str]:
    return {item["code"] for item in report["issues"]}


def test_balanced_twelve_saved_attempts_have_comparable_conditions(trials):
    trials.balanced()
    report = reports.build_report(trials.root, comparison=True)
    assert len(report["runs"]) == 12
    assert report["issues"] == []
    assert report["conditions"]["comparable"] is True
    all_groups = [item for item in report["aggregates"] if item["task_id"] == "all"]
    assert {item["variant"] for item in all_groups} == {"baseline", "changed"}
    assert all(item["runs"] == 6 and item["artifact_successes"] == 6 for item in all_groups)


@pytest.mark.parametrize("change,expected_issue", [
    ({"model": "another-model"}, "condition_differs"),
    ({"model_parameters": {"temperature": 0.9}}, "condition_differs"),
    ({"initial_hashes": {"order.py": "already-fixed"}}, "initial_condition_differs"),
    ({"instruction": "Different instruction inside the same variant"}, "instruction_differs"),
])
def test_uncontrolled_experimental_change_is_not_comparable(trials, change, expected_issue):
    trials.balanced()
    # Меняем только один сохранённый manifest, а не исходный fixture всех попыток.
    trials.update(trials.directories[-1] / "manifest.json", **change)
    report = reports.build_report(trials.root, comparison=True)
    assert report["conditions"]["comparable"] is False
    assert expected_issue in issue_codes(report)


def test_extra_context_change_beyond_declared_instruction_is_detected(trials):
    trials.balanced()
    path = trials.directories[-1] / "manifest.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["initial_messages"][1]["content"] = "Additional hidden task hint"
    trials.write(path, meta)
    report = reports.build_report(trials.root, comparison=True)
    assert "extra_context_difference" in issue_codes(report)
    assert report["conditions"]["comparable"] is False


def test_declared_instruction_must_actually_appear_in_context(trials):
    trials.balanced()
    path = trials.directories[-1] / "manifest.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["initial_messages"][0]["content"] = "Original instruction"
    trials.write(path, meta)
    report = reports.build_report(trials.root, comparison=True)
    assert "instruction_not_in_context" in issue_codes(report)
    assert report["conditions"]["comparable"] is False


def test_artifact_success_and_agent_completion_are_distinct_observations(trials):
    trials.add(status="limit_reached", grade="pass")
    report = reports.build_report(trials.root)
    row = report["runs"][0]
    assert row["artifact_success"] is True
    assert row["agent_completed"] is False
    assert row["agent_status"] == "limit_reached"
    assert row["tokens"] is None and row["tokens_unknown_reason"]


def test_failed_error_and_unknown_outcomes_do_not_turn_into_success_or_zero(trials):
    trials.add(attempt=1, grade="pass", repeated=4, seconds=12)
    trials.add(attempt=2, grade="fail", repeated=0, seconds=8)
    unknown = trials.add(attempt=3, grade="error", status="error", repeated=None, seconds=None)
    trials.update(unknown / "result.json", tool_calls=None, model_requests=None, success=True)
    report = reports.build_report(trials.root)
    rows = {row["independent_grade"]: row for row in report["runs"]}
    assert rows["fail"]["artifact_success"] is False
    assert rows["error"]["artifact_success"] is None
    assert rows["error"]["repeated_failures"] is None
    assert rows["error"]["full_time_s"] is None
    assert rows["error"]["tool_calls"] is None
    aggregate = next(item for item in report["aggregates"] if item["task_id"] == "all")
    assert (aggregate["artifact_successes"], aggregate["artifact_failures"], aggregate["unknown_artifacts"]) == (1, 1, 1)
    assert aggregate["repeated_failures"] == {"median": 2.0, "sum": 4, "measured_runs": 2, "unknown_runs": 1}
    assert aggregate["full_time_s"]["median"] == 10
    assert aggregate["tool_calls"]["unknown_runs"] == 1


def test_restriction_violation_prevents_artifact_success_even_if_grader_passes(trials):
    directory = trials.add()
    trials.update(directory / "result.json", restriction_violations=["test_order.py"], success=True)
    report = reports.build_report(trials.root)
    row = report["runs"][0]
    assert row["independent_grade"] == "pass"
    assert row["artifact_success"] is False
    assert row["restriction_violations"] == ["test_order.py"]


def test_missing_result_preserves_the_started_attempt_and_unknown_measurements(trials):
    directory = trials.add()
    (directory / "result.json").unlink()
    report = reports.build_report(trials.root)
    assert len(report["runs"]) == 1
    row = report["runs"][0]
    assert row["artifact_success"] is None
    assert row["agent_completed"] is None
    assert row["full_time_s"] is None
    assert row["tool_calls"] is None
    assert "unreadable_record" in issue_codes(report)
    assert report["conditions"]["comparable"] is False


def test_missing_journal_is_an_unknown_metric_not_zero_repeats(trials):
    directory = trials.add()
    next(directory.glob("trace/*/journal.jsonl")).unlink()
    report = reports.build_report(trials.root)
    assert report["runs"][0]["repeated_failures"] is None
    assert "journal_count" in issue_codes(report)
    aggregate = next(item for item in report["aggregates"] if item["task_id"] == "all")
    assert aggregate["repeated_failures"]["median"] is None
    assert aggregate["repeated_failures"]["sum"] is None
    assert aggregate["repeated_failures"]["unknown_runs"] == 1


def test_incomplete_analyzer_evidence_keeps_warning_and_observed_counts(trials):
    directory = trials.add(repeated=2)
    journal = next(directory.glob("trace/*/journal.jsonl"))
    trials.metrics[str(journal)]["complete"] = False
    report = reports.build_report(trials.root)
    assert report["runs"][0]["repeated_failures"] == 2
    assert report["runs"][0]["analysis_complete"] is False
    assert "incomplete_analysis" in issue_codes(report)


def test_result_identity_must_match_the_manifest(trials):
    directory = trials.add()
    trials.update(directory / "result.json", variant="unexpected-variant")
    report = reports.build_report(trials.root)
    assert "record_mismatch" in issue_codes(report)
    assert report["conditions"]["comparable"] is False


def test_markdown_has_working_relative_journal_links_and_unknown_values(trials, tmp_path):
    trials.add(grade="error", seconds=None)
    report = reports.build_report(trials.root)
    output = tmp_path / "reports with spaces"
    document = reports.markdown(report, output)
    expected = "../synthetic trials/" + report["runs"][0]["journal"]
    assert f"](<{expected}>)" in document
    assert "неизвестно" in document
    assert "Завершение агента показано отдельно" in document


def test_report_cli_saves_machine_readable_and_markdown_artifacts(trials, tmp_path, capsys):
    trials.balanced()
    output = tmp_path / "report"
    code = reports.main(["--input", str(trials.root), "--output", str(output), "--comparison",
                         "--note", "Synthetic test data only."])
    assert code == 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["conditions"]["comparable"] is True
    assert report["provenance_notes"] == ["Synthetic test data only."]
    assert "# Сравнение A/B" in (output / "report.md").read_text(encoding="utf-8")
    assert capsys.readouterr().err == ""


def test_report_cli_preserves_partial_report_with_nonzero_status(trials, tmp_path):
    directory = trials.add()
    (directory / "result.json").unlink()
    output = tmp_path / "partial"
    assert reports.main(["--input", str(trials.root), "--output", str(output)]) == 1
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert len(report["runs"]) == 1 and report["runs"][0]["artifact_success"] is None
    assert (output / "report.md").exists()


def test_report_cli_missing_directory_has_clear_failure(tmp_path, capsys):
    output = tmp_path / "never-created"
    assert reports.main(["--input", str(tmp_path / "missing"), "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert "Не удалось построить отчёт" in captured.err
    assert not output.exists()


def test_swapped_initial_roles_are_not_a_valid_comparison_context(trials):
    trials.balanced()
    path = trials.directories[-1] / "manifest.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["initial_messages"][0]["role"] = "user"
    meta["initial_messages"][1]["role"] = "system"
    trials.write(path, meta)
    report = reports.build_report(trials.root, comparison=True)
    assert "invalid_initial_roles" in issue_codes(report)
    assert report["conditions"]["comparable"] is False


def test_actual_journal_context_must_match_the_saved_manifest(trials):
    directory = trials.add()
    path = next(directory.glob("trace/*/journal.jsonl"))
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    events[0]["initial_messages"][1]["content"] = "Different task actually sent to the model"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    report = reports.build_report(trials.root)
    assert "journal_context_mismatch" in issue_codes(report)
    assert report["conditions"]["comparable"] is False


def test_saved_counters_must_match_the_journal_events(trials):
    directory = trials.add()
    trials.update(directory / "result.json", tool_calls=99)
    report = reports.build_report(trials.root)
    assert "counter_mismatch" in issue_codes(report)
    assert report["conditions"]["comparable"] is False


def test_invalid_instruction_type_is_noncomparable_instead_of_crashing(trials):
    trials.balanced()
    trials.update(trials.directories[-1] / "manifest.json", instruction=["invalid", "type"])
    report = reports.build_report(trials.root, comparison=True)
    assert "invalid_instruction" in issue_codes(report)
    assert report["conditions"]["comparable"] is False
