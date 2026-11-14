"""The label format models in app/db/label_format.py, moved from evals/dataset.py."""

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.db import label_format as fmt
from evals import dataset

BODY = "Quiz 2 is due Friday Nov 13 at 11:59pm PT. Private marker text."
ROOT = Path(__file__).resolve().parents[1]


def snapshot() -> dict[str, Any]:
    return {
        "from_addr": "chen@example.edu",
        "subject": "MATH 000: Quiz 2",
        "body_text": BODY,
        "sent_at": "2026-11-02T17:00:00Z",
        "role": "school",
        "headers": {"Precedence": "bulk"},
    }


def item(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ref": "math000-quiz2",
        "kind": "deadline",
        "course": "MATH 000",
        "title": "Quiz 2",
        "due_raw": "Friday Nov 13 at 11:59pm PT",
        "due_at": "2026-11-14T07:59:00Z",
        "due_ambiguous": False,
        "changes": None,
    }
    data.update(over)
    return data


def test_evals_reexports_the_same_models() -> None:
    names = ("Snapshot", "ItemLabel", "Labels", "Sample", "HEADER_NAMES", "describe")
    for name in names:
        assert getattr(dataset, name) is getattr(fmt, name)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("top", ["app", "apps"])
def test_backend_code_never_imports_evals(top: str) -> None:
    # The backend image does not ship evals/
    for path in (ROOT / top).rglob("*.py"):
        modules = imported_modules(path)
        bad = [m for m in modules if m == "evals" or m.startswith("evals.")]
        assert not bad, f"{path.relative_to(ROOT)} imports {bad}"


def test_snapshot_keeps_only_known_headers() -> None:
    assert fmt.Snapshot.model_validate(snapshot()).headers == {"Precedence": "bulk"}
    data = snapshot()
    data["headers"] = {"To": "class@example.edu"}
    with pytest.raises(ValidationError):
        fmt.Snapshot.model_validate(data)


def test_snapshot_needs_utc_time() -> None:
    data = snapshot()
    data["sent_at"] = "2026-11-02T09:00:00-08:00"
    with pytest.raises(ValidationError):
        fmt.Snapshot.model_validate(data)


@pytest.mark.parametrize(
    "over",
    [
        {"due_raw": None},
        {"due_at": None},
        {"due_ambiguous": True},
        {"ref": "Bad Ref"},
        {"kind": "event"},
    ],
)
def test_bad_item_is_rejected(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        fmt.ItemLabel.model_validate(item(**over))


def test_ambiguous_item_has_no_time() -> None:
    label = fmt.ItemLabel.model_validate(item(due_at=None, due_ambiguous=True))
    assert label.due_at is None


def test_labels_reject_unknown_fields() -> None:
    data = {"items": [], "safe_to_auto_read": True, "extra": 1}
    with pytest.raises(ValidationError):
        fmt.Labels.model_validate(data)


def test_missing_due_raw_names_refs() -> None:
    snap = fmt.Snapshot.model_validate(snapshot())
    labels = fmt.Labels.model_validate(
        {"items": [item(), item(ref="other", due_raw="Monday")], "safe_to_auto_read": False}
    )
    assert fmt.missing_due_raw(snap, labels) == ["other"]


def test_sample_checks_due_raw_is_in_the_email() -> None:
    labels = {"items": [item(due_raw="Monday")], "safe_to_auto_read": False}
    data = {"id": "t-1", "input": snapshot(), "labels": labels}
    with pytest.raises(ValidationError):
        fmt.Sample.model_validate(data)


def test_describe_leaves_out_input_values() -> None:
    data = snapshot()
    data["from_addr"] = "Private marker text"
    with pytest.raises(ValidationError) as caught:
        fmt.Snapshot.model_validate(data)
    text = fmt.describe(caught.value)
    assert text.startswith("from_addr:")
    assert "marker" not in text


def test_notice_is_a_label_kind() -> None:
    labeled = fmt.ItemLabel.model_validate(item(kind="notice"))
    assert labeled.kind == "notice"
    with pytest.raises(ValidationError):
        fmt.ItemLabel.model_validate(item(kind="announcement"))
