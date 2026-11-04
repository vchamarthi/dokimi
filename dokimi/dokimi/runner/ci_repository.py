# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Repository CI: no-agent checks, then pinned self-checks before metered trials."""

from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from dokimi import process
from dokimi.repos import RepoSource, WorkTree, check_repo, open_source
from dokimi.repos.arms import slug
from dokimi.repos.batch_ledger import cell_name
from dokimi.repos.graders import graders_digest
from dokimi.repos.harness_profile import load_profile
from dokimi.repos.l0 import scan_repo
from dokimi.repos.runner import AgentOptions, execution_record
from dokimi.repos.source import base_opener
from dokimi.repos.static import status_class
from dokimi.repos.workspace import Task, load_tasks

from .ci_catalog_inputs import fingerprint
from .ci_repository_inputs import (
    grader_digest, repository_identity, repository_manifest,
    repository_plan, suite_inputs, worktree_suite,
)
from .plan import plan_digest


def _parts(plan: dict[str, Any], identity: dict[str, Any], statuses: list[dict[str, Any]],
           observations: list[dict[str, Any]], *, stopped: bool = False) -> dict[str, Any]:
    return {"plan": plan, "result": {"schema_version": "result.v1", "plan_digest": plan_digest(plan),
                                     "cells": statuses}, "identity": identity,
            "observations": observations, "budget_stopped": stopped}


def _observation(cid: str, score: float | None = None, cost: float | None = None,
                 elapsed: float = 0.0) -> dict[str, Any]:
    return {"cell_id": cid, "score": score, "cost_usd": cost, "elapsed_sec": elapsed, "discovered": None}


def _blocked(plan: dict[str, Any], identity: dict[str, Any], status: str, detail: str) -> dict[str, Any]:
    cells = [{"cell_id": c["cell_id"], "status": status, "detail": detail} for c in plan["cells"]]
    return _parts(plan, identity, cells, [_observation(c["cell_id"]) for c in plan["cells"]])


def check_repository(root: Path, manifest: str | None = None) -> dict[str, Any]:
    """Read guidance claims without a model, Docker, or repository entry-point execution."""
    tree = WorkTree(root)
    config = repository_manifest(tree, manifest, allow_default=True)
    validation = check_repo(tree, config, base_opener(RepoSource.parse(str(root.resolve())), tree))
    scan = scan_repo(tree, config)
    rows = [("repo::manifest", "pass" if validation.ok else "fail", "; ".join(validation.errors))]
    rows.append(("repo::guidance-coverage", "inconclusive" if scan["incomplete"] else "pass",
                 "; ".join(scan["incomplete_reasons"])))
    for index, claim in enumerate(scan["claims"]):
        status = {"ok": "pass", "fail": "fail", "unresolved": "inconclusive"}[status_class(claim["status"])]
        rows.append((f"repo::claim:{index}", status, f"{claim['file']}:{claim['line']}: {claim['claim']}"))
    suite = fingerprint({"manifest": config.to_dict(), "checks": "repository-guidance-static"})
    identity = repository_identity(tree, config, suite, model=None, harness="repo-static", runtime={})
    plan = repository_plan(identity, [row[0] for row in rows], sha=None, reps=1, live=False)
    statuses = [{"cell_id": cell["cell_id"], "status": row[1], "detail": row[2]}
                for cell, row in zip(plan["cells"], rows, strict=True)]
    observations = [_observation(c["cell_id"], float(row[1] == "pass"), 0)
                    for c, row in zip(plan["cells"], rows, strict=True)]
    return _parts(plan, identity, statuses, observations)


def _invoke(arguments: list[str], directory: Path, label: str) -> process.Completed:
    directory.mkdir(parents=True, exist_ok=True)
    return process.run([sys.executable, "-m", "dokimi", *arguments], env=process.Env.inherit(),
                       timeout=24 * 3600, cwd=Path(__file__).resolve().parents[2],
                       stdout_to=directory / f"{label}.stdout", stderr_to=directory / f"{label}.stderr", term_grace=10)


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing or unsafe repository receipt: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except RecursionError as exc:
        raise ValueError("repository receipt is nested too deeply") from exc
    if not isinstance(value, dict):
        raise ValueError(f"repository receipt is not an object: {path}")
    return value


