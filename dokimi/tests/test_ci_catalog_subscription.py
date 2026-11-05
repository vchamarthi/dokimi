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
        installed=True, authenticated=True, method="claude.ai (firstParty)", version="2.1.306 (Claude Code)"))
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


def auth_status_probe(monkeypatch, status):
    import json

    from dokimi import process

    detect = ci_catalog.llm_cli.detect
    commands = []
    def probe(argv, **_kwargs):
        commands.append(list(argv))
        output = "2.1.306 (Claude Code)" if argv[-1] == "--version" else json.dumps(status)
        return process.Completed(tuple(argv), 0, stdout_bytes=output.encode())
    monkeypatch.setattr(ci_catalog.llm_cli, "detect", lambda provider: detect(provider, runner=probe))
    return commands


@pytest.mark.parametrize("method", ["claude.ai", "oauth_token"])
def test_native_first_party_auth_status_reaches_a_complete_subscription_run(tmp_path, monkeypatch, fake_llm, method):
    commands = auth_status_probe(monkeypatch, {
        "loggedIn": True, "authMethod": method, "apiProvider": "firstParty"})
    fake_llm.usage = UsageRecord(cost_usd=None, n_calls=1)
    fake_llm.push(*(["answer", "Yes", "No"] * 2))
    with reported_models(fake_llm):
        out = ci_catalog.run_catalog(subscription(catalog(tmp_path / "head")), max_calls=6)
    run = out["runs"][0]
    assert completeness(run["plan"], run["result"]).complete
    assert run["runtime"]["billing"] == "subscription"
    assert [row["cost_usd"] for row in run["observations"]] == [None, None]
    assert sum(row["agent_calls"] for row in run["observations"]) == 6
    assert commands == [["claude", "--version"], ["claude", "--setting-sources", "", "auth", "status"]]


@pytest.mark.parametrize("method,provider,logged_in", [
    ("api_key", "firstParty", True),
    ("oauth_token", "gateway", True),
    ("oauth_token", "bedrock", True),
    ("claude.ai", "gateway", True),
    ("unknown", "firstParty", True),
    ("oauth_token", None, True),
    ("claude.ai", None, True),
    ("oauth_token", "firstParty", False),
])
def test_non_native_or_unverified_auth_status_is_rejected_before_model_calls(
        tmp_path, monkeypatch, fake_llm, method, provider, logged_in):
    auth_status_probe(monkeypatch, {"loggedIn": logged_in, "authMethod": method, "apiProvider": provider})
    with pytest.raises(ValueError, match="subscription login"):
        ci_catalog.run_catalog(subscription(catalog(tmp_path / "head")), max_calls=6)
    assert fake_llm.requests == []


def test_subscription_pins_observed_cli_version_before_the_first_model_call(tmp_path, signed_in):
    prepared = subscription(catalog(tmp_path / "head"))
    before = prepared.plans[0]["grader"]["digest"]
    observed = []
    signed_in.push(*(["answer", "Yes", "No"] * 2))
    out = ci_catalog.run_catalog(prepared, max_calls=6,
                                on_usage=lambda _usage: observed.append(prepared.plans[0]["grader"]["digest"]))
    run = out["runs"][0]
    assert observed and all(digest != before for digest in observed)
    assert run["runtime"]["cli_version"] == "2.1.306 (Claude Code)"
    assert run["plan"]["grader"]["digest"] == run["identity"]["grader_digest"]


@pytest.mark.parametrize("version", [None, "", "unknown", "not-a-version"])
def test_subscription_unknown_cli_version_is_rejected_before_calls(tmp_path, monkeypatch, fake_llm, version):
    monkeypatch.setattr(ci_catalog.llm_cli, "detect", lambda *_args: SimpleNamespace(
        installed=True, authenticated=True, method="oauth_token (firstParty)", version=version))
    with pytest.raises(ValueError, match="known.*CLI version"):
        ci_catalog.run_catalog(subscription(catalog(tmp_path / "head")), max_calls=6)
    assert fake_llm.requests == []


