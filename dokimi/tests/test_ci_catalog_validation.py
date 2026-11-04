# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Bad catalog inputs and verifier/telemetry loss never become successful cells."""

from __future__ import annotations

import json

import pytest

from dokimi.llm import Reply
from dokimi.metrics.usage import UsageRecord
from dokimi.runner import ci_catalog
from dokimi.runner.plan import completeness
from tests.test_ci_catalog import catalog, live, prepare

__all__ = ["live"]


@pytest.mark.parametrize("cases", [
    [{"id": "q1", "prompt": "No grading"}],
    [{"id": "q1", "prompt": "Valid", "expected_output": "Yes"},
     {"id": "q1", "prompt": "Duplicate", "expected_output": "Yes"}],
    [{"id": "q1", "prompt": "", "expected_output": "Yes"}],
])
def test_malformed_evals_cannot_shrink_the_promised_suite(tmp_path, cases):
    with pytest.raises(ValueError):
        prepare(catalog(tmp_path / "head", cases=cases))


def test_literal_checks_are_honored_alongside_semantic_expected_output(tmp_path, live):
    cases = [{"id": "q1", "prompt": "Explain", "expected_output": "Correct explanation",
              "checks": {"must_include": ["required()"], "must_not_include": ["forbidden()"]}}]
    live.push("required() forbidden()", "Yes", "No", "required()", "Yes", "No")
    out = ci_catalog.run_catalog(prepare(catalog(tmp_path / "head", cases=cases)), max_cost_usd=1)
    run = out["runs"][0]
    assert [o["score"] for o in run["observations"]] == [pytest.approx(2 / 3), 1]
    assert [c["status"] for c in run["result"]["cells"]] == ["fail", "pass"]


def test_transport_error_is_missing_evidence_and_stops_further_paid_calls(tmp_path, live):
    live.push(RuntimeError("provider is unavailable"))
    out = ci_catalog.run_catalog(prepare(catalog(tmp_path / "head")), max_cost_usd=1)
    assert completeness(out["runs"][0]["plan"], out["runs"][0]["result"]).state == "incomplete"
    assert out["runs"][0]["observations"][0]["score"] is None
    assert len(live.requests) == 1


def test_truncated_answer_does_not_get_graded(tmp_path, live):
    live.push(Reply("unfinished", UsageRecord(cost_usd=.01), finish_reason="length"))
    out = ci_catalog.run_catalog(prepare(catalog(tmp_path / "head")), max_cost_usd=1)
    assert out["runs"][0]["result"]["cells"][0]["status"] == "infra_error"
    assert len(live.requests) == 1


def test_unpriced_requested_model_refused_before_provider_call(tmp_path, live, monkeypatch):
    monkeypatch.setattr(ci_catalog, "list_cost_usd", lambda *_args: None)
    with pytest.raises(ValueError, match="unpriced"):
        ci_catalog.run_catalog(prepare(catalog(tmp_path / "head")), max_cost_usd=1)
    assert live.requests == []


def test_judge_must_be_different_model(tmp_path, live):
    prepared = prepare(catalog(tmp_path / "head"))
    prepared.judge_model = prepared.model
    with pytest.raises(ValueError, match="independent"):
        ci_catalog.run_catalog(prepared, max_cost_usd=1)
    assert live.requests == []


def test_head_new_eval_results_are_separate_and_share_the_same_cap(tmp_path, live):
    base = catalog(tmp_path / "base")
    cases = [{"id": "q1", "prompt": "Changed", "expected_output": "Changed"},
             {"id": "q2", "prompt": "Added", "expected_output": "Added"}]
    live.push(*(["answer", "Yes", "No"] * 4))
    prepared = prepare(catalog(tmp_path / "head", cases=cases), base)
    out = ci_catalog.run_catalog(prepared, max_cost_usd=.095)
    assert len(out["runs"][0]["observations"]) == 3
    assert {c["task"] for c in out["runs"][0]["plan"]["cells"]} == {"skill/demo::q1"}
    assert {c["task"] for c in out["exploratory_runs"][0]["plan"]["cells"]} == {"skill/demo::q2"}
    assert len(live.requests) == 10
    assert out["budget_stopped"]


def test_a_linked_eval_file_is_refused_before_import(tmp_path):
    root = catalog(tmp_path / "head")
    target = tmp_path / "external.json"
    target.write_text(json.dumps({"evals": []}))
    path = root / "skills" / "demo" / "evals" / "evals.json"
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ValueError, match="static checks failed"):
        prepare(root)


def test_explicit_config_limits_subjects_and_refuses_unsupported_suite_layout(tmp_path):
    from dokimi.repo_config import RepoConfig
    root = catalog(tmp_path / "head")
    config = RepoConfig(root / "config.yaml", root, {"schema_version": "dokimi_config.v1", "subjects": []})
    with pytest.raises(ValueError, match="configured"):
        prepare(root, config=config, subjects=["skill/demo"])
    config = RepoConfig(root / "config.yaml", root, {"schema_version": "dokimi_config.v1", "subjects": [
        {"id": "skill/demo", "path": "skills/demo", "suite": "custom/questions.json"}]})
    with pytest.raises(ValueError, match="unsupported.*suite"):
        prepare(root, config=config)


def test_record_identity_recomputes_the_current_content_and_suite(tmp_path, live):
    root = catalog(tmp_path / "head")
    live.push(*(["answer", "Yes", "No"] * 2))
    run = ci_catalog.run_catalog(prepare(root), max_cost_usd=1)["runs"][0]
    ci_catalog.verify_record_identity(root, run)
    path = root / "skills/demo/evals/evals.json"
    data = json.loads(path.read_text())
    data["evals"][0]["expected_output"] = "A revised grading standard"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="identity.*changed"):
        ci_catalog.verify_record_identity(root, run)


def test_revalidation_can_refresh_framework_but_cannot_change_suite(tmp_path, live):
    root = catalog(tmp_path / "head")
    live.push(*(["answer", "Yes", "No"] * 2))
    run = ci_catalog.run_catalog(prepare(root), max_cost_usd=1)["runs"][0]
    run["identity"]["framework_digest"] = "sha256:" + "0" * 64
    ci_catalog.verify_record_identity(root, run, content_only=True)
    run["identity"]["suite_digest"] = "sha256:" + "1" * 64
    with pytest.raises(ValueError, match="identity.*changed"):
        ci_catalog.verify_record_identity(root, run, content_only=True)


@pytest.mark.parametrize("bad_cost", [float("nan"), float("inf"), -1.0])
def test_invalid_provider_cost_is_unknown_and_stops_safely(tmp_path, live, bad_cost):
    live.usage = UsageRecord(cost_usd=bad_cost)
    live.push("answer")
    out = ci_catalog.run_catalog(prepare(catalog(tmp_path / "head")), max_cost_usd=1)
    assert out["budget_stopped"]
    assert out["runs"][0]["observations"][0]["cost_usd"] is None
    assert len(live.requests) == 1


def test_configured_native_suite_cannot_silently_run_unconfigured_prompt_evals(tmp_path):
    from dokimi.repo_config import RepoConfig
    root = catalog(tmp_path / "head")
    config = RepoConfig(root / "config.yaml", root, {"schema_version": "dokimi_config.v1", "subjects": [
        {"id": "skill/demo", "path": "skills/demo", "suite": "evaluation/harbor/tasks/**"}]})
    with pytest.raises(ValueError, match="does not configure.*arms-runner"):
        prepare(root, config=config)
