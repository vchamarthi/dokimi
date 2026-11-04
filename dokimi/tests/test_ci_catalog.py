# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Catalog runtime receipts preserve the approved suite and every paid call."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from dokimi import llm
from dokimi.metrics.usage import UsageRecord
from dokimi.runner import ci_catalog
from dokimi.runner.plan import completeness


def catalog(root: Path, *, body: str = "candidate instruction", cases: list | None = None,
            evaluated: bool = True) -> Path:
    skill = root / "skills" / "demo"
    (skill / "evals").mkdir(parents=True)
    (root / "skills.yaml").write_text("- name: demo\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Use when answering demo questions.\n---\n" + body,
        encoding="utf-8")
    default = [{"id": "q1", "prompt": "Explain the demo.", "expected_output": "A correct demo."}]
    (skill / "evals" / "evals.json").write_text(json.dumps({"skill_name": "demo", "evals": cases if cases is not None else default}))
    suite = root / "evaluation" / "harbor" / "suites.json"
    suite.parent.mkdir(parents=True)
    suite.write_text(json.dumps({"suites": [{"skill": "demo", "tasks": [], "evaluated_here": evaluated}]}))
    return root


def prepare(head: Path, base: Path | None = None, **kwargs):
    return ci_catalog.plan_catalog(head, base, model="gpt-4o-mini", provider="openai",
                                   judge_model="gpt-4o", judge_provider="openai", reps=1,
                                   harness="arms-runner", **kwargs)


@pytest.fixture
def live(monkeypatch, fake_llm):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setattr(ci_catalog, "list_cost_usd", lambda *_args: 0.001)
    fake_llm.usage = UsageRecord(cost_usd=0.01, n_calls=1, cost_known_calls=1)
    with reported_models(fake_llm):
        yield fake_llm


@contextmanager
def reported_models(fake):
    def respond(request):
        explicit = bool(fake.script) and isinstance(fake.script[0], llm.Reply)
        reply = fake(request)
        return reply if explicit else replace(reply, model_id=request.model)
    with llm.use_adapter(respond):
        yield


def test_catalog_primary_plan_uses_only_approved_cases(tmp_path):
    base = catalog(tmp_path / "base", body="approved instruction")
    cases = [{"id": "q1", "prompt": "Candidate easier wording", "expected_output": "Easy"},
             {"id": "q2", "prompt": "New question", "expected_output": "New answer"}]
    plan = prepare(catalog(tmp_path / "head", cases=cases), base)
    assert {c["arm"] for c in plan.plans[0]["cells"]} == {"none", "approved", "candidate"}
    assert {c["task"] for c in plan.plans[0]["cells"]} == {"skill/demo::q1"}
    assert plan.subjects[0].questions[0]["question"] == "Explain the demo."
    assert len(plan.exploratory) == 1
    assert plan.exploratory[0].questions[0]["id"] == "q2"


def test_new_skill_has_no_invented_approved_arm(tmp_path):
    plan = prepare(catalog(tmp_path / "head"))
    assert {c["arm"] for c in plan.plans[0]["cells"]} == {"none", "candidate"}


@pytest.mark.parametrize("evaluated,cases,reason", [(False, None, "checks_only"), (True, [], "no_tasks")])
def test_unmeasurable_skills_are_explicit_without_runtime_cells(tmp_path, evaluated, cases, reason):
    plan = prepare(catalog(tmp_path / "head", cases=cases, evaluated=evaluated))
    assert plan.plans == []
    assert plan.catalog["runtime"]["demo"]["status"] == reason


def test_live_catalog_scores_every_cell_with_answer_and_independent_judge(tmp_path, live):
    live.push("answer", "Yes", "No", "answer", "Yes", "No")
    prepared = prepare(catalog(tmp_path / "head"))
    out = ci_catalog.run_catalog(prepared, max_cost_usd=1.0)
    run = out["runs"][0]
    assert completeness(run["plan"], run["result"]).complete
    assert [o["score"] for o in run["observations"]] == [1.0, 1.0]
    assert [o["cost_usd"] for o in run["observations"]] == [0.03, 0.03]
    assert len(live.requests) == 6
    assert [r.model for r in live.requests] == ["gpt-4o-mini", "gpt-4o", "gpt-4o"] * 2
    assert "candidate instruction" in str(live.requests[3].messages)
    assert "candidate instruction" not in str(live.requests[0].messages)


def test_budget_stops_between_answer_and_first_judge(tmp_path, live):
    live.push("answer")
    out = ci_catalog.run_catalog(prepare(catalog(tmp_path / "head")), max_cost_usd=0.005)
    run = out["runs"][0]
    assert out["budget_stopped"] is True
    assert completeness(run["plan"], run["result"]).state == "incomplete"
    assert len(live.requests) == 1
    assert run["observations"][0]["cost_usd"] == 0.01
    assert run["observations"][0]["score"] is None


def test_unknown_usage_stops_before_next_call_and_is_not_free(tmp_path, live):
    live.usage = UsageRecord(cost_usd=None)
    live.push("answer")
    out = ci_catalog.run_catalog(prepare(catalog(tmp_path / "head")), max_cost_usd=1.0)
    assert out["budget_stopped"]
    assert out["runs"][0]["observations"][0]["cost_usd"] is None
    assert len(live.requests) == 1


def test_missing_credential_refused_before_calls(tmp_path, monkeypatch, fake_llm):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        ci_catalog.run_catalog(prepare(catalog(tmp_path / "head")), max_cost_usd=1.0)
    assert fake_llm.requests == []


def test_missing_base_questions_are_not_replaced_by_candidate_questions(tmp_path):
    base = catalog(tmp_path / "base", cases=[])
    out = prepare(catalog(tmp_path / "head"), base)
    assert out.plans == []
    assert out.catalog["runtime"]["demo"]["status"] == "no_tasks"


def test_bad_case_and_file_inputs_are_refused_instead_of_silently_dropped(tmp_path):
    root = catalog(tmp_path / "head", cases=[{"id": "q1", "prompt": "Where?", "files": ["private.txt"], "expected_output": "There"}])
    with pytest.raises(ValueError, match="files"):
        prepare(root)
