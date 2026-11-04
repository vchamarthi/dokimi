# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Repository CI uses real repository contracts and keeps failed evidence blocking."""

import json
import os
from pathlib import Path

import pytest
import yaml

from dokimi import process
from dokimi.cli import build_parser
from dokimi.runner.ci_policy import DEFAULT_POLICY, gate_bundle, policy_digest
from dokimi.runner.ci_repository import check_repository, run_repository, verify_repository_identity
from dokimi.runner.plan import completeness
from repo_eval_fixture import FakeRunner, fake_harness, git, make_repo, write


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root, _ = make_repo(tmp_path)
    base = git(root, "rev-parse", "HEAD")
    write(root, "AGENTS.md", "# Guidance\nRead `src/calc.py`.\n")
    git(root, "add", "AGENTS.md")
    git(root, "commit", "-qm", "guidance candidate")
    head = git(root, "rev-parse", "HEAD")
    exe, profile = fake_harness(tmp_path)
    profile_doc = yaml.safe_load(profile.read_text())
    profile_doc["delegation"] = {"none": {"all": profile_doc["tools"]["all"]}}
    profile.write_text(yaml.safe_dump(profile_doc))
    monkeypatch.setenv("PATH", str(exe.parent) + os.pathsep + os.environ["PATH"])
    fake = FakeRunner()
    monkeypatch.setattr("dokimi.repos.container.DockerRunner", lambda *args, **kwargs: fake)
    source = {"repository": "owner/toy", "base_sha": base, "head_sha": head}
    return {"root": root, "source": source, "harness": str(profile), "fake": fake, "out": tmp_path / "ci"}


def inprocess_cli(monkeypatch):
    calls = []
    def invoke(arguments, directory, label):
        calls.append(arguments)
        args = build_parser().parse_args(arguments)
        try:
            args.func(args)
        except SystemExit as exc:
            return process.Completed(tuple(arguments), int(exc.code or 0))
        return process.Completed(tuple(arguments), 0)
    monkeypatch.setattr("dokimi.runner.ci_repository._invoke", invoke)
    return calls


def run(s, *, reps=1):
    return run_repository(s["root"], s["root"], s["out"], s["source"], "m-1", s["harness"], reps, 10)


def test_static_checks_fail_a_real_broken_guidance_claim(tmp_path):
    write(tmp_path, "src/existing.py", "x = 1\n")
    write(tmp_path, "AGENTS.md", "# Guidance\nRead `src/missing.py`.\n")
    parts = check_repository(tmp_path)
    assert any(c["status"] == "fail" for c in parts["result"]["cells"])
    assert parts["identity"]["kind"] == "repo"
    assert parts["identity"]["model"] is None


def test_static_checks_accept_repaired_claim_and_change_content_digest(tmp_path):
    write(tmp_path, "src/existing.py", "x = 1\n")
    write(tmp_path, "AGENTS.md", "# Guidance\nRead `src/file.py`.\n")
    before = check_repository(tmp_path)
    write(tmp_path, "src/file.py", "value = 1\n")
    after = check_repository(tmp_path)
    assert any(c["status"] == "fail" for c in before["result"]["cells"])
    assert all(c["status"] == "pass" for c in after["result"]["cells"])
    write(tmp_path, "AGENTS.md", "# Guidance\nRead `src/file.py` carefully.\n")
    assert check_repository(tmp_path)["identity"]["content_digest"] != after["identity"]["content_digest"]


def test_static_prefers_github_manifest_over_agent_eval(tmp_path):
    write(tmp_path, "AGENTS.md", "Read `bad.py`.\n")
    write(tmp_path, "CUSTOM.md", "Read `good.py`.\n")
    write(tmp_path, "good.py", "x = 1\n")
    write(tmp_path, ".github/dokimi/repo.yaml", "schema: repo_manifest.v1\nid: toy\nguidance:\n  globs: [CUSTOM.md]\n")
    parts = check_repository(tmp_path)
    assert all(c["status"] == "pass" for c in parts["result"]["cells"])


def test_live_runner_executes_selfcheck_then_three_fixed_guidance_arms(repository, monkeypatch):
    calls = inprocess_cli(monkeypatch)
    parts = run(repository, reps=2)
    assert calls[0][:2] == ["eval", "check"]
    assert calls[1][:2] == ["repo", "run"]
    expected = str(repository["root"]) + "@" + repository["source"]["base_sha"]
    assert calls[0][2] == calls[1][2] == expected
    assert calls[1][calls[1].index("--agent-network") + 1] == "bridge"
    assert calls[1][calls[1].index("--max-batch-usd") + 1] == "10"
    assert completeness(parts["plan"], parts["result"]).complete
    assert len(parts["plan"]["cells"]) == 12
    assert {c["arm"] for c in parts["plan"]["cells"]} == {"none", "approved", "candidate"}
    assert parts["repository_runtime"]["harness_version"] == "1.2.3"
    assert parts["repository_runtime"]["agent_network"] == "bridge"
    assert all(o["cost_usd"] == 0.01 for o in parts["observations"])
    assert {o["score"] for o in parts["observations"]} == {1.0}
    bundle = {"schema_version": "ci_run.v1", "mode": "live", "policy_digest": policy_digest(DEFAULT_POLICY),
              "source": repository["source"], **parts}
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "fail"


