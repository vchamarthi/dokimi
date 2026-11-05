# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Real, metered catalog trials with approved suites held fixed across skill arms."""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dokimi import llm_cli
from dokimi.eval.reference_checks import grade
from dokimi.external.catalog_eval import evaluate_catalog
from dokimi.external.catalog_eval_inputs import Inputs, checked_root
from dokimi.llm import LLM, credential_env_var, resolve_api_key, resolve_model
from dokimi.llm_pricing import list_cost_usd
from dokimi.metrics.usage import UsageRecord
from dokimi.repo_config import RepoConfig

from .ci_catalog_inputs import CatalogSubject, context, fingerprint, framework_digest, questions
from .plan import plan_cells, plan_digest

GRADER_ID = "catalog-reference-checks"


@dataclass
class CatalogPlan:
    catalog: dict[str, Any]
    subjects: list[CatalogSubject]
    exploratory: list[CatalogSubject]
    model: str
    provider: str
    judge_model: str
    judge_provider: str
    harness: str
    cli_version: str | None = None

    @property
    def plans(self) -> list[dict[str, Any]]:
        return [subject.plan for subject in self.subjects]


def plan_catalog(root: Path, base_root: Path | None = None, *, config: RepoConfig | None = None,
                 subjects: list[str] | None = None, model: str, provider: str,
                 judge_model: str, judge_provider: str, reps: int, harness: str) -> CatalogPlan:
    """Resolve a catalog experiment without credentials, Docker or paid calls."""
    if harness not in {"arms-runner", "docker-claude"}:
        raise ValueError(f"catalog CI harness is unsupported: {harness}")
    if reps < 1 or not model.strip() or not judge_model.strip():
        raise ValueError("reps must be positive and answer/judge models must be explicit")
    root = checked_root(root)
    base_root = checked_root(base_root) if base_root is not None else None
    report = evaluate_catalog(root)
    if report["status"] != "pass":
        raise ValueError("catalog static checks failed; repair the catalog before running live trials")
    base = evaluate_catalog(base_root) if base_root is not None else None
    names = {row["name"] for row in report["skills"]}
    configured = _configured_names(config, root, report["skills"], harness) if config is not None else names
    selected = {name.removeprefix("skill/") for name in subjects} if subjects is not None else configured
    if selected - configured:
        raise ValueError(f"subjects are not configured: {sorted(selected - configured)}")
    if selected - names:
        raise ValueError(f"unknown catalog subjects: {sorted(selected - names)}")
    prepared = CatalogPlan(report, [], [], model, provider, judge_model, judge_provider, harness)
    report["runtime"] = {}
    for row in report["skills"]:
        if row["name"] in selected:
            _prepare_subject(prepared, root, base_root, row, base, reps)
    return prepared


def _prepare_subject(prepared: CatalogPlan, root: Path, base_root: Path | None,
                     row: dict, base: dict | None, reps: int) -> None:
    name = row["name"]
    base_row = next((r for r in base["skills"] if r["name"] == name), None) if base else None
    if not row["evaluated_here"] or (base_row and not base_row["evaluated_here"]):
        prepared.catalog["runtime"][name] = {"status": "checks_only", "reason": "evaluated_here is false"}
        return
    suite_root = base_root if base_row and base_root else root
    suite_row = base_row or row
    inputs = Inputs(suite_root)
    qs = questions(inputs, suite_row["path"]) if prepared.harness == "arms-runner" else []
    tasks = suite_row["tasks"] if prepared.harness == "docker-claude" else []
    if not qs and not tasks:
        prepared.catalog["runtime"][name] = {"status": "no_tasks", "reason": f"no approved {prepared.harness} suite"}
        return
    contexts = {"none": ""}
    paths = {"candidate": root / row["path"]}
    if base_row and base_root:
        contexts["approved"] = context(Inputs(base_root), base_row["path"])
        paths["approved"] = base_root / base_row["path"]
    contexts["candidate"] = context(Inputs(root), row["path"])
    primary = _subject(prepared, root, suite_root, row, qs, contexts, paths, tasks, reps, "base")
    prepared.subjects.append(primary)
    prepared.catalog["runtime"][name] = {"status": "planned", "cells": len(primary.plan["cells"])}
    if base_row and prepared.harness == "arms-runner":
        old = {q["id"] for q in qs}
        extra = [q for q in questions(Inputs(root), row["path"]) if q["id"] not in old]
        if extra:
            prepared.exploratory.append(_subject(prepared, root, root, row, extra, contexts, paths, [], reps, "head-new"))


