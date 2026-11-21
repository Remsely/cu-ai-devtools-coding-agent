"""Проверки анализатора на явно искусственных журналах, без модели и сети."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


class SessionLog:
    """Короткий журнал в формате runtime с устойчивыми адресами событий."""

    def __init__(self, session_id: str = "synthetic-session") -> None:
        self.events: list[dict] = []
        self.add(
            "run_started",
            run_id=session_id,
            workspace="/sample",
            task="test",
            mode="direct",
            limits={"model_requests": 40, "tool_calls": 80, "side_effects": 30, "output_bytes": 262144},
            initial_messages=[{"role": "user", "content": "test"}],
        )

    def add(self, event: str, **payload) -> dict:
        record = {
            "seq": len(self.events) + 1,
            "ts": "2026-11-21T12:00:00.000+00:00",
            "event": event,
            **payload,
        }
        self.events.append(record)
        return record

    def start(
        self,
        call_id: str,
        command: str = "python -m unittest -v",
        cwd: str = ".",
        *,
        attempt: int = 1,
        tool: str = "Bash",
    ) -> dict:
        args = {"command": command, "cwd": cwd} if tool == "Bash" else {"task_id": "t1"}
        return self.add(
            "tool_call_started",
            id=call_id,
            name=tool,
            args_json=json.dumps(args),
            side_effect=tool == "Bash",
            attempt=attempt,
            task_id="t1",
        )

    def finish(self, call_id: str, result: dict, *, attempt: int = 1) -> dict:
        return self.add(
            "tool_call_finished",
            id=call_id,
            attempt=attempt,
            ok=result.get("ok", False),
            size=len(json.dumps(result)),
            result=result,
        )

    def bash(
        self,
        call_id: str,
        *,
        command: str = "python -m unittest -v",
        cwd: str = ".",
        exit_code: int | None = 1,
        stderr: str = "AssertionError: total is incorrect",
        attempt: int = 1,
        truncated: bool = False,
    ) -> None:
        self.start(call_id, command, cwd, attempt=attempt)
        self.finish(
            call_id,
            {
                "ok": True,
                "data": {
                    "command": command,
                    "cwd": cwd,
                    "exit_code": exit_code,
                    "timed_out": False,
                    "duration_s": 0.1,
                    "stdout": "",
                    "stderr": stderr,
                    "truncated": truncated,
                },
            },
            attempt=attempt,
        )

    def save(self, path: Path, *, finish: bool = True) -> Path:
        if finish:
            self.add("run_finished", status="completed", final_answer="done")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in self.events),
            encoding="utf-8",
        )
        return path

    def verify(
        self,
        call_id: str,
        *,
        command: str = "python -m unittest -v",
        expected: int = 0,
        actual: int | None = 1,
        output: str = "AssertionError: total is incorrect",
        already_completed: bool = False,
    ) -> None:
        self.start(call_id, tool="complete_task")
        verification = {
            "command": command,
            "expect_exit_code": expected,
            "exit_code": actual,
            "ok": actual == expected,
            "timed_out": False,
            "output": output,
        }
        payload = {"task_id": "t1", "verification": verification}
        if already_completed:
            payload["already_completed"] = True
        result = (
            {"ok": True, "data": payload}
            if actual == expected or already_completed
            else {"ok": False, "error": {"code": "VERIFICATION_FAILED", "data": payload}}
        )
        self.finish(call_id, result)


def analyze(path: Path) -> dict:
    from agentlens import analyze_path

    return analyze_path(path)


def cli(*args: str) -> int:
    from agentlens.cli import main

    return main(list(args))


def test_help_explains_required_options_and_format(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli("analyze", "--help")
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "--input" in output and "--json" in output
    assert "JSONL" in output.upper()


def test_invalid_json_has_source_line_and_nonzero_json_cli(tmp_path, capsys):
    path = SessionLog().save(tmp_path / "broken.jsonl", finish=False)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{broken\n")

    assert cli("analyze", "--input", str(path), "--json") != 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["complete"] is False
    assert any(error.get("line") == 2 for error in report["errors"])
    assert all(error["source"] == str(path) for error in report["errors"])
    assert captured.err.strip()


def test_unreadable_file_is_listed_and_valid_sessions_are_retained(tmp_path, monkeypatch):
    readable = SessionLog("readable").save(tmp_path / "good.jsonl")
    blocked = SessionLog("blocked").save(tmp_path / "blocked.jsonl")
    original = Path.read_text

    def deny_one(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("synthetic permission denial")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_one)
    report = analyze(tmp_path)
    assert report["complete"] is False
    assert report["summary"]["files_seen"] == 2
    assert any(session["source"] == str(readable) for session in report["sessions"])
    assert any(error["source"] == str(blocked) for error in report["errors"])


@pytest.mark.parametrize("contents", ["", "[]\n", '{"event":"run_started","seq":true}\n'])
def test_invalid_session_does_not_look_like_zero_findings(tmp_path, contents):
    path = tmp_path / "invalid.jsonl"
    path.write_text(contents, encoding="utf-8")
    report = analyze(path)
    assert report["errors"]
    assert report["complete"] is False


def test_missing_input_has_machine_readable_error(tmp_path, capsys):
    missing = tmp_path / "missing.jsonl"
    assert cli("analyze", "--input", str(missing), "--json") != 0
    report = json.loads(capsys.readouterr().out)
    assert report["errors"]
    assert report["summary"]["sessions_analyzed"] == 0


def test_empty_directory_is_an_input_error(tmp_path):
    report = analyze(tmp_path)
    assert report["errors"]
    assert report["complete"] is False


def test_repeated_failure_has_provenance_and_intervening_actions(tmp_path):
    """Одинаковые ошибки после правки остаются фактом, а не диагнозом её причины."""
    log = SessionLog()
    log.bash("c1")
    log.add("tool_call_started", id="read", name="read_file", args_json='{"path":"order.py"}',
            attempt=1, side_effect=False)
    log.finish("read", {"ok": True, "data": {"path": "order.py", "content": "x = 1"}})
    log.add("change_recorded", call_id="edit", attribution="action",
            diff=[{"path": "order.py", "change": "modified", "before_hash": "old", "after_hash": "new"}],
            after_tree={"order.py": "new"})
    log.bash("c2")
    report = analyze(log.save(tmp_path / "positive.jsonl"))

    assert not report["errors"]
    assert report["summary"]["failed_executions"] == 2
    assert report["summary"]["repeat_episodes"] == 1
    finding = report["sessions"][0]["findings"][0]
    assert finding["repeat_count"] == 1
    assert [item["call_id"] for item in finding["occurrences"]] == ["c1", "c2"]
    assert finding["between"][0]["changes"][0]["path"] == "order.py"
    assert any(action["tool"] == "read_file" for action in finding["between"][0]["actions"])
    for occurrence in finding["occurrences"]:
        for key in ("start", "result"):
            reference = occurrence[key]
            event = log.events[reference["line"] - 1]
            assert event["seq"] == reference["seq"]
            assert event["id"] == occurrence["call_id"]


def test_identical_command_in_different_working_directories_is_not_a_repeat(tmp_path):
    log = SessionLog()
    log.bash("c1", cwd="service-a")
    log.bash("c2", cwd="service-b")
    report = analyze(log.save(tmp_path / "different-cwd.jsonl"))
    assert report["summary"]["failed_executions"] == 2
    assert report["summary"]["repeat_episodes"] == 0


def test_bash_nonzero_exit_is_failure_despite_successful_tool_envelope(tmp_path):
    log = SessionLog()
    log.bash("c1", exit_code=3)
    report = analyze(log.save(tmp_path / "exit-status.jsonl"))
    execution = report["sessions"][0]["executions"][0]
    assert execution["state"] == "failure"
    assert execution["exit_code"] == 3


@pytest.mark.parametrize("expected,actual,state", [(2, 1, "failure"), (1, 1, "success"), (0, 0, "success")])
def test_runtime_verification_uses_expected_exit_code(tmp_path, expected, actual, state):
    log = SessionLog()
    log.verify("v1", expected=expected, actual=actual)
    report = analyze(log.save(tmp_path / "verification.jsonl"))
    execution = report["sessions"][0]["executions"][0]
    assert execution["state"] == state
    assert execution["expected_exit_code"] == expected
    assert execution["exit_code"] == actual


def test_bash_and_runtime_verification_can_form_one_failure_episode(tmp_path):
    log = SessionLog()
    log.bash("bash")
    log.verify("verify")
    report = analyze(log.save(tmp_path / "two-paths.jsonl"))
    assert report["summary"]["repeat_episodes"] == 1
    occurrences = report["sessions"][0]["findings"][0]["occurrences"]
    assert {item["tool"] for item in occurrences} == {"Bash", "complete_task"}


def test_changed_expected_exit_contract_does_not_form_a_repeat(tmp_path):
    log = SessionLog()
    log.verify("v1", expected=2, actual=1)
    log.verify("v2", expected=3, actual=1)
    report = analyze(log.save(tmp_path / "different-expectation.jsonl"))
    assert report["summary"]["repeat_episodes"] == 0


@pytest.mark.parametrize("barrier", ["success", "different_error", "missing_result"])
def test_success_different_error_and_unknown_outcome_break_a_failure_chain(tmp_path, barrier):
    log = SessionLog()
    log.bash("c1")
    if barrier == "success":
        log.bash("middle", exit_code=0, stderr="OK")
    elif barrier == "different_error":
        log.bash("middle", stderr="ModuleNotFoundError: other_module")
    else:
        log.start("middle")
    log.bash("c2")
    report = analyze(log.save(tmp_path / f"{barrier}.jsonl"))
    assert report["summary"]["repeat_episodes"] == 0


def test_missing_result_stays_unknown_and_is_explained(tmp_path, capsys):
    log = SessionLog()
    log.start("pending")
    path = log.save(tmp_path / "unfinished.jsonl", finish=False)
    report = analyze(path)
    execution = report["sessions"][0]["executions"][0]
    assert execution["state"] == "unknown"
    assert execution["exit_code"] is None
    assert execution["result"] is None
    assert report["sessions"][0]["warnings"]
    assert report["summary"]["unknown_executions"] == 1
    assert report["summary"]["failed_executions"] == 0
    assert cli("analyze", "--input", str(path), "--json") == 0
    assert json.loads(capsys.readouterr().out)["sessions"][0]["warnings"]


def test_replay_and_cached_task_completion_are_not_new_command_executions(tmp_path):
    log = SessionLog()
    log.bash("c1")
    log.add("tool_call_replayed", id="c1", result=log.events[-1]["result"])
    log.verify("cached", actual=1, already_completed=True)
    report = analyze(log.save(tmp_path / "replay.jsonl"))
    assert report["summary"]["executions"] == 1
    assert report["summary"]["repeat_episodes"] == 0


def test_explicit_retry_keeps_attempt_provenance_and_incomplete_first_attempt(tmp_path):
    log = SessionLog()
    log.start("retry", attempt=1)
    log.add("decision_recorded", subject="tool_call", call_id="retry", choice="retry")
    log.bash("retry", attempt=2)
    log.bash("later")
    report = analyze(log.save(tmp_path / "retry.jsonl"))
    executions = report["sessions"][0]["executions"]
    assert [(item["call_id"], item["attempt"], item["state"]) for item in executions] == [
        ("retry", 1, "unknown"), ("retry", 2, "failure"), ("later", 1, "failure")
    ]
    assert report["summary"]["repeat_episodes"] == 1
    assert [item["attempt"] for item in report["sessions"][0]["findings"][0]["occurrences"]] == [2, 1]


def test_skipped_uncertain_call_is_not_a_known_command_failure(tmp_path):
    log = SessionLog()
    log.start("skipped")
    log.finish("skipped", {"ok": False, "error": {"code": "UNDEFINED_SKIPPED", "message": "unknown effect"}})
    report = analyze(log.save(tmp_path / "skipped.jsonl"))
    assert report["summary"]["unknown_executions"] == 1
    assert report["summary"]["failed_executions"] == 0


def test_timeout_payload_under_error_data_is_a_known_failure(tmp_path):
    log = SessionLog()
    log.start("timeout")
    log.finish("timeout", {"ok": False, "error": {"code": "TIMEOUT", "data": {
        "command": "python -m unittest -v", "cwd": ".", "exit_code": None,
        "timed_out": True, "stdout": "", "stderr": "", "truncated": False,
    }}})
    report = analyze(log.save(tmp_path / "timeout.jsonl"))
    execution = report["sessions"][0]["executions"][0]
    assert execution["state"] == "failure"
    assert execution["exit_code"] is None
    assert report["summary"]["unknown_executions"] == 0


def test_missing_exit_code_is_not_inferred_to_be_success_or_timeout(tmp_path):
    log = SessionLog()
    log.bash("unknown", exit_code=None)
    report = analyze(log.save(tmp_path / "missing-exit.jsonl"))
    assert report["sessions"][0]["executions"][0]["state"] == "unknown"


def test_truncated_diagnostics_preserve_failure_but_cannot_prove_same_error(tmp_path):
    log = SessionLog()
    log.bash("c1", truncated=True)
    log.bash("c2", truncated=True)
    report = analyze(log.save(tmp_path / "truncated.jsonl"))
    assert report["summary"]["failed_executions"] == 2
    assert report["summary"]["repeat_episodes"] == 0
    assert report["sessions"][0]["warnings"]
    assert all(item["signature"] is None for item in report["sessions"][0]["executions"])


def test_directory_aggregate_keeps_session_addresses_and_ignores_snapshot_json(tmp_path):
    positive = SessionLog("has-repeats")
    positive.bash("c1")
    positive.bash("c2")
    positive.save(tmp_path / "a" / "journal.jsonl")
    negative = SessionLog("no-repeats")
    negative.bash("success", exit_code=0, stderr="OK")
    negative.save(tmp_path / "b" / "journal.jsonl")
    (tmp_path / "a" / "snapshots").mkdir()
    (tmp_path / "a" / "snapshots" / "snap1.json").write_text("not a journal", encoding="utf-8")

    report = analyze(tmp_path)
    assert report["summary"]["files_seen"] == 2
    assert report["summary"]["sessions_analyzed"] == 2
    assert report["summary"]["sessions_with_repeats"] == 1
    assert report["summary"]["executions"] == 3
    assert report["summary"]["repeat_episodes"] == 1
    assert {item["session_id"] for item in report["sessions"]} == {"has-repeats", "no-repeats"}


def test_successful_session_without_repeats_has_friendly_text_and_exit_zero(tmp_path, capsys):
    log = SessionLog()
    log.bash("success", exit_code=0, stderr="OK")
    path = log.save(tmp_path / "clean.jsonl")
    assert cli("analyze", "--input", str(path)) == 0
    output = capsys.readouterr()
    assert "не найдено" in output.out
    assert output.err == ""


def test_json_cli_returns_one_object_with_same_findings_as_api(tmp_path, capsys):
    log = SessionLog()
    log.bash("c1")
    log.bash("c2")
    path = log.save(tmp_path / "one-object.jsonl")
    assert cli("analyze", "--input", str(path), "--json") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == analyze(path)
    assert captured.err == ""


@pytest.mark.parametrize("field,value", [
    ("args_json", "{broken"),
    ("args_json", "[]"),
    ("args_json", '{"command":42}'),
    ("args_json", '{"command":"echo test","cwd":[]}'),
    ("id", {"not": "a string"}),
    ("attempt", "one"),
])
def test_malformed_call_fields_are_reported_without_traceback(tmp_path, capsys, field, value):
    log = SessionLog()
    event = log.start("bad")
    event[field] = value
    path = log.save(tmp_path / "bad-call.jsonl")
    assert cli("analyze", "--input", str(path), "--json") != 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["errors"]
    assert "Traceback" not in captured.err


def test_invalid_result_shape_is_reported_without_traceback(tmp_path, capsys):
    log = SessionLog()
    log.bash("bad")
    log.events[-1]["result"] = ["not", "an", "envelope"]
    path = log.save(tmp_path / "bad-result.jsonl")
    assert cli("analyze", "--input", str(path), "--json") != 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["errors"]
    assert "Traceback" not in captured.err


def test_extra_fields_remain_forward_compatible(tmp_path):
    log = SessionLog()
    log.bash("one", exit_code=0, stderr="OK")
    log.events[-1]["new_optional_field"] = {"nested": [1, 2, 3]}
    report = analyze(log.save(tmp_path / "extended.jsonl"))
    assert report["errors"] == []
    assert report["summary"]["executions"] == 1


def test_nonhashable_task_id_is_reported_without_traceback(tmp_path, capsys):
    log = SessionLog()
    event = log.start("bad-task", tool="complete_task")
    event["args_json"] = '{"task_id":{"invalid":"mapping"}}'
    path = log.save(tmp_path / "bad-task.jsonl")
    assert cli("analyze", "--input", str(path), "--json") != 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["errors"]
    assert "Traceback" not in captured.err


def test_result_before_start_cannot_establish_the_later_call_outcome(tmp_path):
    log = SessionLog()
    log.finish("late", {"ok": True, "data": {
        "command": "python -m unittest -v", "cwd": ".", "exit_code": 1,
        "timed_out": False, "stdout": "", "stderr": "AssertionError", "truncated": False,
    }})
    log.start("late")
    report = analyze(log.save(tmp_path / "out-of-order.jsonl"))
    assert report["summary"]["failed_executions"] == 0
    assert report["summary"]["repeat_episodes"] == 0
    assert all(item["state"] == "unknown" for session in report["sessions"] for item in session["executions"])
    assert report["errors"] or any(session["warnings"] for session in report["sessions"])


def test_sequence_gap_cannot_prove_a_consecutive_failure_chain(tmp_path):
    log = SessionLog()
    log.bash("first")
    absent = log.add("user_note", text="This event will be omitted deliberately.")
    log.bash("second")
    log.events.remove(absent)
    report = analyze(log.save(tmp_path / "gap.jsonl"))
    assert report["summary"]["failed_executions"] == 2
    assert report["summary"]["repeat_episodes"] == 0
    assert report["sessions"][0]["warnings"]


@pytest.mark.parametrize("run_id", [None, "", ["ambiguous"]])
def test_session_requires_a_stable_string_id(tmp_path, run_id):
    log = SessionLog()
    log.events[0]["run_id"] = run_id
    report = analyze(log.save(tmp_path / "missing-identity.jsonl"))
    assert report["errors"]
    assert not report["complete"]
    assert report["summary"]["sessions_analyzed"] == 0


def test_finished_event_without_status_is_not_a_complete_session(tmp_path):
    log = SessionLog()
    log.add("run_finished")
    report = analyze(log.save(tmp_path / "missing-status.jsonl"))
    assert report["errors"]
    assert not report["complete"]


@pytest.mark.parametrize("location", ["event", "arguments"])
@pytest.mark.parametrize("number", ["1e309", "-1e309"])
def test_out_of_range_json_number_has_clear_json_error(tmp_path, capsys, location, number):
    log = SessionLog()
    if location == "arguments":
        started = log.start("overflow")
        started["args_json"] = '{"command":"echo check","estimate":' + number + '}'
    path = log.save(tmp_path / "overflow.jsonl")
    if location == "event":
        content = path.read_text(encoding="utf-8")
        path.write_text(content.replace('"task": "test"', '"task": {"estimate":' + number + '}'),
                        encoding="utf-8")
    assert cli("analyze", "--input", str(path), "--json") == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["errors"] and not report["complete"]
    assert "конечных" in report["errors"][0]["message"]
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("error_code", [{"invalid": "mapping"}, ["invalid", "list"]])
def test_nonstring_error_code_has_clear_json_error(tmp_path, capsys, error_code):
    log = SessionLog()
    log.start("invalid-error", tool="complete_task")
    log.finish("invalid-error", {"ok": False, "error": {"code": error_code}})
    path = log.save(tmp_path / "bad-error-code.jsonl")
    assert cli("analyze", "--input", str(path), "--json") == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["errors"] and not report["complete"]
    assert "error.code" in report["errors"][0]["message"]
    assert "Traceback" not in captured.err


def test_orphan_success_result_breaks_a_failure_chain(tmp_path):
    log = SessionLog()
    log.bash("before")
    log.finish("missing-start", {"ok": True, "data": {
        "command": "python -m unittest -v", "cwd": ".", "exit_code": 0,
        "timed_out": False, "stdout": "", "stderr": "", "truncated": False,
    }})
    log.bash("after")
    report = analyze(log.save(tmp_path / "orphan-success.jsonl"))
    assert report["summary"]["failed_executions"] == 2
    assert report["summary"]["repeat_episodes"] == 0
    assert not report["complete"]
    assert any(warning["code"] == "orphan_result" for warning in report["sessions"][0]["warnings"])


@pytest.mark.parametrize("duplicate", ["start", "result"])
def test_ambiguous_unrelated_protocol_event_breaks_failure_chain(tmp_path, duplicate):
    log = SessionLog()
    log.bash("before")
    started = log.add("tool_call_started", id="read", name="read_file", attempt=1,
                      args_json='{"path":"order.py"}')
    if duplicate == "start":
        log.add("tool_call_started", **{key: value for key, value in started.items()
                                      if key not in ("event", "seq", "ts")})
    result = {"ok": True, "data": {"content": "x = 1"}}
    log.finish("read", result)
    if duplicate == "result":
        log.finish("read", result)
    log.bash("after")
    report = analyze(log.save(tmp_path / "ambiguous.jsonl"))
    assert report["summary"]["failed_executions"] == 2
    assert report["summary"]["repeat_episodes"] == 0
    assert not report["complete"]
    assert any(warning["code"] == f"duplicate_{duplicate}"
               for warning in report["sessions"][0]["warnings"])


@pytest.mark.parametrize("location", ["event-value", "arguments-key"])
def test_lone_unicode_surrogate_has_clear_json_error(tmp_path, capsys, location):
    log = SessionLog()
    if location == "arguments-key":
        started = log.start("invalid-unicode")
        started["args_json"] = r'{"command":"echo check","\ud800":"invalid key"}'
    path = log.save(tmp_path / "surrogate.jsonl")
    if location == "event-value":
        content = path.read_text(encoding="utf-8")
        path.write_text(content.replace('"task": "test"', r'"task": {"note":"\ud800"}'),
                        encoding="utf-8")
    assert cli("analyze", "--input", str(path), "--json") == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["errors"] and not report["complete"]
    assert "суррогат" in report["errors"][0]["message"]
    assert "Traceback" not in captured.err


def test_non_bmp_unicode_and_valid_surrogate_pairs_are_preserved(tmp_path, capsys):
    log = SessionLog()
    log.events[0]["метка🙂"] = {"message": "Данные 🙂"}
    log.bash("unicode", stderr="Ошибка 🙂")
    path = log.save(tmp_path / "unicode.jsonl")
    content = path.read_text(encoding="utf-8")
    path.write_text(content.replace("🙂", r"\ud83d\ude42"), encoding="utf-8")
    assert cli("analyze", "--input", str(path), "--json") == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert not report["errors"]
    assert report["sessions"][0]["executions"][0]["output"] == "Ошибка 🙂"
    assert captured.err == ""
