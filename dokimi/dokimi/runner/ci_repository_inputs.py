# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Pinned guidance, manifests, and grader suites for repository CI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dokimi.repos import RepoManifest, RepoTree, WorkTree, load_manifest, match_glob, parse_manifest
from dokimi.repos.graders import graders_digest
from dokimi.repos.workspace import export_eval_dir
from dokimi.utils import tree_digest

from .ci_catalog_inputs import fingerprint, framework_digest
from .plan import plan_cells

DEFAULT_GLOBS = ("**/AGENTS.md", "**/CLAUDE.md", ".github/copilot-instructions.md", ".github/instructions/*.md")


def repository_manifest(tree: RepoTree, manifest: str | None, *, allow_default: bool = False) -> RepoManifest:
    path = manifest or (".github/dokimi/repo.yaml" if tree.is_file(".github/dokimi/repo.yaml")
                        else ".agent-eval/repo-eval.yaml")
    if not tree.is_file(path) and allow_default and manifest is None:
        return parse_manifest({"schema": "repo_manifest.v1", "id": "repo",
                               "guidance": {"globs": list(DEFAULT_GLOBS)}}, path)
    return load_manifest(tree, path)


def guidance_digest(tree: RepoTree, manifest: RepoManifest) -> str:
    paths = [p for p in tree.files() if any(match_glob(g, p) for g in manifest.guidance_globs)]
    return fingerprint({path: tree.read_text(path) for path in paths})


def suite_inputs(tree: RepoTree, manifest: RepoManifest, destination: Path) -> tuple[str, str]:
    exported = export_eval_dir(tree, manifest, destination)
    eval_digest = tree_digest(exported, strict=True)
    suite_digest = fingerprint({"manifest": manifest.to_dict(), "content": eval_digest,
                                "builtin_graders": graders_digest()})
    return suite_digest, eval_digest


def grader_digest(suite: str, runtime: dict[str, Any]) -> str:
    return fingerprint({"suite": suite, "builtin_graders": graders_digest(), "runtime": runtime})


def repository_identity(tree: RepoTree, manifest: RepoManifest, suite: str, *, model: str | None,
                        harness: str, runtime: dict[str, Any]) -> dict[str, Any]:
    return {"subject": "repo", "kind": "repo", "content_digest": guidance_digest(tree, manifest),
            "suite_digest": suite, "framework_digest": framework_digest(), "model": model,
            "harness": harness, "grader_digest": grader_digest(suite, runtime)}


def repository_plan(identity: dict[str, Any], tasks: list[str], *, sha: str | None, reps: int,
                    live: bool, cap: float | None = None) -> dict[str, Any]:
    cells = plan_cells(["none", "approved", "candidate"] if live else ["candidate"],
                       [identity["model"]], tasks, reps)
    return {"schema_version": "plan.v1", "subjects": [{"id": "repo", "kind": "repo", "sha": sha,
                                                        "fingerprint": identity["content_digest"]}],
            "suites": [{"id": "repo:approved", "hash": identity["suite_digest"],
                        "reasons": ["base revision fixes tasks, image, manifest, and graders",
                                    "agent-network bridge permits broad network access" if live else "static guidance claims"]}],
            "models": [identity["model"]], "harnesses": [identity["harness"]],
            "grader": {"id": "repository-contract", "digest": identity["grader_digest"]},
            "varied_variable": "repository-guidance", "cells": cells,
            "budget": {"cells": len(cells), "agent_calls": len(cells) if live else 0, "cost_usd": cap}}


def worktree_suite(root: Path, manifest_path: str | None, destination: Path) -> tuple[RepoManifest, str]:
    tree = WorkTree(root)
    manifest = repository_manifest(tree, manifest_path)
    return manifest, suite_inputs(tree, manifest, destination)[0]
