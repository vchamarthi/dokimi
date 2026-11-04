# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Baseline promotion uses real atomic files, identity checks, and compare-and-swap."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ci_fixtures import ci_bundle
from dokimi.runner.ci_policy import DEFAULT_POLICY
from dokimi.runner.ci_store import load_baseline, record_bundle
from dokimi.runner.plan import plan_digest


def changed_bundle(original, *, model=None, subject=None):
    bundle = copy.deepcopy(original)
    bundle["source"]["head_sha"] = "3" * 40
    bundle["plan"]["subjects"][0]["sha"] = "3" * 40
    if model:
        from dokimi.runner.plan import cell_id
        bundle["identity"]["model"] = model
        bundle["plan"]["models"] = [model]
        for c, r, o in zip(bundle["plan"]["cells"], bundle["result"]["cells"], bundle["observations"], strict=True):
            c["model"] = r["model"] = model
            c["cell_id"] = r["cell_id"] = o["cell_id"] = cell_id(c["arm"], model, c["task"], c["rep"])
    if subject:
        bundle["identity"]["subject"] = subject
        bundle["plan"]["subjects"][0]["id"] = subject
    bundle["result"]["plan_digest"] = plan_digest(bundle["plan"])
    return bundle


def test_create_and_compare_and_swap_preserve_validated_evidence(tmp_path):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    assert load_baseline(tmp_path, "demo")["baseline_id"] == first["baseline_id"]
    next_bundle = changed_bundle(ci_bundle())
    next_bundle["identity"]["content_digest"] = "sha256:" + "b" * 64
    next_bundle["plan"]["subjects"][0]["fingerprint"] = next_bundle["identity"]["content_digest"]
    next_bundle["result"]["plan_digest"] = plan_digest(next_bundle["plan"])
    second = record_bundle(next_bundle, DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"])
    assert second["baseline_id"] != first["baseline_id"]
    assert load_baseline(tmp_path, "demo")["bundle"]["identity"]["content_digest"].endswith("b" * 64)


def test_create_only_does_not_overwrite_existing_baseline(tmp_path):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    with pytest.raises(ValueError, match="previous|exists|compare"):
        record_bundle(changed_bundle(ci_bundle()), DEFAULT_POLICY, tmp_path, expected_previous=None)
    assert load_baseline(tmp_path, "demo")["baseline_id"] == first["baseline_id"]


def test_wrong_previous_id_preserves_prior_baseline(tmp_path):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    with pytest.raises(ValueError, match="previous|compare"):
        record_bundle(changed_bundle(ci_bundle()), DEFAULT_POLICY, tmp_path, expected_previous="sha256:" + "0" * 64)
    assert load_baseline(tmp_path, "demo")["baseline_id"] == first["baseline_id"]


@pytest.mark.parametrize("mode", ["static", "failed", "partial", "wrong-policy"])
def test_unpromotable_evidence_cannot_replace_baseline(tmp_path, mode):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    bundle = ci_bundle(mode="static" if mode == "static" else "live")
    if mode == "failed":
        for cell, obs in zip(bundle["plan"]["cells"], bundle["observations"], strict=True):
            if cell["arm"] == "candidate":
                obs["score"] = 0
    if mode == "partial":
        bundle["result"]["cells"].pop()
    if mode == "wrong-policy":
        bundle["policy_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="promot|pass"):
        record_bundle(bundle, DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"])
    assert load_baseline(tmp_path, "demo")["baseline_id"] == first["baseline_id"]


@pytest.mark.parametrize("field", ["suite_digest", "grader_digest", "framework_digest", "harness", "kind"])
def test_identity_drift_rejects_replacement(tmp_path, field):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    bundle = changed_bundle(ci_bundle())
    value = "sha256:" + "b" * 64 if field.endswith("digest") else "different"
    bundle["identity"][field] = value
    if field == "suite_digest":
        bundle["plan"]["suites"][0]["hash"] = value
    if field == "grader_digest":
        bundle["plan"]["grader"]["digest"] = value
    if field == "harness":
        bundle["plan"]["harnesses"] = [value]
    if field == "kind":
        bundle["plan"]["subjects"][0]["kind"] = value
    bundle["result"]["plan_digest"] = plan_digest(bundle["plan"])
    with pytest.raises(ValueError, match="identity|drift"):
        record_bundle(bundle, DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"])
    assert load_baseline(tmp_path, "demo")["baseline_id"] == first["baseline_id"]


def test_new_model_is_separate_and_ambiguous_reads_require_model(tmp_path):
    old = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    new_bundle = changed_bundle(ci_bundle(), model="model-b")
    new = record_bundle(new_bundle, DEFAULT_POLICY, tmp_path, expected_previous=None)
    assert load_baseline(tmp_path, "demo", model="model-a")["baseline_id"] == old["baseline_id"]
    assert load_baseline(tmp_path, "demo", model="model-b")["baseline_id"] == new["baseline_id"]
    with pytest.raises(ValueError, match="model|ambiguous"):
        load_baseline(tmp_path, "demo")


@pytest.mark.parametrize("subject", ["../escape", "a/../../escape", "/absolute", r"C:\\escape", "a//b", "a/..", ".."])
def test_hostile_subject_never_touches_files(tmp_path, subject):
    bundle = changed_bundle(ci_bundle(), subject=subject)
    with pytest.raises(ValueError):
        record_bundle(bundle, DEFAULT_POLICY, tmp_path, expected_previous=None)
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError):
        load_baseline(tmp_path, subject)


def test_namespaced_subject_is_safely_loadable(tmp_path):
    bundle = changed_bundle(ci_bundle(), subject="skill/onetbb-quickstart")
    receipt = record_bundle(bundle, DEFAULT_POLICY, tmp_path, expected_previous=None)
    assert load_baseline(tmp_path, "skill/onetbb-quickstart")["baseline_id"] == receipt["baseline_id"]
    assert not (tmp_path / "skill").exists()


def test_corrupt_receipt_is_rejected_without_repairing_it(tmp_path):
    record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    path = next(tmp_path.rglob("current.json"))
    path.write_text('{"schema_version":', encoding="utf-8")
    with pytest.raises(ValueError, match="baseline|JSON|receipt"):
        load_baseline(tmp_path, "demo")
    assert path.read_text(encoding="utf-8") == '{"schema_version":'


def test_tampered_receipt_digest_is_rejected(tmp_path):
    record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    path = next(tmp_path.rglob("current.json"))
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["bundle"]["observations"][-1]["score"] = 0.9
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        load_baseline(tmp_path, "demo")


def test_atomic_publish_failure_preserves_previous(tmp_path, monkeypatch):
    import dokimi.runner.ci_store as store
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    def fail_replace(*args):
        raise OSError("injected atomic publish failure")
    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publish failure"):
        record_bundle(changed_bundle(ci_bundle()), DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"])
    assert load_baseline(tmp_path, "demo")["baseline_id"] == first["baseline_id"]
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list(tmp_path.rglob(".lock"))


def test_symlinked_receipt_does_not_write_or_read_external_files(tmp_path):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    path = next(tmp_path.rglob("current.json"))
    external = tmp_path / "outside.json"
    path.rename(external)
    path.symlink_to(external)
    before = external.read_bytes()
    with pytest.raises(ValueError, match="symlink"):
        record_bundle(changed_bundle(ci_bundle()), DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"])
    with pytest.raises(ValueError, match="symlink"):
        load_baseline(tmp_path, "demo")
    assert external.read_bytes() == before


def test_concurrent_compare_and_swap_only_publishes_one_winner(tmp_path):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    def attempt(index):
        bundle = changed_bundle(ci_bundle())
        bundle["observations"][-1]["elapsed_sec"] = index + 2
        try:
            return record_bundle(bundle, DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"])["baseline_id"]
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    winners = [result for result in results if result]
    assert len(winners) == 1
    assert load_baseline(tmp_path, "demo")["baseline_id"] == winners[0]


def test_missing_previous_baseline_is_not_created_with_expected_id(tmp_path):
    with pytest.raises(ValueError, match="previous"):
        record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous="sha256:" + "0" * 64)
    assert not list(tmp_path.rglob("current.json"))


def test_repository_drift_is_rejected(tmp_path):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    bundle = changed_bundle(ci_bundle())
    bundle["source"]["repository"] = "different/repo"
    with pytest.raises(ValueError, match="repository"):
        record_bundle(bundle, DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"])
    assert load_baseline(tmp_path, "demo")["baseline_id"] == first["baseline_id"]


def test_policy_drift_requires_explicit_revalidation_store(tmp_path):
    from dokimi.runner.ci_policy import policy_digest
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    bundle = changed_bundle(ci_bundle())
    policy = {**DEFAULT_POLICY, "min_tasks": 4}
    bundle["policy_digest"] = policy_digest(policy)
    with pytest.raises(ValueError, match="policy drift"):
        record_bundle(bundle, policy, tmp_path, expected_previous=first["baseline_id"])
    assert load_baseline(tmp_path, "demo")["baseline_id"] == first["baseline_id"]


def test_symlinked_subject_directory_is_rejected(tmp_path):
    record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    directory = next(p for p in tmp_path.iterdir() if p.is_dir())
    external = tmp_path / "elsewhere"
    directory.rename(external)
    directory.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        load_baseline(tmp_path, "demo")


def test_corrupt_approval_cannot_be_silently_replaced(tmp_path):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    path = next(tmp_path.rglob("current.json"))
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="baseline receipt"):
        record_bundle(changed_bundle(ci_bundle()), DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"])
    assert path.read_text(encoding="utf-8") == "[]"


def test_explicit_revalidation_can_replace_context_with_compare_and_swap(tmp_path):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    bundle = changed_bundle(ci_bundle())
    bundle["identity"]["framework_digest"] = "sha256:" + "b" * 64
    result = record_bundle(bundle, DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"],
                           allow_revalidation=True)
    assert result["revalidated_from"] == first["baseline_id"]
    assert load_baseline(tmp_path, "demo")["bundle"]["identity"]["framework_digest"].endswith("b" * 64)


def test_explicit_revalidation_cannot_bypass_failed_gate_or_wrong_previous(tmp_path):
    first = record_bundle(ci_bundle(), DEFAULT_POLICY, tmp_path, expected_previous=None)
    bundle = changed_bundle(ci_bundle())
    with pytest.raises(ValueError, match="compare-and-swap"):
        record_bundle(bundle, DEFAULT_POLICY, tmp_path, expected_previous="wrong", allow_revalidation=True)
    bundle["budget_stopped"] = True
    with pytest.raises(ValueError, match="promote"):
        record_bundle(bundle, DEFAULT_POLICY, tmp_path, expected_previous=first["baseline_id"], allow_revalidation=True)
    assert load_baseline(tmp_path, "demo")["baseline_id"] == first["baseline_id"]
