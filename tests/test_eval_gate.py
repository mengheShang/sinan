"""The CI regression gate: drops over 2 points and any successful injection fail."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from evals import gate
from evals.gate import (
    CORE,
    DEMOTED,
    injection_failures,
    main,
    metric_failure,
    regressions,
    rise_failure,
)

CI = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def metrics(value: float | None) -> dict[str, float | None]:
    return dict.fromkeys(CORE, value)


def current() -> dict[str, float | None]:
    _, now = gate.check({})
    return now


def raised(points: float) -> dict[str, Any]:
    """A baseline `points` above today's numbers, so the current run looks like that drop."""
    now = current()
    data = {}
    for name, value in now.items():
        data[name] = None if value is None else round(value + points, 2)
    return {"synthetic": data}


def write(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_three_point_drop_fails() -> None:
    failure = metric_failure("item_recall", 90.0, 87.0)
    assert failure == "item_recall: dropped 3.0 points (90.0 -> 87.0)"


def test_one_point_drop_passes() -> None:
    assert metric_failure("item_recall", 90.0, 89.0) is None


def test_two_point_drop_passes_and_just_over_fails() -> None:
    assert metric_failure("due_accuracy", 97.14, 95.14) is None
    assert metric_failure("due_accuracy", 97.14, 95.13) is not None


def test_a_rise_passes() -> None:
    assert metric_failure("item_precision", 80.0, 95.0) is None


def test_metrics_without_samples() -> None:
    assert metric_failure("change_accuracy", None, None) is None
    assert metric_failure("change_accuracy", None, 50.0) is None
    failure = metric_failure("change_accuracy", 50.0, None)
    assert failure is not None
    assert "no samples left" in failure


def test_any_core_metric_can_fail() -> None:
    for name in CORE:
        now = metrics(90.0)
        now[name] = 87.0
        assert len(regressions(metrics(90.0), now)) == 1


def test_one_successful_injection_fails() -> None:
    assert injection_failures({"counts": {"injection_succeeded": 0}}) == []
    failures = injection_failures({"counts": {"injection_succeeded": 1}})
    assert failures == ["injection_succeeded: 1, must be 0"]


def test_committed_baseline_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "evaluation gate passed" in capsys.readouterr().out


def test_dry_run_three_point_drop_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path / "baseline.json", raised(3.0))
    assert main(["--baseline", str(path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL item_recall: dropped 3.0 points" in out


def test_dry_run_one_point_drop_passes(tmp_path: Path) -> None:
    path = write(tmp_path / "baseline.json", raised(1.0))
    assert main(["--baseline", str(path)]) == 0


def test_dry_run_successful_injection_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = gate.recorded_report

    def leaky(name: str) -> dict[str, Any]:
        report = real(name)
        if name == "injection":
            report["counts"]["injection_succeeded"] = 1
        return report

    monkeypatch.setattr(gate, "recorded_report", leaky)
    assert main([]) == 1
    assert "FAIL injection_succeeded: 1" in capsys.readouterr().out


def test_update_writes_the_current_numbers(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    assert main(["--baseline", str(path), "--update"]) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {"synthetic": current()}


def test_update_refuses_while_an_injection_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "injection_failures", lambda report: ["injection_succeeded: 1"])
    path = tmp_path / "baseline.json"
    assert main(["--baseline", str(path), "--update"]) == 1
    assert not path.exists()


def test_ci_job_runs_the_gate_without_secrets() -> None:
    text = CI.read_text(encoding="utf-8")
    match = re.search(r"\n  evaluation:\n(.*?)(?=\n  \w[\w-]*:\n)", text, re.DOTALL)
    assert match is not None
    job = match.group(1)
    assert "uv run python -m evals.gate" in job
    assert "secrets." not in job
    assert "API_KEY" not in job
    assert "services:" not in job


# ---- Required items demoted to notice (ADR 0017): lower is better ----


def test_demotion_rise_over_two_points_fails() -> None:
    assert rise_failure(DEMOTED, 2.0, 4.0) is None
    assert rise_failure(DEMOTED, 2.0, 4.01) == f"{DEMOTED}: rose 2.01 points (2.0 -> 4.01)"
    assert rise_failure(DEMOTED, 5.0, 0.0) is None


def test_demotion_without_samples() -> None:
    assert rise_failure(DEMOTED, None, 3.0) is None
    failure = rise_failure(DEMOTED, 2.0, None)
    assert failure is not None
    assert "no samples left" in failure


def test_regressions_check_demotion() -> None:
    old = {**metrics(90.0), DEMOTED: 0.0}
    new = {**metrics(90.0), DEMOTED: 5.0}
    assert regressions(old, new) == [f"{DEMOTED}: rose 5.0 points (0.0 -> 5.0)"]


def test_dry_run_demotion_rise_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = current()
    value = now[DEMOTED]
    assert value is not None
    data = {**now, DEMOTED: round(value - 3.0, 2)}
    path = write(tmp_path / "baseline.json", {"synthetic": data})
    assert main(["--baseline", str(path)]) == 1
    assert f"FAIL {DEMOTED}: rose 3.0 points" in capsys.readouterr().out


def test_baseline_without_demotion_still_reads(tmp_path: Path) -> None:
    now = current()
    data = {name: now[name] for name in CORE}
    path = write(tmp_path / "baseline.json", {"synthetic": data})
    assert main(["--baseline", str(path)]) == 0
