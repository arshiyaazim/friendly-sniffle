"""Markers for tests that read private, deliberately-uncommitted operational data.

This repository ships code, not the owner's business corpus. `.gitignore` keeps
`knowledge_base/`, `resources/ops/`, `resources/*.csv` and the bridge stores out
of git, so a fresh checkout genuinely does not have them — which is correct. A
test that reads one of those files cannot pass there, and "fails" is the wrong
report: it is unanswerable, not broken.

`requires_private()` returns a `skipif` marker that says exactly which file is
missing and how to supply it, so:

  * the run summary separates "not exercised here" from "broken";
  * on the machine that *does* hold the corpus, the test runs normally and still
    guards the behaviour.

Usage:

    from tests.support.private_data import requires_private

    _NEEDS_SOURCE = requires_private("resources/ops/recruitment_source_of_truth.txt")

    @_NEEDS_SOURCE
    def test_something_about_the_source(): ...
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ROLE_HINT = (
    "copy it from the deployment that owns it, or point the module's *_PATH "
    "setting at a test copy"
)


def present(relative_path: str) -> bool:
    return (REPO_ROOT / relative_path).exists()


def requires_private(relative_path: str, *, why: str = "") -> pytest.MarkDecorator:
    """``skipif`` marker: the test needs a file this repository never commits."""
    target = REPO_ROOT / relative_path
    reason = f"private operational data not in this checkout: {relative_path}"
    if why:
        reason += f" ({why})"
    reason += f" — {ROLE_HINT}"
    return pytest.mark.skipif(not target.exists(), reason=reason)
