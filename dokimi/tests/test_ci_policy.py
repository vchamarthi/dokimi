# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""CI decisions require complete, paired, attributable evidence."""

import copy
import math

import pytest

from ci_fixtures import ci_bundle
from dokimi.runner.ci_policy import DEFAULT_POLICY, gate_bundle, policy_digest
from dokimi.runner.plan import plan_digest


def test_measured_improvement_passes_and_reports_task_effects():
    result = gate_bundle(ci_bundle(), DEFAULT_POLICY)
    assert result["status"] == "pass"
    assert result["exit_code"] == 0
    assert result["promotion_eligible"] is True
    assert result["summary"]["n_tasks"] == 6
    assert result["task_effects"][0]["lift"] == pytest.approx(0.6)


@pytest.mark.parametrize(("status", "code"), [("invalid", 4), ("infra_error", 5),
                                                ("not_run", 3), ("inconclusive", 3)])
def test_cell_uncertainty_and_errors_never_pass(status, code):
    bundle = ci_bundle()
    bundle["result"]["cells"][0]["status"] = status
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert (result["status"], result["exit_code"]) == (status, code)


@pytest.mark.parametrize("side", ["result", "observations"])
def test_missing_evidence_cannot_pass(side):
    bundle = ci_bundle()
    cells = bundle[side]["cells"] if side == "result" else bundle[side]
    cells.pop()
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert result["status"] == "not_run"
    assert not result["promotion_eligible"]


@pytest.mark.parametrize("side", ["result", "observations", "plan"])
def test_duplicate_evidence_is_invalid(side):
    bundle = ci_bundle()
    cells = bundle[side]["cells"] if side != "observations" else bundle[side]
    cells.append(copy.deepcopy(cells[0]))
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert result["status"] == "invalid"


@pytest.mark.parametrize("side", ["result", "observations"])
def test_unplanned_evidence_is_invalid(side):
    bundle = ci_bundle()
    cells = bundle[side]["cells"] if side == "result" else bundle[side]
    cells[0]["cell_id"] = "unplanned"
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "invalid"


@pytest.mark.parametrize("score", [None, math.nan, math.inf, -0.01, 1.01, True])
def test_unusable_scores_are_blocking(score):
    bundle = ci_bundle()
    bundle["observations"][0]["score"] = score
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "invalid"


def test_budget_stop_blocks_a_complete_good_run():
    bundle = ci_bundle()
    bundle["budget_stopped"] = True
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "not_run"


def test_observed_regression_fails_without_hiding_behind_lift():
    result = gate_bundle(ci_bundle(scores={"approved": 1.0}), DEFAULT_POLICY)
    assert (result["status"], result["exit_code"]) == ("fail", 1)
    assert "regression" in result["reason"]


def test_no_observed_lift_fails():
    result = gate_bundle(ci_bundle(scores={"none": 0.8}), DEFAULT_POLICY)
    assert result["status"] == "fail"


def test_task_clustered_interval_does_not_count_repetitions_as_tasks():
    bundle = ci_bundle(tasks=2, reps=100)
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert result["status"] == "inconclusive"
    assert result["summary"]["n_tasks"] == 2


def test_uncertain_positive_lift_is_inconclusive():
    bundle = ci_bundle()
    for cell, obs in zip(bundle["plan"]["cells"], bundle["observations"], strict=True):
        if cell["arm"] == "none":
            obs["score"] = 1.0 if cell["task"] in {"task-0", "task-1", "task-2"} else 0.0
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert result["status"] == "inconclusive"
    assert result["summary"]["lift"]["lo"] <= 0


def test_static_pass_is_explicitly_ineligible_for_promotion():
    result = gate_bundle(ci_bundle(mode="static"), DEFAULT_POLICY)
    assert result["status"] == "pass"
    assert result["scope"] == "static"
    assert result["promotion_eligible"] is False


def test_static_failed_check_fails():
    bundle = ci_bundle(mode="static")
    bundle["result"]["cells"][0]["status"] = "fail"
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "fail"


def test_no_candidate_discovery_fails():
    bundle = ci_bundle()
    bundle["observations"][-1]["discovered"] = False
    policy = {**DEFAULT_POLICY, "require_discovery": True}
    bundle["policy_digest"] = policy_digest(policy)
    assert gate_bundle(bundle, policy)["status"] == "fail"


@pytest.mark.parametrize("key", ["content_digest", "suite_digest", "model", "harness", "grader_digest"])
def test_identity_mismatch_invalidates_evidence(key):
    bundle = ci_bundle()
    bundle["identity"][key] = "sha256:" + "b" * 64 if "digest" in key else "other"
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "invalid"


def test_resolved_model_must_match_identity():
    bundle = ci_bundle()
    bundle["result"]["cells"][0]["model"] = "different-model"
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "invalid"


def test_unpaired_plan_is_invalid_even_if_all_planned_cells_ran():
    bundle = ci_bundle()
    removed = bundle["plan"]["cells"].pop()
    bundle["result"]["cells"].pop()
    bundle["observations"].pop()
    bundle["plan"]["budget"]["cells"] -= 1
    bundle["result"]["plan_digest"] = plan_digest(bundle["plan"])
    assert removed["arm"] == "candidate"
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "invalid"