def _checked(out: Path, tasks: list[Task]) -> bool:
    receipt = _read(out / "check" / "check.json")
    rows = receipt.get("tasks")
    if not isinstance(rows, list) or len(rows) != len(tasks):
        return False
    expected = {t.id for t in tasks}
    return ({r.get("task") for r in rows} == expected and
            all(r.get("verdict") == "ok" and not r.get("problems") for r in rows))


def _trial_args(source: str, manifest: str, out: Path, arms: dict[str, str], model: str,
                harness: str, reps: int, cap: float) -> list[str]:
    return ["repo", "run", source, "--manifest", manifest, "--out", str(out), "--batch", "ci",
            "--arm", ",".join(arms.values()), "--model", model, "--harness", harness,
            "--reps", str(reps), "--budget", str(cap), "--max-batch-usd", str(cap),
            "--jobs", "1", "--delegation", "none", "--agent-network", "bridge"]


def _validate_options(model: str, reps: int, cap: float) -> None:
    if not model.strip() or model.lower() in {"unknown", "none", "sonnet", "opus", "haiku"}:
        raise ValueError("repository CI needs an explicit resolved model id, not a moving alias")
    if reps < 1 or not math.isfinite(cap) or cap <= 0:
        raise ValueError("repository repetitions and finite budget must be positive")


def run_repository(root: Path, trusted_base: Path, out: Path, source: dict[str, Any], model: str,
                   harness: str, reps: int, cap: float, manifest: str | None = None) -> dict[str, Any]:
    """Check every approved task, then compare no, approved, and candidate guidance.

    The agent container uses bridge networking with unrestricted egress. Tasks, image and grader
    content come only from ``source.base_sha``; candidate eval changes cannot make the trial easier.
    """
    _validate_options(model, reps, cap)
    if source["base_sha"] == source["head_sha"]:
        raise ValueError("repository live CI requires distinct approved base and candidate revisions")
    pinned = f"{root.resolve()}@{source['base_sha']}"
    tree = open_source(RepoSource.parse(pinned))
    candidate = open_source(RepoSource.parse(f"{root.resolve()}@{source['head_sha']}"))
    config = repository_manifest(tree, manifest)
    profile = load_profile(harness)
    profile.tool_args("all", "none")
    work = out / ("repository-" + uuid.uuid4().hex)
    work.mkdir(parents=True)
    suite, eval_digest = suite_inputs(tree, config, work / "approved-content")
    tasks = load_tasks(work / "approved-content", config)
    with tempfile.TemporaryDirectory(prefix="dokimi-repo-base-") as tmp:
        if worktree_suite(trusted_base, config.path, Path(tmp) / "content")[1] != suite:
            raise ValueError("trusted base checkout differs from pinned repository evaluation suite")
    identity = repository_identity(candidate, config, suite, model=model, harness=profile.name, runtime={})
    plan = repository_plan(identity, [f"repo::{t.id}" for t in tasks], sha=source["head_sha"],
                           reps=reps, live=True, cap=cap)
    check_out = work / "selfcheck"
    try:
        checked = _invoke(["eval", "check", pinned, "--manifest", config.path, "--out", str(check_out)], work, "selfcheck")
        if not _checked(check_out, tasks):
            return _blocked(plan, identity, "invalid", "approved task self-check did not pass every planned task")
        if not checked.ok:
            return _blocked(plan, identity, "infra_error", "self-check process failed despite its receipt")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return _blocked(plan, identity, "infra_error", f"self-check could not be verified: {exc}")
    arms = {"none": "guidance:none", "approved": f"guidance:rev:{source['base_sha']}",
            "candidate": f"guidance:rev:{source['head_sha']}"}
    try:
        result = _invoke(_trial_args(pinned, config.path, work / "runs", arms, model, harness, reps, cap), work, "trials")
        return _normalize(work / "runs" / "ci", plan, identity, arms, profile.name, tasks,
                          {"tree": tree.label, "eval_dir": eval_digest,
                           "image": f"dokimi/{slug(config.id).lower()}:{source['base_sha'][:12]}"}, result, config.path, harness)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _blocked(plan, identity, "infra_error", f"repository trial could not be verified: {exc}")


