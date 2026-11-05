# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Hosted receipts bind GitHub's exact run and attempt, rather than just a commit."""

from pathlib import Path
import subprocess

import pytest

from dokimi.runner.ci_source import source_identity


@pytest.fixture
def source(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "source"], check=True)
    return tmp_path


def test_hosted_source_records_numeric_run_and_attempt(source: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    identity = source_identity(source)
    assert identity["run_id"] == 123456 and identity["run_attempt"] == 2


@pytest.mark.parametrize("run_id,attempt", [("", "1"), ("0", "1"), ("1", "0"), ("x", "1"), ("1", "-1"), ("1", "1.5")])
def test_malformed_hosted_run_identity_is_rejected(source: Path, monkeypatch, run_id: str, attempt: str):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
    with pytest.raises(ValueError, match="GitHub"):
        source_identity(source)


def test_local_source_does_not_claim_ambient_run_variables(source: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    identity = source_identity(source)
    assert "run_id" not in identity and "run_attempt" not in identity