@pytest.mark.parametrize("policy", [{}, {**DEFAULT_POLICY, "min_tasks": 0}, {**DEFAULT_POLICY, "min_lift": math.nan}])
def test_malformed_policy_is_invalid(policy):
    assert gate_bundle(ci_bundle(), policy)["status"] == "invalid"


def test_initial_uplift_without_approved_arm_is_explicit():
    bundle = ci_bundle()
    keep = [i for i, cell in enumerate(bundle["plan"]["cells"]) if cell["arm"] != "approved"]
    bundle["plan"]["cells"] = [bundle["plan"]["cells"][i] for i in keep]
    bundle["result"]["cells"] = [bundle["result"]["cells"][i] for i in keep]
    bundle["observations"] = [bundle["observations"][i] for i in keep]
    bundle["plan"]["budget"]["cells"] = len(keep)
    bundle["result"]["plan_digest"] = plan_digest(bundle["plan"])
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert result["status"] == "pass"
    assert result["summary"]["comparison"] == "initial-uplift"
    assert result["summary"]["regression"]["n"] == 0


def test_lift_can_be_explicitly_disabled_without_disabling_regression():
    policy = {**DEFAULT_POLICY, "require_lift": False}
    bundle = ci_bundle(scores={"none": 0.9})
    bundle["policy_digest"] = policy_digest(policy)
    assert gate_bundle(bundle, policy)["status"] == "pass"
    bundle["observations"][-1]["score"] = 0
    bundle["observations"][-2]["score"] = 0
    assert gate_bundle(bundle, policy)["status"] == "fail"


@pytest.mark.parametrize("bundle", [None, [], "bad", 1])
def test_wrong_top_level_shape_returns_invalid(bundle):
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "invalid"


def test_policy_rebinding_is_required():
    bundle = ci_bundle()
    policy = {**DEFAULT_POLICY, "min_tasks": 5}
    result = gate_bundle(bundle, policy)
    assert result["status"] == "invalid"
    assert "policy digest" in result["reason"]


def test_capped_live_run_with_unknown_cost_is_infrastructure_error():
    bundle = ci_bundle()
    bundle["observations"][0]["cost_usd"] = None
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert result["status"] == "infra_error"
    assert result["exit_code"] == 5


def test_over_budget_run_cannot_pass_even_without_stop_marker():
    bundle = ci_bundle()
    bundle["observations"][0]["cost_usd"] = 100
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert result["status"] == "not_run"
    assert "budget" in result["reason"]


def test_exact_regression_boundary_does_not_fail_from_float_roundoff():
    bundle = ci_bundle(scores={"approved": 0.8, "candidate": 0.75})
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "pass"


@pytest.mark.parametrize(("status", "code"), [("infra_error", 5), ("not_run", 3), ("inconclusive", 3)])
def test_unscored_unmeasured_cells_keep_their_explicit_outcome(status, code):
    bundle = ci_bundle()
    bundle["result"]["cells"][0]["status"] = status
    bundle["observations"][0]["score"] = None
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert (result["status"], result["exit_code"]) == (status, code)


def subscription_bundle():
    bundle = ci_bundle()
    bundle["runtime"] = {"requested_model": "model-a", "provider": "claude-cli", "judge_model": "other",
                         "judge_provider": "claude-cli", "reps": 1, "billing": "subscription", "transport": "claude-cli"}
    bundle["plan"]["budget"]["cost_usd"] = None
    bundle["plan"]["budget"]["agent_calls"] = len(bundle["observations"]) * 2
    bundle["result"]["plan_digest"] = plan_digest(bundle["plan"])
    for observation in bundle["observations"]:
        observation["cost_usd"] = None
        observation["agent_calls"] = 2
    return bundle


def test_subscription_pass_uses_measured_call_cap_and_keeps_unknown_usd():
    bundle = subscription_bundle()
    result = gate_bundle(bundle, DEFAULT_POLICY)
    assert result["status"] == "pass"
    assert result["promotion_eligible"]
    assert all(o["cost_usd"] is None for o in bundle["observations"])


@pytest.mark.parametrize("missing", ["cap", "measurement", "provider", "transport"])
def test_subscription_unknown_cost_needs_complete_call_provenance(missing):
    bundle = subscription_bundle()
    if missing == "cap":
        bundle["plan"]["budget"]["agent_calls"] = None
        bundle["result"]["plan_digest"] = plan_digest(bundle["plan"])
    elif missing == "measurement":
        bundle["observations"][0].pop("agent_calls")
    elif missing == "provider":
        bundle["runtime"]["judge_provider"] = "openai"
    else:
        bundle["runtime"].pop("transport")
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] in {"invalid", "infra_error"}


def test_subscription_cannot_exceed_the_measured_call_cap():
    bundle = subscription_bundle()
    bundle["observations"][0]["agent_calls"] = 3
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "not_run"


def test_api_evidence_without_a_finite_cost_cap_cannot_promote():
    bundle = ci_bundle()
    bundle["plan"]["budget"]["cost_usd"] = None
    bundle["result"]["plan_digest"] = plan_digest(bundle["plan"])
    bundle["observations"][0]["cost_usd"] = None
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "infra_error"


def test_subscription_measured_cells_need_at_least_one_answer_call():
    bundle = subscription_bundle()
    bundle["observations"][0]["agent_calls"] = 0
    assert gate_bundle(bundle, DEFAULT_POLICY)["status"] == "infra_error"
