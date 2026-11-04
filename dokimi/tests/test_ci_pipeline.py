# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""CI receipts bind actual inputs and never reuse an earlier successful run."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "fixtures/task_formats/catalog"
    root = tmp_path / "catalog"
    shutil.copytree(source, root)
    shutil.rmtree(root / "evaluation/harbor/tasks")
    (root / "skills/dpnp-io/SKILL.md").write_text(
        "---\nname: dpnp-io\ndescription: Read dpnp arrays from files.\n---\n# File IO\n"
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=vchamarthi", "-c",
                    "user.email=123653452+vchamarthi@users.noreply.github.com", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "fixture"], check=True)
    return root


def test_static_pipeline_binds_source_and_preserves_explicit_scope(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check

    out = tmp_path / "receipt"
    verdict = run_check(catalog, out, source_repository="vchamarthi/intel-skills")
    bundle = json.loads((out / "run.json").read_text())
    assert verdict["status"] == "pass"
    assert verdict["scope"] == "static"
    assert verdict["promotion_eligible"] is False
    assert bundle["source"]["repository"] == "vchamarthi/intel-skills"
    assert bundle["source"]["head_sha"] == bundle["source"]["checked_sha"]
    assert bundle["plan"]["budget"]["agent_calls"] == 0
    assert "not measured" in (out / "report.md").read_text().lower()
    assert json.loads((out / "catalog.json").read_text())["plan_digest"] == bundle["result"]["plan_digest"]


def test_false_expected_revision_is_rejected_before_receipts(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check

    out = tmp_path / "receipt"
    with pytest.raises(ValueError, match="head"):
        run_check(catalog, out, expected_head="f" * 40)
    assert not (out / "run.json").exists()


def test_check_failure_cannot_leave_an_earlier_success(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check

    out = tmp_path / "receipt"
    assert run_check(catalog, out)["status"] == "pass"
    skill = catalog / "skills/dpnp-io/SKILL.md"
    skill.write_text(skill.read_text().replace("name: dpnp-io", "name: invalid-name"))
    assert run_check(catalog, out)["status"] == "fail"
    assert json.loads((out / "gate.json").read_text())["status"] == "fail"


def test_outputs_inside_catalog_are_refused_without_changes(catalog: Path):
    from dokimi.runner.ci_pipeline import run_check

    with pytest.raises(ValueError, match="outside"):
        run_check(catalog, catalog / "out")
    assert not (catalog / "out").exists()


def test_receipt_directory_does_not_clobber_an_unrelated_file(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check

    out = tmp_path / "receipt"
    out.mkdir()
    keep = out / "customer-data.json"
    keep.write_text("keep")
    with pytest.raises(ValueError, match="dedicated"):
        run_check(catalog, out)
    assert keep.read_text() == "keep"


def test_symlink_output_directory_is_refused(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check

    target = tmp_path / "actual"
    target.mkdir()
    out = tmp_path / "linked"
    out.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        run_check(catalog, out)
    assert list(target.iterdir()) == []


def test_candidate_cannot_weaken_authoritative_base_policy(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check
    from dokimi.runner.ci_policy import DEFAULT_POLICY, policy_digest

    policy = catalog / "policy.json"
    policy.write_text(json.dumps({**DEFAULT_POLICY, "min_tasks": 2}))
    out = tmp_path / "receipt"
    run_check(catalog, out, policy_path=policy)
    bundle = json.loads((out / "run.json").read_text())
    assert bundle["policy_digest"] == policy_digest(DEFAULT_POLICY)


def test_invalid_candidate_controls_cannot_be_ignored(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check

    config = catalog / "bad-config.yaml"
    config.write_text("schema_version: not-a-dokimi-config\n")
    with pytest.raises(ValueError, match="config"):
        run_check(catalog, tmp_path / "receipt", config_path=config)
    assert not (tmp_path / "receipt/run.json").exists()


def test_supplied_base_checkout_must_match_pinned_revision(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(catalog), str(other)], check=True)
    (other / "extra").write_text("dirty data")
    with pytest.raises(ValueError, match="clean"):
        run_check(catalog, tmp_path / "receipt", base_root=other)
    assert not (tmp_path / "receipt/run.json").exists()


def test_cli_exposes_ci_operations_with_explicit_model_budget():
    from dokimi.cli import parse_args

    check = parse_args(["ci", "check", ".", "--out-dir", "/path/to/result"])
    assert check.ci_command == "check"
    with pytest.raises(SystemExit) as missing:
        parse_args(["ci", "run", ".", "--out-dir", "/path/to/result"])
    assert missing.value.code == 2


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-qm", message], check=True)


def test_candidate_policy_symlink_cannot_change_the_base_policy_path(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check
    from dokimi.runner.ci_policy import DEFAULT_POLICY, policy_digest

    strict = catalog / "policy.json"
    strict.write_text(json.dumps(DEFAULT_POLICY))
    (catalog / "weaker.json").write_text(json.dumps({**DEFAULT_POLICY, "min_tasks": 2}))
    _commit(catalog, "approved policies")
    strict.unlink()
    strict.symlink_to("weaker.json")
    out = tmp_path / "receipt"
    run_check(catalog, out, policy_path=strict)
    assert json.loads((out / "run.json").read_text())["policy_digest"] == policy_digest(DEFAULT_POLICY)


def test_ignored_uncommitted_base_controls_are_not_trusted(catalog: Path, tmp_path: Path):
    from dokimi.runner.ci_pipeline import run_check
    from dokimi.runner.ci_policy import DEFAULT_POLICY

    (catalog / ".gitignore").write_text("policy.json\n")
    _commit(catalog, "ignore local policy")
    policy = catalog / "policy.json"
    policy.write_text(json.dumps(DEFAULT_POLICY))
    with pytest.raises(ValueError, match="clean"):
        run_check(catalog, tmp_path / "receipt", base_root=catalog, policy_path=policy)


def test_invalid_revalidation_cannot_keep_an_earlier_success(catalog: Path, tmp_path: Path):
    from dokimi.cli import parse_args
    from dokimi.runner.ci_pipeline import run_check

    out = tmp_path / "receipt"
    assert run_check(catalog, out)["status"] == "pass"
    bad = tmp_path / "bad-baseline.json"
    bad.write_text("{}")
    args = parse_args(["ci", "revalidate", str(catalog), "--out-dir", str(out), "--baseline", str(bad),
                       "--max-usd", "1", "--model", "gpt-4o-mini", "--provider", "openai",
                       "--judge-model", "gpt-4o", "--judge-provider", "openai"])
    with pytest.raises(SystemExit) as failure:
        args.func(args)
    assert failure.value.code == 2
    assert not (out / "run.json").exists()


def test_merge_checkout_cannot_claim_a_different_base(catalog: Path):
    from dokimi.runner.ci_source import revision, source_identity

    original = revision(catalog)
    subprocess.run(["git", "-C", str(catalog), "checkout", "-qb", "updated-base"], check=True)
    (catalog / "base-change").write_text("new base")
    _commit(catalog, "advance base")
    subprocess.run(["git", "-C", str(catalog), "checkout", "-qb", "topic", original], check=True)
    (catalog / "candidate-change").write_text("candidate")
    _commit(catalog, "candidate")
    head = revision(catalog)
    subprocess.run(["git", "-C", str(catalog), "checkout", "-q", "updated-base"], check=True)
    subprocess.run(["git", "-C", str(catalog), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", "merge", "--no-ff", "-qm", "synthetic PR", "topic"], check=True)
    with pytest.raises(ValueError, match="base"):
        source_identity(catalog, expected_head=head, expected_base=original)


def test_subscription_cli_accepts_a_call_cap_without_a_fabricated_usd_cap():
    from dokimi.cli import parse_args

    args = parse_args(["ci", "run", ".", "--out-dir", "/path/to/result", "--max-calls", "18",
                       "--model", "claude-sonnet-4-6", "--provider", "claude-cli",
                       "--judge-model", "claude-opus-4-6", "--judge-provider", "claude-cli"])
    assert args.max_calls == 18 and args.max_usd is None


@pytest.mark.parametrize("regression", [False, True])
def test_cli_live_gate_and_record_follow_measured_answers(catalog: Path, tmp_path: Path, monkeypatch,
                                                         fake_llm, capsys, regression: bool):
    from dokimi.cli import parse_args
    from dokimi.metrics.usage import UsageRecord
    from dokimi.llm import Reply
    from dokimi.runner import ci_catalog
    from dokimi.runner.ci_source import revision

    cases = [{"id": f"case-{i}", "prompt": f"Question {i}", "expected_output": f"Correct answer {i}"}
             for i in range(3)]
    (catalog / "skills/dpnp-io/evals/evals.json").write_text(json.dumps({"skill_name": "dpnp-io", "evals": cases}))
    _commit(catalog, "fixed approved questions")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fixture-key")
    monkeypatch.setattr(ci_catalog, "list_cost_usd", lambda *_args: 0.001)
    fake_llm.usage = UsageRecord(cost_usd=0.001, n_calls=1, cost_known_calls=1)
    for arm in ("none", "approved", "candidate"):
        correct = arm == "approved" or (arm == "candidate" and not regression)
        for _ in cases:
            fake_llm.push(
                Reply(text="scripted answer", model_id="gpt-4o-mini", usage=fake_llm.usage),
                Reply(text="Yes" if correct else "No", model_id="gpt-4o", usage=fake_llm.usage),
                Reply(text="No" if correct else "Yes", model_id="gpt-4o", usage=fake_llm.usage))
    out = tmp_path / "receipt"
    args = parse_args(["ci", "run", str(catalog), "--out-dir", str(out), "--max-usd", "1",
                       "--model", "gpt-4o-mini", "--provider", "openai", "--judge-model", "gpt-4o",
                       "--judge-provider", "openai", "--reps", "1", "--subject", "skill/dpnp-io"])
    with pytest.raises(SystemExit) as measured:
        args.func(args)
    assert measured.value.code == int(regression)
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["status"] == ("fail" if regression else "pass")
    bundle = json.loads((out / "run.json").read_text())
    candidate_ids = {c["cell_id"] for c in bundle["plan"]["cells"] if c["arm"] == "candidate"}
    assert {o["score"] for o in bundle["observations"] if o["cell_id"] in candidate_ids} == {float(not regression)}
    store = tmp_path / "baseline"
    record = parse_args(["ci", "record", str(out / "run.json"), "--root", str(catalog), "--store", str(store),
                         "--expect-previous", "none", "--expected-head", revision(catalog)])
    with pytest.raises(SystemExit) as promoted:
        record.func(record)
    assert promoted.value.code == (2 if regression else 0)
    assert bool(list(store.glob("*/*/current.json"))) is (not regression)


def test_report_values_cannot_render_remote_images_or_links():
    import markdown

    from tests.ci_fixtures import ci_bundle
    from dokimi.runner.ci_io import render
    from dokimi.runner.ci_policy import DEFAULT_POLICY, gate_bundle

    bundle = ci_bundle()
    verdict = gate_bundle(bundle, DEFAULT_POLICY)
    bundle["identity"]["model"] = "model`\n![tracker](https://example.invalid/track)\n`"
    verdict["reason"] = "[forged authority](https://example.invalid/claim)"
    verdict["task_effects"][0]["task"] = "case | ![tracker](https://example.invalid/task)"
    html = markdown.markdown(render(bundle, verdict), extensions=["tables"])
    assert "<img" not in html and '<a href="https://example.invalid/' not in html


def test_paid_fork_with_older_history_imports_verified_base(catalog: Path, tmp_path: Path):
    import os

    import yaml

    from dokimi.cli import parse_args
    from dokimi.runner.ci_source import revision

    candidate = tmp_path / "candidate"
    subprocess.run(["git", "clone", "--no-local", "-q", str(catalog), str(candidate)], check=True)
    (candidate / "candidate-only").write_text("fork contribution")
    _commit(candidate, "fork head")
    head = revision(candidate)
    (catalog / "base-only").write_text("upstream advanced after the fork")
    _commit(catalog, "new upstream base")
    base = revision(catalog)
    missing = subprocess.run(["git", "-C", str(candidate), "cat-file", "-e", f"{base}^{{commit}}"],
                             capture_output=True, text=True)
    assert missing.returncode != 0
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/dokimi-pr.yml").read_text())
    step = next(s for s in workflow["jobs"]["run"]["steps"]
                if s.get("name") == "Import verified base object into candidate")
    env = {**os.environ, "CANDIDATE": str(candidate), "BASE_ROOT": str(catalog), "BASE_SHA": base}
    fetched = subprocess.run(["bash", "-euo", "pipefail", "-c", step["run"]],
                             env=env, capture_output=True, text=True)
    assert fetched.returncode == 0, fetched.stderr
    out = tmp_path / "receipt"
    args = parse_args(["ci", "check", str(candidate), "--out-dir", str(out), "--base-root", str(catalog),
                       "--changed-from", base, "--expected-head", head, "--expected-base", base,
                       "--source-repository", "vchamarthi/intel-skills"])
    with pytest.raises(SystemExit) as checked:
        args.func(args)
    assert checked.value.code == 0
    source = json.loads((out / "run.json").read_text())["source"]
    assert source["head_sha"] == head and source["base_sha"] == base