def test_changed_cli_version_requires_explicit_revalidation_for_baseline_cas(tmp_path, monkeypatch, fake_llm):
    from dokimi.runner.ci_pipeline import bind_run
    from dokimi.runner.ci_policy import DEFAULT_POLICY
    from dokimi.runner.ci_store import load_baseline, record_bundle

    root = catalog(tmp_path / "head", cases=[
        {"id": f"q{i}", "prompt": f"Question {i}", "expected_output": f"Correct {i}"} for i in range(3)])
    fake_llm.usage = UsageRecord(cost_usd=None, n_calls=1)
    runs = []
    for version in ("2.1.306 (Claude Code)", "2.1.307 (Claude Code)"):
        monkeypatch.setattr(ci_catalog.llm_cli, "detect", lambda *_args, cli_version=version: SimpleNamespace(
            installed=True, authenticated=True, method="oauth_token (firstParty)", version=cli_version))
        fake_llm.push(*(["answer", "No", "Yes"] * 3 + ["answer", "Yes", "No"] * 3))
        with reported_models(fake_llm):
            parts = ci_catalog.run_catalog(subscription(root), max_calls=18)["runs"][0]
        runs.append(bind_run(parts, {"repository": "example/catalog", "head_sha": "a" * 40,
                                     "base_sha": "a" * 40}, DEFAULT_POLICY, mode="live"))
    first, second = runs
    assert first["identity"]["grader_digest"] != second["identity"]["grader_digest"]
    assert first["identity"]["framework_digest"] == second["identity"]["framework_digest"]
    def no_probe(*_args):
        raise AssertionError("record verification must not require the CLI to be installed or authenticated")
    monkeypatch.setattr(ci_catalog.llm_cli, "detect", no_probe)
    ci_catalog.verify_record_identity(root, first)
    ci_catalog.verify_record_identity(root, first, content_only=True)
    store = tmp_path / "store"
    saved = record_bundle(first, DEFAULT_POLICY, store)
    with pytest.raises(ValueError, match="identity drift.*revalidation"):
        record_bundle(second, DEFAULT_POLICY, store, expected_previous=saved["baseline_id"])
    assert load_baseline(store, "skill/demo")["bundle"]["runtime"]["cli_version"] == "2.1.306 (Claude Code)"
    replaced = record_bundle(second, DEFAULT_POLICY, store, expected_previous=saved["baseline_id"],
                             allow_revalidation=True)
    assert replaced["revalidated_from"] == saved["baseline_id"]
    assert load_baseline(store, "skill/demo")["bundle"]["runtime"]["cli_version"] == "2.1.307 (Claude Code)"


def test_reusing_a_prepared_subscription_plan_keeps_the_version_digest_stable(tmp_path, signed_in):
    prepared = subscription(catalog(tmp_path / "head"))
    signed_in.push(*(["answer", "Yes", "No"] * 4))
    first = ci_catalog.run_catalog(prepared, max_calls=6)["runs"][0]
    fingerprint = first["identity"]["grader_digest"]
    plan_hash = first["result"]["plan_digest"]
    second = ci_catalog.run_catalog(prepared, max_calls=6)["runs"][0]
    assert second["identity"]["grader_digest"] == fingerprint
    assert second["result"]["plan_digest"] == plan_hash
    assert completeness(second["plan"], second["result"]).complete


def test_reusing_a_prepared_plan_after_cli_update_requires_fresh_planning(tmp_path, signed_in, monkeypatch):
    prepared = subscription(catalog(tmp_path / "head"))
    signed_in.push(*(["answer", "Yes", "No"] * 2))
    ci_catalog.run_catalog(prepared, max_calls=6)
    monkeypatch.setattr(ci_catalog.llm_cli, "detect", lambda *_args: SimpleNamespace(
        installed=True, authenticated=True, method="oauth_token (firstParty)", version="2.1.307 (Claude Code)"))
    with pytest.raises(ValueError, match="CLI version changed.*fresh plan"):
        ci_catalog.run_catalog(prepared, max_calls=6)
    assert len(signed_in.requests) == 6
