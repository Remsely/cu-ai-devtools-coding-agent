"""Независимые проверки результата; запускаются только внутри контейнера."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import math
import sys


def grade(task_id: str, workspace: str) -> dict:
    checks: list[dict] = []
    captured = io.StringIO()

    def check(name, operation):
        try:
            operation()
            checks.append({"name": name, "passed": True})
        except Exception as err:
            checks.append({"name": name, "passed": False,
                           "reason": f"{type(err).__name__}: {err}"})

    def equals(actual, expected):
        assert isinstance(actual, (int, float)) and math.isclose(
            actual, expected, rel_tol=1e-9, abs_tol=1e-9
        ), f"expected {expected!r}, got {actual!r}"

    def rejects(function, *args):
        try:
            function(*args)
        except ValueError:
            return
        raise AssertionError(f"expected ValueError for {args!r}")

    sys.path.insert(0, workspace)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        try:
            order = importlib.import_module("order")
        except Exception as err:
            return {"status": "fail", "checks": [{"name": "import", "passed": False,
                    "reason": f"{type(err).__name__}: {err}"}],
                    "captured_output": captured.getvalue()[-2000:]}

        if task_id == "discount":
            f = getattr(order, "calculate_total", None)
            for prices, discount, expected in [([100, 50], 10, 135), ([], 25, 0),
                    ([12.5, 7.5], 12.5, 17.5), ([100], 0, 100), ([100], 100, 0)]:
                check(f"total:{prices}:{discount}",
                      lambda p=prices, d=discount, e=expected: equals(f(p, d), e))
            for d in (-0.01, 100.01):
                check(f"reject_discount:{d}", lambda d=d: rejects(f, [100], d))
        elif task_id == "shipping":
            f = getattr(order, "calculate_shipping", None)
            for args, expected in [((99,), 10), ((100,), 0), ((101,), 0), ((0,), 10),
                    ((0, 0, 7), 0), ((49.5, 50, 3.5), 3.5), ((50, 50, 3.5), 0)]:
                check(f"shipping:{args}", lambda a=args, e=expected: equals(f(*a), e))
            for args in [(-0.01,), (10, -0.01), (10, 100, -0.01)]:
                check(f"reject_shipping:{args}", lambda a=args: rejects(f, *a))
        elif task_id == "quantity":
            f = getattr(order, "calculate_line_total", None)
            for args, expected in [((12.5, 3), 37.5), ((12.5, 0), 0), ((0, 5), 0),
                    ((0.1, 3), 0.3), ((5, 1), 5)]:
                check(f"line_total:{args}", lambda a=args, e=expected: equals(f(*a), e))
            for args in [(-0.01, 2), (10, -1), (10, 1.5), (10, True), (10, False)]:
                check(f"reject_quantity:{args}", lambda a=args: rejects(f, *a))
        else:
            return {"status": "error", "reason": f"unknown task: {task_id}"}

    return {"status": "pass" if all(c["passed"] for c in checks) else "fail",
            "checks": checks, "captured_output": captured.getvalue()[-2000:]}


if __name__ == "__main__":
    result = grade(sys.argv[1], sys.argv[2])
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "pass" else 1)