def _subject(prepared: CatalogPlan, root: Path, suite_root: Path, row: dict, qs: list,
             contexts: dict, paths: dict, tasks: list, reps: int, scope: str) -> CatalogSubject:
    subject_id = f"skill/{row['name']}"
    suite_hash = fingerprint({"questions": qs, "tasks": tasks, "scope": scope})
    package_hash = framework_digest()
    grader_hash = _grader_digest(prepared, package_hash, qs)
    model = resolve_model(prepared.model, prepared.provider)
    task_ids = [f"{subject_id}::{q['id']}" for q in qs] + [f"{subject_id}::{t['name']}" for t in tasks]
    cells = plan_cells(contexts, [model], task_ids, reps)
    identity = {"subject": subject_id, "kind": "skill", "content_digest": "sha256:" + row["bundle_digest"],
                "suite_digest": suite_hash, "framework_digest": package_hash, "model": model,
                "harness": prepared.harness, "grader_digest": grader_hash}
    plan = {"schema_version": "plan.v1", "subjects": [{"id": subject_id, "kind": "skill", "sha": None,
                                                     "fingerprint": identity["content_digest"]}],
            "suites": [{"id": f"{subject_id}:{scope}", "hash": suite_hash,
                        "reasons": ["approved suite held fixed across every treatment arm" if scope == "base" else "head-added exploratory cases"]}],
            "models": [model], "harnesses": [prepared.harness],
            "grader": {"id": GRADER_ID, "digest": grader_hash}, "varied_variable": "skill-content",
            "cells": cells, "budget": {"cells": len(cells), "agent_calls": None, "cost_usd": None}}
    return CatalogSubject(row["name"], root, suite_root, row, qs, contexts, paths, tasks, plan, identity)


class BudgetStopped(RuntimeError):
    """No subsequent paid call is allowed."""


@dataclass
class _Meter:
    maximum: float | None
    callback: Callable[[UsageRecord], Any] | None = None
    max_calls: int | None = None
    subscription: bool = False
    spent: float = 0.0
    calls: int = 0
    stopped: bool = False
    records: list[UsageRecord] = field(default_factory=list)

    def before(self) -> None:
        cost_exhausted = self.maximum is not None and self.spent >= self.maximum
        calls_exhausted = self.max_calls is not None and self.calls >= self.max_calls
        if self.stopped or cost_exhausted or calls_exhausted:
            self.stopped = True
            raise BudgetStopped("call/cost cap reached or a previous API call has unknown cost")

    def add(self, usage: UsageRecord) -> None:
        known = usage.cost_usd is not None and not isinstance(usage.cost_usd, bool) and math.isfinite(usage.cost_usd) and usage.cost_usd >= 0
        if not known:
            usage = replace(usage, cost_usd=None)
        self.records.append(usage)
        self.calls += usage.n_calls
        if known:
            self.spent += float(usage.cost_usd or 0)
        if not self.subscription and (not known or (self.maximum is not None and self.spent >= self.maximum)):
            self.stopped = True
        if self.callback is not None:
            try:
                self.callback(usage)
            except Exception:
                self.stopped = True
                raise


def _preflight(prepared: CatalogPlan, maximum: float | None, max_calls: int | None = None) -> None:
    if os.environ.get("DOKIMI_RESPONSE_CACHE"):
        raise ValueError("live catalog CI requires DOKIMI_RESPONSE_CACHE to be disabled")
    if prepared.harness == "docker-claude":
        _native_provider(prepared.provider)
    models = [(prepared.model, prepared.provider)]
    if prepared.harness == "arms-runner":
        models.append((prepared.judge_model, prepared.judge_provider))
        if resolve_model(*models[0]) == resolve_model(*models[1]):
            raise ValueError("catalog CI requires an independent judge model")
    if _subscription(prepared):
        _subscription_preflight(prepared, max_calls)
        return
    if maximum is None or isinstance(maximum, bool) or not math.isfinite(maximum) or maximum <= 0:
        raise ValueError("max_cost_usd must be a finite positive amount")
    for model, provider in models:
        if not resolve_api_key(provider):
            raise ValueError(f"missing {credential_env_var(provider)} for provider {provider}")
        price = list_cost_usd(model, provider, 1000, 1000)
        if price is None or not math.isfinite(price) or price <= 0:
            raise ValueError(f"unknown or unpriced model: {provider}/{model}")


