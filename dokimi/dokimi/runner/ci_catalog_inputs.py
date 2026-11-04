# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Immutable prompt-suite inputs for catalog CI, without executing content."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dokimi.external.catalog_eval_inputs import Inputs, digest
from dokimi.skills.loader import load_skill
from dokimi.task_formats.imports.skill_creator import import_file


def fingerprint(value: Any) -> str:
    return "sha256:" + digest(value)


def framework_digest() -> str:
    package = Path(__file__).resolve().parents[1]
    files = {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(package.rglob("*")) if p.is_file() and p.suffix in {".py", ".json", ".yaml"}}
    return fingerprint(files)


@dataclass
class CatalogSubject:
    """Resolved content and the suite all arms share; source files are never re-read live."""

    name: str
    root: Path
    suite_root: Path
    row: dict[str, Any]
    questions: list[dict[str, Any]]
    contexts: dict[str, str]
    skill_paths: dict[str, Path]
    tasks: list[dict[str, Any]]
    plan: dict[str, Any]
    identity: dict[str, Any]


def _validate_cases(data: Any, path: Path) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("evals"), list):
        raise ValueError(f"{path}: expected an evals list")
    rows = data["evals"]
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("prompt"), str) or not row["prompt"].strip():
            raise ValueError(f"{path}: every eval needs a nonempty prompt")
        qid = str(row.get("id", ""))
        if not qid or qid in ids:
            raise ValueError(f"{path}: missing or duplicate eval id {qid!r}")
        ids.add(qid)
        if row.get("files"):
            raise ValueError(f"{path}: files are unsupported by the answer-only CI harness")
    return rows


def questions(inputs: Inputs, skill_path: str) -> list[dict[str, Any]]:
    path = inputs.root / skill_path / "evals" / "evals.json"
    if not path.exists():
        return []
    rows = _validate_cases(json.loads(inputs.text(path)), path)
    imported = import_file(path)
    if len(imported.questions) != len(rows):
        raise ValueError(f"{path}: every eval needs expectations, assertions or expected_output")
    for source, question in zip(rows, imported.questions, strict=True):
        _literal_checks(source.get("checks"), question, path)
        question["metadata"].pop("source_path", None)
    inputs.verify()
    return imported.questions


def _literal_checks(checks: Any, question: dict, path: Path) -> None:
    if checks is None:
        return
    if not isinstance(checks, dict) or set(checks) - {"must_include", "must_not_include"}:
        raise ValueError(f"{path}: unsupported checks (only must_include and must_not_include)")
    for key, values in checks.items():
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f"{path}: {key} must be a list of nonempty strings")
        question["grading"]["checks"].extend(
            {"type": "match", "values": [v], "location": "any", "ignore_case": True,
             "ignore_punctuation": False, "negate": key == "must_not_include"} for v in values)


def context(inputs: Inputs, path: str) -> str:
    before = inputs.bundle(inputs.root / path)
    skill = load_skill(inputs.root / path)
    if len(skill.body.strip()) > 12000:
        raise ValueError(f"{path}: skill body exceeds the arms-runner context limit (12000 characters)")
    result = skill.as_context()
    inputs.verify()
    if inputs.bundle(inputs.root / path) != before:
        raise ValueError(f"{path}: content changed while loading")
    return result
