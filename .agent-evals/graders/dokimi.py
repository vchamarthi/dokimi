# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""repoevals graders for dokimi's own tasks.

dokimi_code: hidden_tests, plus the checks CI runs on the agent's tree (ruff, mypy, config, schema, schema_compat).
dokimi_scan: the use-repo-scan task; repoevals has no grader family for "use the tool for a user".
"""
from __future__ import annotations

import json
import re
import shlex

from repoevals.graders import GradeContext, evaluate_gate
from repoevals.graders.tests import apply_reference, g_hidden_tests, o_hidden_tests
from repoevals.util import run_shell

CHECKS = {
    "ruff_ok": "ruff check .",
    "mypy_ok": "mypy",
    "config_ok": "python -m dokimi.config_check",
    "schema_ok": "python -m dokimi.schema_check benchmarks/spec.v1.yaml",
}


def _sh(ctx: GradeContext, cmd: str):
    return run_shell(cmd, cwd=ctx.workspace / "dokimi", timeout=ctx.task.verifier.get("timeout_sec", 1800),
                     env=ctx.config.test.env)


def grade_dokimi_code(ctx: GradeContext) -> dict:
    g = ctx.task.grader
    # hidden tests that compare against the starting tree read the start commit from the environment
    ctx.task.grader = {**g, "test_env": {**g.get("test_env", {}), "DOKIMI_EVAL_START_SHA": ctx.start_sha}}
    try:
        out = g_hidden_tests(ctx)
    finally:
        ctx.task.grader = g
    logs = {}
    for name, cmd in {**CHECKS, "compat_ok": f"python -m dokimi.schema_compat --base {shlex.quote(ctx.start_sha)}"}.items():
        p = _sh(ctx, cmd)
        out[name] = p.ok
        logs[name] = p.out[-3000:]
    out["check_logs"] = logs
    out["pass"] = evaluate_gate(out, ctx.task.gate or ["strict_pass"])
    return out


CLAIMS = {"answer_names_claim_1": (r"parser\.py", r"tokens\.py"),               # AGENTS.md:9 src/tinycalc/parser.py
          "answer_names_claim_2": (r"releasing\.md", r"(?<![\w-])release\.md")}  # AGENTS.md:23 docs/releasing.md
BROKEN = re.compile(r"n[o']t\s+exist|non-?existent|\bis\s+no\b|missing|absent|not\s+found|no\s+such|broken|"
                    r"dead|stale|wrong|incorrect|outdated|invalid|fail", re.I)
LOAD_MANIFEST = ("from dokimi.repos.manifest import load_manifest; from dokimi.repos.source import WorkTree; "
                 "load_manifest(WorkTree('../examples/toy-repo'), 'repo-eval.yaml')")


def _calls_broken(answer: str, claim: str, fix: str) -> bool:
    """A paragraph or top-level list item names *claim* and says it is broken or gives *fix*."""
    blocks = re.split(r"\n\s*\n|\n(?=[-*]\s|\d+\.\s)", answer)
    return any(re.search(claim, b, re.I) and (BROKEN.search(b) or re.search(fix, b, re.I)) for b in blocks)


def grade_dokimi_scan(ctx: GradeContext) -> dict:
    toy = ctx.workspace / "examples" / "toy-repo"
    out: dict = {"manifest_exists": (toy / "repo-eval.yaml").is_file(), "manifest_loads": False,
                 "scan_covers_agents_md": False, "scan_reports_both_fails": False}
    if out["manifest_exists"]:
        out["manifest_loads"] = _sh(ctx, f"python -c {shlex.quote(LOAD_MANIFEST)}").ok
        p = _sh(ctx, "python cli.py repo scan ../examples/toy-repo --manifest repo-eval.yaml --json 2>/dev/null")
        if p.ok:
            try:
                payload = json.loads(p.out)
            except ValueError:
                payload = {}
            out["scan_covers_agents_md"] = "AGENTS.md" in payload.get("files", [])
            fails = {(c.get("file"), str(c.get("claim"))) for c in payload.get("claims", [])
                     if str(c.get("status", "")).startswith("fail")}
            out["scan_reports_both_fails"] = {("AGENTS.md", "src/tinycalc/parser.py"),
                                              ("AGENTS.md", "docs/releasing.md")} <= fails
    out["manifest_ok"] = out["manifest_exists"] and out["manifest_loads"] and out["scan_covers_agents_md"]
    for name, (claim, fix) in CLAIMS.items():
        out[name] = _calls_broken(ctx.answer, claim, fix)
        out[name.replace("names_claim", "fix")] = bool(re.search(fix, ctx.answer, re.I))
    agents_md = (toy / "AGENTS.md").read_text(encoding="utf-8", errors="replace") if (toy / "AGENTS.md").is_file() else ""
    out["toy_agents_md_unchanged"] = "docs/releasing.md" in agents_md and "src/tinycalc/parser.py" in agents_md
    out["ran_repo_scan"] = any(e.get("kind") == "command" and re.search(r"\brepo\s+scan\b", str(e.get("text", "")))
                               for e in ctx.events)
    out["pass"] = evaluate_gate(out, ctx.task.gate or ["manifest_ok", "answer_names_claim_1", "answer_names_claim_2"])
    return out


def oracle_dokimi_scan(ctx: GradeContext) -> str:
    apply_reference(ctx)
    return ctx.task.file("oracle_answer.md").read_text(encoding="utf-8")


GRADERS = {"dokimi_code": grade_dokimi_code, "dokimi_scan": grade_dokimi_scan}
ORACLES = {"dokimi_code": o_hidden_tests, "dokimi_scan": oracle_dokimi_scan}