def run_catalog(prepared: CatalogPlan, *, max_cost_usd: float | None = None, max_calls: int | None = None,
                on_usage: Callable[[UsageRecord], Any] | None = None) -> dict[str, Any]:
    """Run all approved cells; stop between every metered call on unknown/exhausted cost.

    The cap stops subsequent calls; a call already in flight can exceed the remaining cap.
    Head-added cases stay separately labeled exploratory and share the same total cap.
    """
    _preflight(prepared, max_cost_usd, max_calls)
    maximum = None if _subscription(prepared) else max_cost_usd
    for subject in [*prepared.subjects, *prepared.exploratory]:
        subject.plan["budget"]["cost_usd"] = maximum
        subject.plan["budget"]["agent_calls"] = max_calls
    meter = _Meter(maximum, on_usage, max_calls=max_calls, subscription=_subscription(prepared))
    runs = [_run_subject(prepared, subject, meter) for subject in prepared.subjects]
    exploratory = [_run_subject(prepared, subject, meter) for subject in prepared.exploratory]
    return {"runs": runs, "exploratory_runs": exploratory, "catalog": prepared.catalog,
            "budget_stopped": meter.stopped}


def _run_subject(prepared: CatalogPlan, subject: CatalogSubject, meter: _Meter) -> dict[str, Any]:
    result: dict[str, Any] = {"schema_version": "result.v1", "plan_digest": plan_digest(subject.plan), "cells": []}
    observations = []
    for cell in subject.plan["cells"]:
        start, record_start = time.monotonic(), len(meter.records)
        status, score, detail, resolved, discovered = "not_run", None, None, None, None
        try:
            meter.before()
            if prepared.harness == "arms-runner":
                status, score, detail, resolved = _prompt_cell(prepared, subject, cell, meter)
            else:
                status, score, detail, resolved, discovered = _task_cell(prepared, subject, cell, meter)
        except BudgetStopped as exc:
            detail = str(exc)
        except Exception as exc:
            status, detail = "infra_error", f"{type(exc).__name__}: {exc}"
            meter.stopped = True
        records = meter.records[record_start:]
        cost = round(sum(r.cost_usd or 0 for r in records), 10) if records and all(r.cost_usd is not None for r in records) else None
        result["cells"].append({"cell_id": cell["cell_id"], "status": status, "model": resolved, "detail": detail})
        observations.append({"cell_id": cell["cell_id"], "score": score, "cost_usd": cost,
                             "elapsed_sec": max(0, time.monotonic() - start), "discovered": discovered,
                             "agent_calls": sum(record.n_calls for record in records)})
    return {"plan": subject.plan, "result": result, "identity": subject.identity,
            "observations": observations, "budget_stopped": meter.stopped,
            "runtime": {"requested_model": prepared.model, "provider": prepared.provider,
                        "judge_model": prepared.judge_model, "judge_provider": prepared.judge_provider,
                        "reps": max(cell["rep"] for cell in subject.plan["cells"]),
                        **({"billing": "subscription", "transport": "claude-cli", "cli_version": prepared.cli_version}
                           if meter.subscription else {})}}


def _complete(model: LLM, prompt: str, meter: _Meter, *, system: str = "", draw: int = 0):
    meter.before()
    reply = model.complete(prompt, system=system or None, draw=draw)
    meter.add(reply.usage)
    actual = reply.model_id or reply.usage.resolved_model
    if not actual or resolve_model(actual, model.provider) != resolve_model(model.model, model.provider):
        meter.stopped = True
        raise ValueError("model response has missing or unexpected resolved model identity")
    if reply.truncated or not isinstance(reply.text, str) or not reply.text.strip():
        raise ValueError("empty or truncated model response")
    return reply