def test_broken_selfcheck_prevents_every_agent_call(repository, monkeypatch):
    calls = []
    def broken(arguments, directory, label):
        calls.append(arguments)
        destination = Path(arguments[arguments.index("--out") + 1]) / "check"
        destination.mkdir(parents=True)
        (destination / "check.json").write_text(json.dumps({"tasks": [
            {"task": "fix_add", "verdict": "broken"}, {"task": "review_add", "verdict": "ok"}]}))
        return process.Completed(tuple(arguments), 0)
    monkeypatch.setattr("dokimi.runner.ci_repository._invoke", broken)
    parts = run(repository)
    assert len(calls) == 1
    assert all(c["status"] == "invalid" for c in parts["result"]["cells"])
    assert all(o["score"] is None for o in parts["observations"])


def test_missing_selfcheck_receipt_does_not_trust_exit_zero(repository, monkeypatch):
    calls = []
    def no_receipt(arguments, directory, label):
        calls.append(arguments)
        return process.Completed(tuple(arguments), 0)
    monkeypatch.setattr("dokimi.runner.ci_repository._invoke", no_receipt)
    parts = run(repository)
    assert len(calls) == 1
    assert {c["status"] for c in parts["result"]["cells"]} == {"infra_error"}


def mutate_record(path, corruption):
    if corruption == "missing":
        path.unlink()
        return
    patches = {
        "revision": ("meta.json", ["arm_rev"], {"sha": "0" * 40}),
        "task-base": ("meta.json", ["base_rev"], "0" * 40),
        "score-max": ("result.json", ["score_max"], 99),
        "resolved": ("result.json", ["model_resolved"], "other-model"),
        "version": ("result.json", ["harness_version"], "9.9.9"),
        "execution": ("result.json", ["execution", "id"], "other"),
        "grade": ("result.json", ["grade", "status"], "grader_failure"),
        "unknown-cost": ("result.json", ["agent", "cost_usd"], None),
    }
    name, keys, value = patches[corruption]
    target = path.parent / name
    document = json.loads(target.read_text())
    parent = document
    for key in keys[:-1]:
        parent = parent[key]
    parent[keys[-1]] = value
    target.write_text(json.dumps(document))


@pytest.mark.parametrize("corruption", ["resolved", "version", "execution", "grade", "unknown-cost", "missing", "revision", "score-max", "task-base"])
def test_corrupt_or_unmeasured_batch_cells_never_pass(repository, monkeypatch, corruption):
    inprocess_cli(monkeypatch)
    import dokimi.runner.ci_repository as adapter
    original = adapter._invoke
    def mutate(arguments, directory, label):
        response = original(arguments, directory, label)
        if arguments[:2] == ["repo", "run"]:
            batch = Path(arguments[arguments.index("--out") + 1]) / arguments[arguments.index("--batch") + 1]
            mutate_record(next(batch.glob("*/result.json")), corruption)
        return response
    monkeypatch.setattr(adapter, "_invoke", mutate)
    parts = run(repository)
    bundle = {"schema_version": "ci_run.v1", "mode": "live", "policy_digest": policy_digest(DEFAULT_POLICY),
              "source": repository["source"], **parts}
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert result["status"] in {"invalid", "infra_error", "not_run"}
    assert not result["promotion_eligible"]


def test_current_checkout_revalidation_detects_content_and_suite_drift(repository, monkeypatch):
    inprocess_cli(monkeypatch)
    parts = run(repository)
    bundle = {"schema_version": "ci_run.v1", "mode": "live", "policy_digest": policy_digest(DEFAULT_POLICY),
              "source": repository["source"], **parts}
    verify_repository_identity(repository["root"], bundle)
    write(repository["root"], "AGENTS.md", "changed guidance\n")
    with pytest.raises(ValueError, match="identity|content"):
        verify_repository_identity(repository["root"], bundle)
    write(repository["root"], "AGENTS.md", "# Guidance\nRead `src/calc.py`.\n")
    write(repository["root"], ".agent-eval/grade.py", "changed grader\n")
    with pytest.raises(ValueError, match="identity|suite"):
        verify_repository_identity(repository["root"], bundle)


def test_same_revision_never_reuses_one_run_as_two_conditions(repository, monkeypatch):
    calls = inprocess_cli(monkeypatch)
    repository["source"]["head_sha"] = repository["source"]["base_sha"]
    with pytest.raises(ValueError, match="distinct|different"):
        run(repository)
    assert calls == []


def test_empty_guidance_is_not_a_static_pass(tmp_path):
    parts = check_repository(tmp_path)
    assert any(c["status"] == "inconclusive" for c in parts["result"]["cells"])


def test_nonzero_run_exit_never_becomes_a_successful_measurement(repository, monkeypatch):
    inprocess_cli(monkeypatch)
    import dokimi.runner.ci_repository as adapter
    original = adapter._invoke
    def fail_after_results(arguments, directory, label):
        result = original(arguments, directory, label)
        return process.Completed(result.argv, 1) if arguments[:2] == ["repo", "run"] else result
    monkeypatch.setattr(adapter, "_invoke", fail_after_results)
    parts = run(repository)
    assert {c["status"] for c in parts["result"]["cells"]} == {"infra_error"}


def test_record_verifies_installed_harness_bytes(repository, monkeypatch):
    inprocess_cli(monkeypatch)
    parts = run(repository)
    binary = Path(repository["harness"]).parent / "bin" / "fake-agent"
    binary.write_text(binary.read_text() + "\n# changed executable\n")
    with pytest.raises(ValueError, match="harness identity"):
        verify_repository_identity(repository["root"], parts)
