# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed CI decisions over complete, paired run bundles."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from typing import Any

from dokimi.artifacts import ArtifactValidationError, validate_artifact
from dokimi.eval.stats import bootstrap_ci
from dokimi.runner.plan import completeness

DEFAULT_POLICY: dict[str, Any] = {
    "schema_version": "ci_policy.v1", "min_tasks": 3, "max_regression": 0.05,
    "min_lift": 0.0, "require_lift": True, "require_discovery": False,
}
EXIT_CODES = {"pass": 0, "fail": 1, "inconclusive": 3, "invalid": 4, "infra_error": 5, "not_run": 3}


def bundle_digest(bundle: dict[str, Any]) -> str:
    """Hash canonical JSON, refusing nonfinite numbers rather than hashing invalid JSON."""
    payload = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def policy_digest(policy: dict[str, Any]) -> str:
    """Digest the explicit, versioned policy without injecting implicit defaults."""
    return bundle_digest(policy)


def _verdict(status: str, scope: str, reasons: list[str], **details: Any) -> dict[str, Any]:
    return {"status": status, "exit_code": EXIT_CODES[status], "scope": scope,
            "reason": "; ".join(reasons), "reasons": reasons,
            "promotion_eligible": status == "pass" and scope == "behavioral",
            "task_effects": [], "summary": {}, **details}


def _validated(bundle: dict[str, Any], policy: dict[str, Any]) -> None:
    if not isinstance(bundle, dict) or not isinstance(policy, dict):
        raise ValueError("CI bundle and policy must be JSON objects")
    validate_artifact("ci_policy", policy)
    validate_artifact("ci_run", bundle)
    bundle_digest(bundle)
    if bundle["policy_digest"] != policy_digest(policy):
        raise ValueError("policy digest differs from the policy that produced the evidence")


def _identity_problems(bundle: dict[str, Any]) -> list[str]:
    identity, plan = bundle["identity"], bundle["plan"]
    expected = {
        "subjects": [{"id": identity["subject"], "kind": identity["kind"],
                      "sha": bundle["source"]["head_sha"], "fingerprint": identity["content_digest"]}],
        "models": [identity["model"]], "harnesses": [identity["harness"]],
    }
    problems = [f"identity does not match plan {key}" for key, value in expected.items() if plan[key] != value]
    if len(plan["suites"]) != 1 or plan["suites"][0]["hash"] != identity["suite_digest"]:
        problems.append("identity does not match suite digest")
    if plan["grader"]["digest"] != identity["grader_digest"]:
        problems.append("identity does not match grader digest")
    if bundle["mode"] == "live":
        problems.extend(_model_problems(bundle))
    return problems


def _model_problems(bundle: dict[str, Any]) -> list[str]:
    model = bundle["identity"]["model"]
    problems = []
    if model.lower() in {"unknown", "unresolved", "none", "static"}:
        problems.append("live evidence requires a known resolved model")
    if any(c["model"] != model for c in bundle["plan"]["cells"]):
        problems.append("planned cell model differs from identity")
    measured = {"pass", "fail", "inconclusive"}
    if any(c.get("model") != model for c in bundle["result"]["cells"] if c["status"] in measured):
        problems.append("resolved cell model differs from identity or is missing")
    return problems


def _observation_problems(bundle: dict[str, Any]) -> tuple[list[str], list[str]]:
    planned = {c["cell_id"] for c in bundle["plan"]["cells"]}
    observed = Counter(c["cell_id"] for c in bundle["observations"])
    problems = []
    if any(count != 1 for count in observed.values()):
        problems.append("duplicate observations")
    if set(observed) - planned:
        problems.append("unplanned observations")
    if _unscored_measured(bundle):
        problems.append("observation has no measured score")
    return problems, sorted(planned - set(observed))


def _unscored_measured(bundle: dict[str, Any]) -> bool:
    statuses = {c["cell_id"]: c["status"] for c in bundle["result"]["cells"]}
    return any(c["score"] is None and statuses.get(c["cell_id"]) in {"pass", "fail"}
               for c in bundle["observations"])


def _evidence_status(bundle: dict[str, Any], scope: str) -> dict[str, Any] | None:
    check = completeness(bundle["plan"], bundle["result"])
    problems, missing_obs = _observation_problems(bundle)
    problems.extend(check.problems)
    problems.extend(_identity_problems(bundle))
    if problems:
        return _verdict("invalid", scope, problems)
    statuses = check.statuses
    for status in ("invalid", "infra_error"):
        if statuses.get(status):
            return _verdict(status, scope, [f"a planned cell returned {status}"])
    if bundle["budget_stopped"] or not check.complete or missing_obs:
        return _verdict("not_run", scope, ["planned evidence is missing or the run stopped at its budget"])
    if statuses.get("inconclusive"):
        return _verdict("inconclusive", scope, ["a planned cell has an inconclusive measurement"])
    return None