def _prompt_cell(prepared: CatalogPlan, subject: CatalogSubject, cell: dict, meter: _Meter):
    qid = cell["task"].split("::", 1)[1]
    question = next(q for q in subject.questions if q["id"] == qid)
    answer_model = LLM(prepared.model, prepared.provider, max_tokens=4096, max_retries=0)
    judge = LLM(prepared.judge_model, prepared.judge_provider, max_tokens=2048, max_retries=0)
    answer = _complete(answer_model, question["question"], meter, system=subject.contexts[cell["arm"]], draw=cell["rep"])
    def ask(prompt: str, **kwargs) -> str:
        return _complete(judge, prompt, meter, draw=cell["rep"] * 1000 + kwargs.get("draw", 0)).text
    verdict = grade(question["grading"], question["question"], answer.text, ask)
    if verdict.error or verdict.unjudged or verdict.unrun:
        return "infra_error", None, verdict.error or "incomplete grading", answer.model_id
    score = verdict.score
    status = "inconclusive" if score is None else ("pass" if verdict.passed else "fail")
    return status, score, "answer-only reference checks; no coding task executed", resolve_model(answer.model_id or prepared.model, prepared.provider)


def _task_cell(prepared: CatalogPlan, subject: CatalogSubject, cell: dict, meter: _Meter):
    from dokimi.harnesses.runner import TaskSuiteRunner, is_errored
    from dokimi.harnesses.tasks import load_task

    name = cell["task"].split("::", 1)[1]
    entry = next(t for t in subject.tasks if t["name"] == name)
    task_path = subject.suite_root / entry["path"]
    if Inputs(subject.suite_root).bundle(task_path) != entry["digest"]:
        raise ValueError("approved task changed after planning")
    if Inputs(subject.root).bundle(subject.root / subject.row["path"]) != subject.row["bundle_digest"]:
        raise ValueError("candidate skill changed after planning")
    harness = "docker-claude" if cell["arm"] == "none" else f"docker-claude-skill:{subject.skill_paths[cell['arm']]}"
    with tempfile.TemporaryDirectory(prefix="dokimi-catalog-cell-") as scratch:
        snapshot = Path(scratch) / "task" / name
        shutil.copytree(task_path, snapshot)
        if Inputs(Path(scratch)).bundle(snapshot) != entry["digest"]:
            raise ValueError("approved task changed while snapshotting")
        task = load_task(snapshot)
        run = TaskSuiteRunner([task], [harness], model=prepared.model, output_root=Path(scratch) / "runs").run()
    row = run["results"][0]
    metrics = row.get("metrics", {})
    meter.add(UsageRecord(cost_usd=metrics.get("cost_usd"), n_calls=1))
    reported = metrics.get("models_used")
    if not isinstance(reported, list) or not reported or any(
            resolve_model(str(model), prepared.provider) != subject.identity["model"] for model in reported):
        return "invalid", None, "agent model use was missing or differed from the planned model", None, None
    if is_errored(row):
        return "infra_error", None, "task infrastructure failed", prepared.model, None
    reward = row.get("reward")
    score = float(reward) if isinstance(reward, (int, float)) and not isinstance(reward, bool) else float(bool(row["passed"]))
    if not math.isfinite(score) or not 0 <= score <= 1:
        return "invalid", None, "verifier reward outside [0,1]", prepared.model, None
    invoked = metrics.get("n_skill_invocations")
    return ("pass" if row["passed"] else "fail", score, "executable approved-suite verifier",
            subject.identity["model"], invoked > 0 if isinstance(invoked, int) else None)


