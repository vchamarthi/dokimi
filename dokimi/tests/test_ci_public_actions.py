# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run public composite steps with an argv recorder, without GitHub or models."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _run_action(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    script = next(step["run"] for step in action["runs"]["steps"] if step.get("id") == "ci")
    executable = tmp_path / "dokimi"
    executable.write_text("#!/usr/bin/env python3\nimport json,os,sys\nopen(os.environ['RECEIPT'],'w').write(json.dumps(sys.argv[1:]))\n")
    executable.chmod(0o755)
    receipt = tmp_path / "receipt"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "RECEIPT": str(receipt),
           "MODE": "check", "ROOT": "content", "BASE": "a" * 40, "BASE_ROOT": "base",
           "OUT": str(tmp_path / "out"), "CONFIG": "", "POLICY": "", "SUBJECT_KIND": "catalog",
           "MAX_USD": "1", "MODEL": "m", "PROVIDER": "p", "JUDGE_MODEL": "j",
           "JUDGE_PROVIDER": "p", "REPS": "3", "SUBJECT": "one", "HARNESS": "arms-runner", **overrides}
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=env, capture_output=True, text=True)
    return result, json.loads(receipt.read_text()) if receipt.exists() else []


def test_check_preserves_hostile_paths_as_single_arguments(tmp_path: Path) -> None:
    result, argv = _run_action(tmp_path, ROOT="a path;$(false)", CONFIG="config path")
    assert result.returncode == 0, result.stderr
    assert argv[:3] == ["ci", "check", "a path;$(false)"]
    assert argv[argv.index("--config") + 1] == "config path"
    assert "--max-usd" not in argv


@pytest.mark.parametrize("mode", ["auto", "check;false", ""])
def test_unknown_mode_cannot_fall_through_to_paid_work(tmp_path: Path, mode: str) -> None:
    result, argv = _run_action(tmp_path, MODE=mode)
    assert result.returncode != 0 and argv == []


def test_paid_mode_binds_base_and_explicit_runtime(tmp_path: Path) -> None:
    result, argv = _run_action(tmp_path, MODE="run", POLICY="base/policy.json")
    assert result.returncode == 0, result.stderr
    assert argv[:3] == ["ci", "run", "content"]
    assert argv[argv.index("--base-root") + 1] == "base"
    assert argv[argv.index("--policy") + 1] == "base/policy.json"
    assert argv[argv.index("--max-usd") + 1] == "1"


def test_default_outputs_go_to_runner_temp(tmp_path: Path) -> None:
    result, argv = _run_action(tmp_path, OUT="", RUNNER_TEMP=str(tmp_path / "runner"))
    assert result.returncode == 0, result.stderr
    assert argv[argv.index("--out-dir") + 1] == str(tmp_path / "runner/dokimi-out")


@pytest.mark.parametrize("overrides", [{"HARNESS":"docker-claude"},{"SUBJECT_KIND":"repo"}])
def test_public_paid_action_requires_the_data_only_adapter(tmp_path: Path, overrides: dict) -> None:
    result, argv = _run_action(tmp_path, MODE="run", POLICY="base/policy.json", **overrides)
    assert result.returncode != 0 and argv == []


def test_report_action_uses_the_callers_trusted_policy(tmp_path: Path) -> None:
    action = yaml.safe_load((ROOT / "actions/report/action.yml").read_text())
    script = action["runs"]["steps"][0]["run"]
    executable = tmp_path / "dokimi"
    executable.write_text("#!/usr/bin/env python3\nimport json,os,pathlib,sys\na=sys.argv[1:]\n"
                          "pathlib.Path(os.environ['RECEIPT']).write_text(json.dumps(a))\n"
                          "pathlib.Path(a[a.index('--out')+1]).write_text('Scoped report')\n")
    executable.chmod(0o755)
    receipt = tmp_path / "argv.json"
    summary = tmp_path / "summary"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "RUN_FILE": "untrusted run.json",
           "OUT": str(tmp_path / "report.md"), "POLICY": "approved base/policy.json",
           "RECEIPT": str(receipt), "GITHUB_STEP_SUMMARY": str(summary)}
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    argv = json.loads(receipt.read_text())
    assert argv[argv.index("--policy") + 1] == "approved base/policy.json"
    assert summary.read_text() == "Scoped report"


def test_subscription_action_forwards_a_finite_call_cap_without_usd(tmp_path: Path) -> None:
    result, argv = _run_action(tmp_path, MODE="run", POLICY="base/policy.json", MAX_USD="", MAX_CALLS="18",
                               PROVIDER="claude-cli", JUDGE_PROVIDER="claude-cli")
    assert result.returncode == 0, result.stderr
    assert "--max-usd" not in argv
    assert argv[argv.index("--max-calls") + 1] == "18"
