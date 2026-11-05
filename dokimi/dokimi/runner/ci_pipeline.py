# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""CI orchestration over existing catalog checks and runtime measurement adapters."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dokimi.runner.ci_catalog import CatalogPlan

from dokimi import __version__
from dokimi.artifacts import load_artifact, save_artifact
from dokimi.external.catalog_eval import evaluate_catalog
from dokimi.external.catalog_git import source_and_changes
from dokimi.external.catalog_receipts import receipts
from dokimi.runner.ci_catalog_inputs import framework_digest
from dokimi.runner.ci_io import output_directory, read_policy, write_bundle
from dokimi.runner.ci_policy import policy_digest
from dokimi.runner.ci_source import source_identity, trusted_base
from dokimi.runner.plan import plan_digest


def bind_run(parts: dict[str, Any], source: dict[str, Any], policy: dict[str, Any], *, mode: str) -> dict[str, Any]:
    bundle = deepcopy(parts)
    bundle.update(schema_version="ci_run.v1", mode=mode, source=source, policy_digest=policy_digest(policy))
    bundle["plan"]["subjects"] = [{"id": bundle["identity"]["subject"], "kind": bundle["identity"]["kind"],
                                  "sha": source["head_sha"], "fingerprint": bundle["identity"]["content_digest"]}]
    bundle["result"]["plan_digest"] = plan_digest(bundle["plan"])
    bundle.setdefault("budget_stopped", False)
    return bundle


