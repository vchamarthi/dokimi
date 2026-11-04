# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Persisted CI artifacts reject incompatible or internally contradictory envelopes."""

import copy

import pytest

from ci_fixtures import ci_bundle
from dokimi.artifacts import ArtifactValidationError, load_artifact, save_artifact, validate_artifact
from dokimi.runner.ci_policy import DEFAULT_POLICY, bundle_digest, gate_bundle


def test_ci_kinds_round_trip_with_nested_plan_validation(tmp_path):
    bundle = ci_bundle()
    payloads = {"ci_run": bundle, "ci_policy": DEFAULT_POLICY,
                "ci_gate": gate_bundle(bundle, DEFAULT_POLICY),
                "ci_baseline": {"baseline_id": bundle_digest(bundle), "bundle": bundle, "policy": DEFAULT_POLICY}}
    for kind, payload in payloads.items():
        path = tmp_path / f"{kind}.json"
        save_artifact(kind, payload, path)
        assert load_artifact(kind, path)["schema_version"] == kind + ".v1"
    broken = copy.deepcopy(bundle)
    broken["plan"]["cells"][0]["rep"] = 0
    with pytest.raises(ArtifactValidationError, match="rep"):
        validate_artifact("ci_run", broken)


@pytest.mark.parametrize("patch", [{"exit_code": 0}, {"promotion_eligible": True}])
def test_failed_gate_cannot_persist_success_claim(patch):
    verdict = gate_bundle(ci_bundle(scores={"candidate": 0}), DEFAULT_POLICY)
    verdict.update(patch)
    verdict["schema_version"] = "ci_gate.v1"
    with pytest.raises(ArtifactValidationError):
        validate_artifact("ci_gate", verdict)


def test_static_gate_cannot_persist_behavioral_promotion():
    verdict = gate_bundle(ci_bundle(mode="static"), DEFAULT_POLICY)
    verdict["schema_version"] = "ci_gate.v1"
    verdict["promotion_eligible"] = True
    with pytest.raises(ArtifactValidationError):
        validate_artifact("ci_gate", verdict)


def test_optional_runtime_receipt_is_strict_and_round_trips(tmp_path):
    bundle = ci_bundle()
    bundle["runtime"] = {"requested_model": "model-a", "provider": "fixture", "judge_model": "judge",
                         "judge_provider": "fixture", "reps": 1}
    path = tmp_path / "runtime.json"
    save_artifact("ci_run", bundle, path)
    assert load_artifact("ci_run", path)["runtime"]["reps"] == 1
    bundle["runtime"]["secret"] = "forbidden"
    with pytest.raises(ArtifactValidationError):
        validate_artifact("ci_run", bundle)


def test_ci_receipts_are_discoverable_with_their_subject_and_model(tmp_path):
    from dokimi.runs import scan
    from dokimi.runs.show import show
    bundle = ci_bundle()
    save_artifact("ci_run", bundle, tmp_path / "run.json")
    save_artifact("ci_gate", gate_bundle(bundle, DEFAULT_POLICY), tmp_path / "gate.json")
    save_artifact("ci_policy", DEFAULT_POLICY, tmp_path / "policy.json")
    save_artifact("ci_baseline", {"baseline_id": bundle_digest(bundle), "bundle": bundle, "policy": DEFAULT_POLICY},
                  tmp_path / "baseline.json")
    artifacts = {artifact.kind: artifact for artifact in scan(tmp_path).artifacts}
    assert (artifacts["ci_run"].subject, artifacts["ci_run"].model) == ("demo", "model-a")
    assert (artifacts["ci_baseline"].subject, artifacts["ci_baseline"].model) == ("demo", "model-a")
    assert set(artifacts) == {"ci_run", "ci_gate", "ci_policy", "ci_baseline"}
    assert show(tmp_path, "run.json").contents == {"observations": 18}
    assert show(tmp_path, "gate.json").contents == {"reasons": 1, "task_effects": 6}
