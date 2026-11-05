# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Executable catalog cells preserve the approved verifier and reject model mixing."""

from pathlib import Path

import pytest

from dokimi.harnesses.runner import TaskSuiteRunner
from dokimi.runner import ci_catalog
from tests.test_ci_catalog import catalog


def with_task(root: Path, text: str) -> Path:
    root = catalog(root)
    task = root / "evaluation/harbor/tasks/demo-task"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "solution").mkdir()
    (task / "task.toml").write_text('[metadata]\nskill="demo"\n[agent]\ntimeout_sec=60\n[verifier]\ntimeout_sec=10\n[environment]\n')
    (task / "instruction.md").write_text(text)
    (task / "environment/Dockerfile").write_text('FROM ubuntu:22.04\nWORKDIR /app\n')
    (task / "tests/test.sh").write_text('echo 1 > /logs/verifier/reward.txt\n')
    (task / "solution/solve.sh").write_text('true\n')
    return root


def native(head, base=None):
    return ci_catalog.plan_catalog(head, base, subjects=["demo"], model="claude-sonnet-4-5-20250929",
                                   provider="anthropic", judge_model="gpt-4o", judge_provider="openai",
                                   reps=1, harness="docker-claude")


def test_native_uses_base_task_bytes_and_distinct_skill_arms(tmp_path, monkeypatch):
    base = with_task(tmp_path / "base", "APPROVED VERIFIER TASK")
    head = with_task(tmp_path / "head", "CANDIDATE EASIER TASK")
    seen = []
    def run(runner):
        seen.append((runner.tasks[0].instruction, runner.harnesses[0]))
        return {"results": [{"passed": True, "reward": 1, "metrics": {
            "cost_usd": .02, "models_used": [runner.model], "n_skill_invocations": 1}}]}
    monkeypatch.setattr(TaskSuiteRunner, "run", run)
    monkeypatch.setattr(ci_catalog, "_preflight", lambda *_args: None)
    out = ci_catalog.run_catalog(native(head, base), max_cost_usd=1)
    assert {text for text, _ in seen} == {"APPROVED VERIFIER TASK"}
    assert seen[0][1] == "docker-claude"
    assert str(base / "skills/demo") in seen[1][1]
    assert str(head / "skills/demo") in seen[2][1]
    assert all(row["score"] == 1 for row in out["runs"][0]["observations"])


def test_native_rejects_an_agent_using_a_second_model(tmp_path, monkeypatch):
    head = with_task(tmp_path / "head", "TASK")
    monkeypatch.setattr(ci_catalog, "_preflight", lambda *_args: None)
    monkeypatch.setattr(TaskSuiteRunner, "run", lambda self: {"results": [
        {"passed": True, "reward": 1, "metrics": {"cost_usd": .02, "models_used": [self.model, "other-model"]}}]})
    out = ci_catalog.run_catalog(native(head), max_cost_usd=1)
    assert out["runs"][0]["result"]["cells"][0]["status"] == "invalid"
    assert out["runs"][0]["observations"][0]["score"] is None


def test_native_preflight_refuses_openai_provider_instead_of_calling_claude(tmp_path):
    prepared = native(with_task(tmp_path / "head", "TASK"))
    prepared.provider = "openai"
    with pytest.raises(ValueError, match="docker-claude.*provider"):
        ci_catalog.run_catalog(prepared, max_cost_usd=1)