def run_check(root: Path, out: Path, *, base: str | None = None, policy_path: Path | None = None,
              source_repository: str | None = None, expected_head: str | None = None,
              expected_base: str | None = None, subject_kind: str = "catalog",
              manifest: str | None = None, base_root: Path | None = None,
              config_path: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    out = output_directory(root, out)
    source = source_identity(root, base=base, source_repository=source_repository,
                             expected_head=expected_head, expected_base=expected_base)
    policy = _check_controls(root, base_root, source, policy_path, config_path)
    if subject_kind == "repo":
        from dokimi.runner.ci_repository import check_repository

        parts = check_repository(root, manifest)
    else:
        parts = _catalog_check(root, out, base)
    bundle = bind_run(parts, source, policy, mode="static")
    if subject_kind == "catalog":
        report = load_artifact("catalog_eval", out / "catalog.json")
        report["plan_digest"] = bundle["result"]["plan_digest"]
        save_artifact("catalog_eval", report, out / "catalog.json")
    return write_bundle(out, bundle, policy)


def _check_controls(root: Path, base_root: Path | None, source: dict[str, Any],
                    policy_path: Path | None, config_path: Path | None) -> dict[str, Any]:
    from dokimi.repo_config import discover, load_repo_config

    config_file = discover(root, explicit=config_path)
    config = load_repo_config(root, explicit=config_file) if config_file else None
    if config:
        config.subjects(hashes=False)
    if policy_path:
        read_policy(_candidate_path(policy_path, root))
    with trusted_base(root, source["base_sha"], base_root) as trusted:
        path = _trusted_path(policy_path, root, trusted)
        return read_policy(path if path and path.exists() else None)


def _catalog_check(root: Path, out: Path, base: str | None) -> dict[str, Any]:
    import platform

    source, changed, untracked, owners, note = source_and_changes(root, base, out)
    report = evaluate_catalog(root, changed_paths=changed, untracked_paths=untracked, base_owners=owners)
    checker = framework_digest()
    report.update(source=source, checker={"package_sha256": checker, "dokimi_version": __version__,
                                         "python_version": platform.python_version()})
    if note:
        report["selection_note"] = note
    plan, result = receipts(report)
    digest = "sha256:" + report["catalog_digest"]
    identity = {"subject": "catalog", "kind": "catalog", "content_digest": digest,
                "suite_digest": digest, "framework_digest": checker, "model": None,
                "harness": "catalog-static", "grader_digest": checker}
    plan["schema_version"] = "plan.v1"
    result["schema_version"] = "result.v1"
    plan["suites"][0]["hash"] = digest
    report["plan_digest"] = result["plan_digest"]
    save_artifact("catalog_eval", report, out / "catalog.json")
    observations = [{"cell_id": cell["cell_id"], "score": float(cell["status"] == "pass"),
                     "cost_usd": 0.0, "elapsed_sec": 0.0, "discovered": None} for cell in result["cells"]]
    return {"plan": plan, "result": result, "identity": identity, "observations": observations}


def run_live(root: Path, out: Path, *, base_root: Path | None, base: str | None, policy_path: Path | None,
             config_path: Path | None, subjects: list[str] | None, model: str, provider: str,
             judge_model: str, judge_provider: str, reps: int, harness: str, max_usd: float | None,
             source_repository: str | None = None, expected_head: str | None = None,
             expected_base: str | None = None, subject_kind: str = "catalog",
             manifest: str | None = None, max_calls: int | None = None) -> dict[str, Any]:
    from dokimi.repo_config import load_repo_config
    from dokimi.runner.ci_catalog import plan_catalog, run_catalog

    root = root.resolve()
    out = output_directory(root, out)
    source = source_identity(root, base=base, source_repository=source_repository,
                             expected_head=expected_head, expected_base=expected_base, clean=True)
    with trusted_base(root, source["base_sha"], base_root) as trusted:
        policy = read_policy(_trusted_path(policy_path, root, trusted))
        config_file = _trusted_path(config_path, root, trusted)
        config = load_repo_config(trusted, explicit=config_file) if config_file else None
        cap = None if provider == judge_provider == "claude-cli" else _cap(max_usd, config)
        if subject_kind == "repo":
            from dokimi.runner.ci_repository import run_repository

            if cap is None:
                raise ValueError("repository subscription runs do not yet have a call-count budget adapter")
            parts = run_repository(root, trusted, out, source, model, harness, reps, cap, manifest)
        else:
            prepared = plan_catalog(root, trusted, config=config, subjects=subjects, model=model,
                                    provider=provider, judge_model=judge_model, judge_provider=judge_provider,
                                    reps=reps, harness=harness)
            if len(prepared.subjects) != 1:
                raise ValueError("live CI needs exactly one eligible --subject with an approved suite")
            _approved_cases_only(prepared)
            for subject in prepared.subjects:
                subject.plan["budget"]["cost_usd"] = cap
            raw = run_catalog(prepared, max_cost_usd=cap, max_calls=max_calls)
            parts = raw["runs"][0]
            parts["budget_stopped"] = raw["budget_stopped"]
        bundle = bind_run(parts, source, policy, mode="live")
        return write_bundle(out, bundle, policy)


def _approved_cases_only(prepared: CatalogPlan) -> None:
    if not prepared.exploratory:
        return
    skipped = sum(len(subject.plan["cells"]) for subject in prepared.exploratory)
    for subject in prepared.subjects:
        subject.plan["suites"][0]["reasons"].append(
            f"{skipped} head-added exploratory cells not executed in CI; approve the suite and revalidate separately")
    prepared.exploratory.clear()


def _trusted_path(path: Path | None, root: Path, trusted: Path) -> Path | None:
    if path is None:
        return None
    resolved = Path(os.path.abspath(_candidate_path(path, root)))
    if resolved.is_relative_to(root):
        target = trusted / resolved.relative_to(root)
        if not target.resolve().is_relative_to(trusted.resolve()):
            raise ValueError("trusted config and policy must not escape the comparison base")
        return target
    if resolved.is_relative_to(trusted.resolve()):
        return resolved
    raise ValueError("live config and policy must come from the trusted comparison base")


def _candidate_path(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def _cap(maximum: float | None, config: Any) -> float:
    import math

    if maximum is None or not math.isfinite(maximum) or maximum <= 0:
        raise ValueError("--max-usd must be a positive finite cap")
    configured = config.data.get("budget", {}).get("pr_max_usd") if config else None
    return min(maximum, configured) if configured is not None else maximum
