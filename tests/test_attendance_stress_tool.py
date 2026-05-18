from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def load_stress_module():
    module_path = Path(__file__).resolve().parents[1] / "tools" / "attendance_stress.py"
    spec = importlib.util.spec_from_file_location("attendance_stress", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def passing_report() -> dict:
    return {
        "websockets": 2,
        "metrics": {
            "endpoints": {
                "health": {
                    "status_counts": {"200": 100},
                    "error_counts": {},
                    "p95_ms": 100,
                    "max_ms": 500,
                },
                "auth-bootstrap": {
                    "status_counts": {"200": 50},
                    "error_counts": {},
                    "p95_ms": 100,
                    "max_ms": 500,
                },
                "websocket-bootstrap": {
                    "status_counts": {"101": 4},
                    "error_counts": {},
                    "p95_ms": 100,
                    "max_ms": 500,
                },
            }
        },
    }


def test_acceptance_gate_allows_clean_stress_report() -> None:
    stress = load_stress_module()

    assert stress.acceptance_failures(passing_report(), allow_failures=False) == []


def test_acceptance_gate_fails_errors_5xx_and_slow_health() -> None:
    stress = load_stress_module()
    report = passing_report()
    report["metrics"]["endpoints"]["health"]["p95_ms"] = 501
    report["metrics"]["endpoints"]["student-check-in"] = {
        "status_counts": {"200": 1, "503": 1, "error": 1},
        "error_counts": {"ReadTimeout": 1},
        "p95_ms": 100,
        "max_ms": 200,
    }

    failures = stress.acceptance_failures(report, allow_failures=False)

    assert any("health: p95_ms" in failure for failure in failures)
    assert any("student-check-in: unexpected client/runtime errors" in failure for failure in failures)
    assert any("student-check-in: request errors recorded" in failure for failure in failures)
    assert any("student-check-in: unexpected 5xx responses" in failure for failure in failures)


def test_acceptance_gate_can_be_disabled_for_baseline_reproduction() -> None:
    stress = load_stress_module()
    report = passing_report()
    report["metrics"]["endpoints"]["health"]["error_counts"] = {"ReadTimeout": 1}
    report["metrics"]["endpoints"]["health"]["status_counts"] = {"200": 1, "error": 1}

    assert stress.acceptance_failures(report, allow_failures=True) == []