def _finite(value: Any, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError("repository observation is not a finite number in range")
    return float(value)


def _score(raw: dict[str, Any]) -> float:
    grade = raw["grade"]
    if grade["score_only"]:
        score = _finite(grade["score"])
        top = _finite(raw["score_max"], minimum=1e-300)
        if score > top:
            raise ValueError("repository score exceeds declared maximum")
        return score / top
    if not isinstance(grade.get("success"), bool):
        raise ValueError("repository gate has no boolean success result")
    return float(grade["success"])


def _runtime(raw: dict[str, Any], meta: dict[str, Any], *, model: str, harness: str) -> tuple[str, str]:
    for record in (raw, meta):
        if record.get("model") != model or record.get("model_resolved") != model or record.get("harness") != harness:
            raise ValueError("repository cell has unknown or mismatched resolved model/harness")
    version = raw.get("harness_version")
    execution = raw.get("execution", {}).get("id")
    if not version or version == "unknown" or not execution:
        raise ValueError("repository cell has unknown harness version/execution")
    if meta.get("harness_version") != version or meta.get("execution", {}).get("id") != execution:
        raise ValueError("repository result and metadata disagree about execution")
    if meta.get("agent_network") != "bridge" or raw.get("agent", {}).get("network") != "bridge":
        raise ValueError("repository agent network differs from declared bridge mode")
    return version, execution


def _task_provenance(raw: dict[str, Any], meta: dict[str, Any], task: Task, arm: str) -> None:
    if meta.get("base_rev") != task.base:
        raise ValueError("repository cell ran on a different task base")
    if arm.startswith("guidance:rev:") and meta.get("arm_rev", {}).get("sha") != arm.removeprefix("guidance:rev:"):
        raise ValueError("repository guidance revision differs from declared condition")
    if not arm.startswith("guidance:rev:") and meta.get("arm_rev") is not None:
        raise ValueError("no-guidance condition unexpectedly records a guidance revision")
    expected_max = task.score["max"] if task.score else None
    if raw.get("score_task") != bool(task.score) or raw.get("score_max") != expected_max:
        raise ValueError("repository score normalization differs from the approved task")


def _cell(directory: Path, cell: dict[str, Any], arm: str, harness: str, task: Task) -> tuple[dict, dict, tuple[str, str] | None]:
    cid = cell["cell_id"]
    result = {"cell_id": cid, "status": "not_run"}
    observation = _observation(cid)
    if not (directory / "result.json").exists():
        return result, observation, None
    try:
        raw, meta = _read(directory / "result.json"), _read(directory / "meta.json")
        expected = {"task": cell["task"].removeprefix("repo::"), "arm": arm,
                    "model": cell["model"], "rep": cell["rep"]}
        if any(record.get(k) != v for record in (raw, meta) for k, v in expected.items()):
            raise ValueError("repository cell coordinates differ from declared plan")
        _task_provenance(raw, meta, task, arm)
        runtime = _runtime(raw, meta, model=cell["model"], harness=harness)
        agent, grade = raw["agent"], raw["grade"]
        observation["elapsed_sec"] = _finite(agent["wall_s"]) + _finite(grade["wall_s"])
        observation["cost_usd"] = None if agent["cost_usd"] is None else _finite(agent["cost_usd"])
        if grade["status"] != "graded" or agent["rc"] != 0 or agent["timed_out"] or agent["is_error"] is not False:
            result.update(status="infra_error", detail="repository agent or grader failed")
            return result, observation, runtime
        observation["score"] = _score(raw)
        result.update(status="pass" if grade["score_only"] or grade["success"] else "fail", model=cell["model"])
        return result, observation, runtime
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        result.update(status="invalid", detail=str(exc))
        return result, observation, None


def _normalize(batch: Path, plan: dict[str, Any], identity: dict[str, Any], arms: dict[str, str],
               harness: str, tasks: list[Task], expected_pin: dict[str, str], process_result: process.Completed,
               manifest: str, requested_harness: str) -> dict[str, Any]:
    pin = _read(batch / "_eval" / "batch.json")
    if any(t.raw["grader"].startswith("dokimi:") for t in tasks):
        expected_pin["graders"] = graders_digest()
    if any(pin.get(k) != value for k, value in expected_pin.items()):
        raise ValueError("repository batch has a different source, image or grader suite pin")
    expected = {cell_name(c["task"].removeprefix("repo::"), arms[c["arm"]], c["model"], c["rep"])
                for c in plan["cells"]}
    declared = _read(batch / "_eval" / "plan.json").get("cells")
    if not isinstance(declared, list) or len(declared) != len(expected) or set(declared) != expected:
        raise ValueError("repository batch plan differs from declared CI cells")
    if {p.parent.name for p in batch.glob("*/result.json")} - expected:
        raise ValueError("repository batch has unplanned completed cells")
    task_by_id = {f"repo::{task.id}": task for task in tasks}
    normalized = [_cell(batch / cell_name(c["task"].removeprefix("repo::"), arms[c["arm"]], c["model"], c["rep"]),
                        c, arms[c["arm"]], harness, task_by_id[c["task"]]) for c in plan["cells"]]
    return _finish(plan, identity, normalized, process_result, manifest, requested_harness)


def _finish(plan: dict[str, Any], identity: dict[str, Any], normalized: list[tuple[dict, dict, tuple[str, str] | None]],
            proc: process.Completed, manifest: str, harness: str) -> dict[str, Any]:
    runtimes = {row[2] for row in normalized if row[2] is not None}
    if len(runtimes) > 1:
        return _blocked(plan, identity, "invalid", "repository cells mix harness versions or execution identities")
    if not runtimes:
        return _parts(plan, identity, [r[0] for r in normalized], [r[1] for r in normalized])
    version, execution = next(iter(runtimes))
    runtime = {"manifest": manifest, "harness": harness, "harness_version": version,
               "execution_id": execution, "agent_network": "bridge"}
    identity["grader_digest"] = grader_digest(identity["suite_digest"], runtime)
    plan["grader"]["digest"] = identity["grader_digest"]
    results, observations = [r[0] for r in normalized], [r[1] for r in normalized]
    spent = sum(o["cost_usd"] or 0 for o in observations)
    stopped = spent >= plan["budget"]["cost_usd"]
    if not proc.ok and all(r["status"] in {"pass", "fail"} for r in results):
        return _blocked(plan, identity, "infra_error", "repository process failed despite complete measurements")
    parts = _parts(plan, identity, results, observations, stopped=stopped)
    parts["repository_runtime"] = runtime
    return parts


def _execution(harness: str) -> tuple[str, str]:
    profile = load_profile(harness)
    binary = shutil.which(profile.bin)
    if binary is None:
        raise ValueError("repository harness executable is unavailable for identity verification")
    version = profile.version(process.Env.only(["PATH", "SYSTEMROOT"]), bin_path=binary)
    options = AgentOptions(profile=profile, model="identity-only", harness_bin=Path(binary),
                           harness_version=version, tools="all", delegation="none")
    return version, execution_record(options)["id"]


def verify_repository_identity(root: Path, bundle: dict[str, Any], *, manifest: str | None = None,
                               content_only: bool = False) -> None:
    """Re-read merged guidance and eval content, then verify the installed harness identity."""
    runtime = bundle.get("repository_runtime")
    if not isinstance(runtime, dict):
        raise ValueError("repository record requires runtime provenance")
    with tempfile.TemporaryDirectory(prefix="dokimi-repo-record-") as tmp:
        config, suite = worktree_suite(root, manifest or runtime["manifest"], Path(tmp) / "content")
    identity = bundle["identity"]
    expected = repository_identity(WorkTree(root), config, suite, model=identity["model"],
                                   harness=load_profile(runtime["harness"]).name, runtime=runtime)
    keys = ("subject", "kind", "content_digest", "suite_digest") if content_only else tuple(identity)
    if any(expected.get(key) != identity.get(key) for key in keys):
        raise ValueError("repository record identity changed: content, suite, framework or grader")
    if not content_only and _execution(runtime["harness"]) != (runtime["harness_version"], runtime["execution_id"]):
        raise ValueError("repository record harness identity changed since measurement")