def _budget_status(bundle: dict[str, Any], scope: str) -> dict[str, Any] | None:
    if scope != "static" and bundle.get("runtime", {}).get("billing") == "subscription":
        return _subscription_budget(bundle, scope)
    cap = bundle["plan"]["budget"].get("cost_usd")
    if scope == "static":
        return None
    if cap is None or cap <= 0:
        return _verdict("infra_error", scope, ["live API evidence requires an explicit positive finite USD cap"])
    costs = [c["cost_usd"] for c in bundle["observations"]]
    if any(cost is None for cost in costs):
        return _verdict("infra_error", scope, ["capped live evidence has unknown measured cost"])
    if sum(costs) >= cap:
        return _verdict("not_run", scope, ["measured cost reached or exceeded the declared budget"])
    return None


def _subscription_budget(bundle: dict[str, Any], scope: str) -> dict[str, Any] | None:
    cap = bundle["plan"]["budget"].get("agent_calls")
    calls = [o.get("agent_calls") for o in bundle["observations"]]
    if not isinstance(cap, int) or cap <= 0 or any(c is None or c < 1 for c in calls):
        return _verdict("infra_error", scope, ["subscription evidence needs an explicit call cap and measured calls"])
    if sum(calls) > cap:
        return _verdict("not_run", scope, ["subscription measured calls exceeded the declared budget"])
    return None


def _paired_tasks(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    observations = {c["cell_id"]: c for c in bundle["observations"]}
    tasks: dict[str, dict[int, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for cell in bundle["plan"]["cells"]:
        tasks[cell["task"]][cell["rep"]][cell["arm"]] = observations[cell["cell_id"]]["score"]
    arms = {cell["arm"] for cell in bundle["plan"]["cells"]}
    if not {"none", "candidate"} <= arms or not arms <= {"none", "approved", "candidate"}:
        raise ValueError("live plan requires none/candidate arms and optional approved")
    effects = []
    for task, repetitions in sorted(tasks.items()):
        if any(set(values) != arms for values in repetitions.values()):
            raise ValueError(f"unpaired arms for task {task}")
        means = {arm: sum(v[arm] for v in repetitions.values()) / len(repetitions) for arm in arms}
        effects.append({"task": task, "reps": len(repetitions), "scores": means,
                        "lift": means["candidate"] - means["none"],
                        "regression": means["candidate"] - means["approved"] if "approved" in arms else None})
    return effects


def _behavioral_decision(bundle: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    effects = _paired_tasks(bundle)
    lifts = [e["lift"] for e in effects]
    regressions = [e["regression"] for e in effects if e["regression"] is not None]
    summary: dict[str, Any] = {"n_tasks": len(effects), "comparison": "candidate-vs-approved" if regressions else "initial-uplift",
               "lift": bootstrap_ci(lifts, ndigits=None),
               "regression": bootstrap_ci(regressions, ndigits=None),
               "uncertainty_unit": "task", "confidence": 0.95}
    failures = _behavioral_failures(bundle, policy, summary)
    if failures:
        return _verdict("fail", "behavioral", failures, task_effects=effects, summary=summary)
    if len(effects) < policy["min_tasks"]:
        return _verdict("inconclusive", "behavioral", ["too few independent tasks"], task_effects=effects, summary=summary)
    if policy["require_lift"] and summary["lift"]["lo"] <= policy["min_lift"]:
        return _verdict("inconclusive", "behavioral", ["task-clustered lift interval does not clear minimum lift"],
                        task_effects=effects, summary=summary)
    return _verdict("pass", "behavioral", ["complete paired evidence satisfies policy"], task_effects=effects, summary=summary)


def _behavioral_failures(bundle: dict[str, Any], policy: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    reasons = []
    regression = summary["regression"]["mean"]
    if regression is not None and regression < -policy["max_regression"] and not math.isclose(
            regression, -policy["max_regression"], rel_tol=0.0, abs_tol=1e-12):
        reasons.append("observed candidate regression exceeds policy")
    if policy["require_lift"] and summary["lift"]["mean"] <= policy["min_lift"]:
        reasons.append("observed candidate lift does not exceed policy")
    candidates = {c["cell_id"] for c in bundle["plan"]["cells"] if c["arm"] == "candidate"}
    if policy["require_discovery"] and any(o["discovered"] is not True for o in bundle["observations"]
                                            if o["cell_id"] in candidates):
        reasons.append("candidate discovery was not demonstrated for every planned cell")
    return reasons


def gate_bundle(bundle: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Return one of six outcomes; malformed or incomplete evidence cannot pass."""
    scope = "static" if isinstance(bundle, dict) and bundle.get("mode") == "static" else "behavioral"
    try:
        _validated(bundle, policy)
        blocked = _evidence_status(bundle, scope) or _budget_status(bundle, scope)
        if blocked is not None:
            return blocked
        if scope == "static":
            status = "fail" if any(c["status"] == "fail" for c in bundle["result"]["cells"]) else "pass"
            return _verdict(status, scope, [f"static checks {status}; no behavioral promotion"])
        return _behavioral_decision(bundle, policy)
    except (ArtifactValidationError, ValueError, TypeError, KeyError) as exc:
        return _verdict("invalid", scope, [str(exc)])
