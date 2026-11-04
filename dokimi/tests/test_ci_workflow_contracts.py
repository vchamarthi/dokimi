# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Workflow permission and exact-revision boundaries are deployment contracts."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _wf(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / f"dokimi-{name}.yml").read_text())


def test_free_and_paid_jobs_have_separate_credentials_and_approval() -> None:
    wf = _wf("pr")
    free = wf["jobs"]["check"]
    paid = wf["jobs"]["run"]
    assert free["permissions"] == {"contents": "read"}
    assert "secrets." not in str(free)
    assert paid["environment"] == "${{ inputs.environment }}"
    assert "workflow_dispatch" in paid["if"] and "inputs.mode == 'run'" in paid["if"]
    assert all(step.get("with", {}).get("persist-credentials") is False
               for job in wf["jobs"].values() for step in job.get("steps", [])
               if str(step.get("uses", "")).startswith("actions/checkout@"))


def test_publisher_has_no_code_checkout_or_package_install() -> None:
    wf = _wf("publish")
    assert wf["permissions"] == {}
    assert wf["jobs"]["publish"]["permissions"] == {"actions": "read", "pull-requests": "read", "issues": "write"}
    assert all("run" not in step for step in wf["jobs"]["publish"]["steps"])
    assert "actions/checkout" not in str(wf)


def test_record_writes_only_the_baseline_branch_with_cas() -> None:
    wf = _wf("record")
    steps = wf["jobs"]["record"]["steps"]
    push = next(s for s in steps if s.get("name") == "Commit baseline with compare-and-swap")
    assert "--force-with-lease" in push["run"]
    assert "refs/heads/dokimi-baselines" in push["run"]
    assert "refs/heads/main" not in push["run"]
    assert "--expect-previous" in push["run"]


def test_revalidation_cannot_promote_or_write_repository() -> None:
    wf = _wf("revalidate")
    assert wf["jobs"]["revalidate"]["permissions"] == {"contents": "read"}
    text = str(wf)
    assert "ci revalidate" in text and "git push" not in text


def test_free_check_supplies_an_independent_base_checkout() -> None:
    steps = _wf("pr")["jobs"]["check"]["steps"]
    base = next(s for s in steps if s.get("with", {}).get("path") == "base")
    assert "pull_request.base.sha" in base["with"]["ref"]
    check = next(s for s in steps if s.get("name") == "Run free content checks")
    assert '--base-root "$BASE_ROOT"' in check["run"]
    assert check["env"]["BASE_ROOT"] == "${{ github.workspace }}/base"


def test_hosted_paid_controller_rejects_execution_harnesses() -> None:
    steps = _wf("pr")["jobs"]["run"]["steps"]
    guard = next(s for s in steps if s.get("name") == "Validate exact approved candidate")
    assert "process.env.HARNESS !== 'arms-runner'" in guard["with"]["script"]
    assert "process.env.SUBJECT_KIND !== 'catalog'" in guard["with"]["script"]