def _configured_names(config: RepoConfig, root: Path, rows: list[dict], harness: str) -> set[str]:
    config = RepoConfig(config.path, root, config.data)
    by_name = {row["name"]: row for row in rows}
    names = set()
    for subject in config.subjects(hashes=False):
        name = subject.id.removeprefix("skill/")
        row = by_name.get(name)
        if subject.kind != "skill" or row is None or subject.path != row["path"]:
            raise ValueError(f"unsupported catalog configured subject: {subject.id}")
        allowed = {f"{subject.path}/evals", f"{subject.path}/evals/**",
                   f"{subject.path}/evals/evals.json", "evaluation/harbor/tasks/**",
                   "evaluation/harbor/tasks/*", "evaluation/harbor/suites.json"}
        if set(subject.suite_globs) - allowed:
            raise ValueError(f"unsupported catalog suite layout for {subject.id}: {subject.suite_globs}")
        supported = ({f"{subject.path}/evals", f"{subject.path}/evals/**", f"{subject.path}/evals/evals.json"}
                     if harness == "arms-runner" else {"evaluation/harbor/tasks/**", "evaluation/harbor/tasks/*"})
        if not supported.intersection(subject.suite_globs):
            raise ValueError(f"suite for {subject.id} does not configure the {harness} harness")
        names.add(name)
    return names


def verify_record_identity(root: Path, bundle: dict[str, Any], *, content_only: bool = False) -> None:
    """Re-plan the current checkout without a model call before accepting stored evidence."""
    runtime = bundle.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("catalog record requires runtime provenance to recompute its identity")
    identity = bundle["identity"]
    current = plan_catalog(root, root, subjects=[identity["subject"]],
                           model=runtime["requested_model"], provider=runtime["provider"],
                           judge_model=runtime["judge_model"], judge_provider=runtime["judge_provider"],
                           reps=runtime["reps"], harness=identity["harness"])
    if not content_only and "cli_version" in runtime:
        _bind_cli_version(current, runtime["cli_version"])
    expected = current.subjects[0].identity if len(current.subjects) == 1 else {}
    keys = ("subject", "kind", "content_digest", "suite_digest") if content_only else tuple(identity)
    if any(expected.get(key) != identity.get(key) for key in keys):
        raise ValueError("catalog record identity has changed: content, suite, framework, model or grader")


def _native_provider(requested: str) -> None:
    from dokimi.harnesses.agent_sandbox import operator_source_env
    from dokimi.harnesses.egress_proxy import provider

    actual = {"firstparty": "anthropic", "bedrock": "amazon-bedrock"}.get(provider(operator_source_env()))
    if requested not in {"anthropic", "amazon-bedrock"} or actual != requested:
        raise ValueError("docker-claude provider must match the configured Claude Code provider")


def _subscription(prepared: CatalogPlan) -> bool:
    return "claude-cli" in {prepared.provider, prepared.judge_provider}


def _subscription_preflight(prepared: CatalogPlan, max_calls: int | None) -> None:
    """Require an explicit first-party browser login or dedicated subscription OAuth token."""
    if prepared.harness != "arms-runner" or {prepared.provider, prepared.judge_provider} != {"claude-cli"}:
        raise ValueError("subscription trials require both providers to be claude-cli with arms-runner")
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
        raise ValueError("subscription trials require a finite positive max_calls cap")
    cli = llm_cli.detect("claude-cli")
    native_methods = {"claude.ai (firstParty)", "oauth_token (firstParty)"}
    if not cli.installed or not cli.authenticated or cli.method not in native_methods:
        raise ValueError("claude-cli requires an authenticated Claude subscription login")
    _bind_cli_version(prepared, getattr(cli, "version", None))


def _grader_digest(prepared: CatalogPlan, framework: str, questions: list[dict],
                   cli_version: str | None = None) -> str:
    payload = {"framework": framework, "judge": prepared.judge_model,
               "provider": prepared.judge_provider, "checks": [q["grading"] for q in questions]}
    if cli_version is not None:
        payload["cli_version"] = cli_version
    return fingerprint(payload)


def _bind_cli_version(prepared: CatalogPlan, version: str | None) -> None:
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?(?: \(Claude Code\))?", version):
        raise ValueError("subscription trials require a known Claude CLI version")
    if prepared.cli_version is not None and prepared.cli_version != version:
        raise ValueError("Claude CLI version changed; create a fresh plan before running")
    prepared.cli_version = version
    for subject in [*prepared.subjects, *prepared.exploratory]:
        digest = _grader_digest(prepared, subject.identity["framework_digest"], subject.questions, version)
        subject.identity["grader_digest"] = digest
        subject.plan["grader"]["digest"] = digest
