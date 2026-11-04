# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Subscription-only trials use CLI calls and a call cap, preserving unknown billed USD."""

from types import SimpleNamespace

import pytest

from dokimi.metrics.usage import UsageRecord
from dokimi.runner import ci_catalog
from dokimi.runner.plan import completeness
from tests.test_ci_catalog import catalog, reported_models


def subscription(root):
    return ci_catalog.plan_catalog(root, model="claude-sonnet-5-5", provider="claude-cli",
                                   judge_model="claude-haiku-5-5", judge_provider="claude-cli",
                                   reps=1, harness="arms-runner")


@pytest.fixture
def signed_in(monkeypatch, fake_llm):
    monkeypatch.setattr(ci_catalog.llm_cli, "detect", lambda *_args: SimpleNamespace(
        installed=True, authenticated=True, method="claude.ai (firstParty)"))
    fake_llm.usage = UsageRecord(cost_usd=None, n_calls=1)
    with reported_models(fake_llm):
        yield fake_llm


def test_subscription_completes_with_null_cost_and_explicit_call_counts(tmp_path, signed_in):
    signed_in.push(*(["answer", "Yes", "No"] * 2))
    out = ci_catalog.run_catalog(subscription(catalog(tmp_path / "head")), max_calls=6)
    run = out["runs"][0]
    assert completeness(run["plan"], run["result"]).complete
    assert out["budget_stopped"] is False
    assert run["runtime"]["billing"] == "subscription"
    assert run["runtime"]["transport"] == "claude-cli"
    assert run["plan"]["budget"]["agent_calls"] == 6
    assert run["plan"]["budget"]["cost_usd"] is None
    assert [o["cost_usd"] for o in run["observations"]] == [None, None]
    assert [o["agent_calls"] for o in run["observations"]] == [3, 3]


def test_subscription_cap_stops_the_next_cli_call_without_inventing_a_grade(tmp_path, signed_in):
    signed_in.push("answer")
    out = ci_catalog.run_catalog(subscription(catalog(tmp_path / "head")), max_calls=1)
    assert out["budget_stopped"]
    assert len(signed_in.requests) == 1
    assert out["runs"][0]["observations"][0]["score"] is None
    assert completeness(out["runs"][0]["plan"], out["runs"][0]["result"]).state == "incomplete"


def test_subscription_requires_its_own_finite_call_cap(tmp_path, signed_in):
    with pytest.raises(ValueError, match="max_calls"):
        ci_catalog.run_catalog(subscription(catalog(tmp_path / "head")), max_cost_usd=1)
    assert signed_in.requests == []


def test_subscription_refuses_mixed_api_judge_before_any_call(tmp_path, signed_in):
    prepared = subscription(catalog(tmp_path / "head"))
    prepared.judge_provider = "anthropic"
    with pytest.raises(ValueError, match="both.*claude-cli"):
        ci_catalog.run_catalog(prepared, max_calls=6)
    assert signed_in.requests == []


def test_cli_api_auth_is_not_subscription_provenance(tmp_path, signed_in, monkeypatch):
    monkeypatch.setattr(ci_catalog.llm_cli, "detect", lambda *_args: SimpleNamespace(
        installed=True, authenticated=True, method="api_key (firstParty)"))
    with pytest.raises(ValueError, match="subscription login"):
        ci_catalog.run_catalog(subscription(catalog(tmp_path / "head")), max_calls=6)
    assert signed_in.requests == []


def test_judge_resolving_to_the_answer_model_is_rejected(tmp_path, signed_in):
    from dokimi.llm import Reply
    signed_in.push(Reply("answer", UsageRecord(cost_usd=None), model_id="claude-sonnet-5-5"),
                   Reply("Yes", UsageRecord(cost_usd=None), model_id="claude-sonnet-5-5"))
    out = ci_catalog.run_catalog(subscription(catalog(tmp_path / "head")), max_calls=6)
    run = out["runs"][0]
    assert run["result"]["cells"][0]["status"] == "infra_error"
    assert run["observations"][0]["score"] is None
    assert len(signed_in.requests) == 2


def test_judge_with_missing_model_provenance_is_rejected(tmp_path, signed_in):
    from dokimi.llm import Reply
    signed_in.push(Reply("answer", UsageRecord(cost_usd=None), model_id="claude-sonnet-5-5"),
                   Reply("Yes", UsageRecord(cost_usd=None)))
    out = ci_catalog.run_catalog(subscription(catalog(tmp_path / "head")), max_calls=6)
    assert out["runs"][0]["result"]["cells"][0]["status"] == "infra_error"
    assert len(signed_in.requests) == 2
