"""The Worker's extraction job setup: which rules load, and running without a key."""

import logging
from pathlib import Path

import pytest

from app.config import Settings
from app.extraction.prefilter import EXAMPLE_RULES, RulesError
from apps.worker.extract_job import extract_all, load_prefilter, rules_path


def test_missing_local_rules_fall_back_to_the_example(tmp_path: Path) -> None:
    settings = Settings(sinan_prefilter_rules=str(tmp_path / "absent.toml"))
    assert rules_path(settings) == EXAMPLE_RULES


def test_local_rules_are_used_when_present(tmp_path: Path) -> None:
    local = tmp_path / "prefilter.toml"
    local.write_text('[skip]\nkeywords = ["webinar"]\n', encoding="utf-8")
    settings = Settings(sinan_prefilter_rules=str(local))
    assert rules_path(settings) == local
    assert load_prefilter(settings).skip.keywords == ("webinar",)


def test_malformed_local_rules_stop_the_worker(tmp_path: Path) -> None:
    local = tmp_path / "prefilter.toml"
    local.write_text('[skip]\nsender = ["a@example.com"]\n', encoding="utf-8")
    settings = Settings(sinan_prefilter_rules=str(local))
    with pytest.raises(RulesError, match="unknown keys"):
        load_prefilter(settings)


def test_no_extractor_skips_extraction(caplog: pytest.LogCaptureFixture) -> None:
    def no_sessions() -> None:
        raise AssertionError("must not open a session")

    caplog.set_level(logging.INFO, logger="sinan.worker.extract")
    rules = load_prefilter(Settings())
    extract_all(no_sessions, None, rules, cap=10)  # type: ignore[arg-type]
    assert "no ANTHROPIC_API_KEY" in caplog.text
