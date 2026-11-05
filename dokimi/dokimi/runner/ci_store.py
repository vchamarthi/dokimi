# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Atomic, compare-and-swap behavioral baselines, isolated by subject and model."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from dokimi.artifacts import validate_artifact
from dokimi.runner.ci_policy import bundle_digest, gate_bundle

_SUBJECT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?\Z")
_STABLE_IDENTITY = ("subject", "kind", "suite_digest", "framework_digest", "model", "harness", "grader_digest")


def _subject_key(subject: str) -> str:
    if not isinstance(subject, str) or _SUBJECT.fullmatch(subject) is None:
        raise ValueError("unsafe baseline subject; use a slug or namespace/slug")
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def _model_key(model: str) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("a behavioral baseline requires a resolved model")
    return hashlib.sha256(model.encode("utf-8")).hexdigest()


def _safe(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"baseline symlink is not permitted: {path}")
    return path


def _subject_dir(store: Path, subject: str) -> Path:
    key = _subject_key(subject)
    return _safe(_safe(Path(store)) / key)


def _current_path(store: Path, subject: str, model: str) -> Path:
    directory = _safe(_subject_dir(store, subject) / _model_key(model))
    return _safe(directory / "current.json")


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    _safe(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ValueError("baseline writer lock is held; no baseline was changed") from exc
    try:
        yield
    finally:
        lock.rmdir()


def _read(path: Path) -> dict[str, Any]:
    _safe(path)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        validate_artifact("ci_baseline", receipt)
    except (OSError, UnicodeError, ValueError, AttributeError) as exc:
        raise ValueError(f"invalid baseline receipt at {path}: {exc}") from exc
    if receipt["baseline_id"] != bundle_digest(receipt["bundle"]):
        raise ValueError("baseline receipt digest does not match evidence")
    verdict = gate_bundle(receipt["bundle"], receipt["policy"])
    if not verdict["promotion_eligible"]:
        raise ValueError(f"baseline no longer satisfies its recorded policy: {verdict['reason']}")
    return {**receipt, "verdict": verdict}


def _check_identity(receipt: dict[str, Any], subject: str, model: str | None) -> None:
    identity = receipt["bundle"]["identity"]
    if identity["subject"] != subject:
        raise ValueError("baseline identity does not match requested subject")
    if model is not None and identity["model"] != model:
        raise ValueError("baseline identity does not match requested model")


def load_baseline(store: Path, subject: str, *, model: str | None = None) -> dict[str, Any]:
    """Load a validated baseline; a subject with several models requires an explicit model."""
    directory = _subject_dir(store, subject)
    if model is not None:
        path = _current_path(store, subject, model)
    else:
        matches = list(directory.glob("*/current.json"))
        if len(matches) != 1:
            raise ValueError("baseline is missing or model is ambiguous; specify a model")
        path = _safe(_safe(matches[0].parent) / matches[0].name)
    receipt = _read(path)
    _check_identity(receipt, subject, model)
    return receipt


def _check_previous(path: Path, bundle: dict[str, Any], policy: dict[str, Any],
                    expected: str | None, allow_revalidation: bool) -> str | None:
    if not path.exists():
        if expected is not None:
            raise ValueError("compare-and-swap expected a previous baseline, but none exists")
        return None
    previous = _read(path)
    if expected is None or previous["baseline_id"] != expected:
        raise ValueError("compare-and-swap previous baseline does not match")
    identity, prior = bundle["identity"], previous["bundle"]["identity"]
    fixed = ("subject", "kind", "model") if allow_revalidation else _STABLE_IDENTITY
    if any(identity[key] != prior[key] for key in fixed):
        raise ValueError("baseline identity drift requires a separate baseline store or explicit revalidation")
    if bundle["source"]["repository"] != previous["bundle"]["source"]["repository"]:
        raise ValueError("baseline repository identity drift")
    if policy != previous["policy"] and not allow_revalidation:
        raise ValueError("baseline policy drift requires explicit revalidation in a separate store")
    return previous["baseline_id"] if allow_revalidation else None


def _publish(path: Path, receipt: dict[str, Any]) -> None:
    validate_artifact("ci_baseline", receipt)
    body = json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n"
    fd, name = tempfile.mkstemp(prefix=".baseline-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _safe(path)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def record_bundle(bundle: dict[str, Any], policy: dict[str, Any], store: Path, *,
                  expected_previous: str | None = None, allow_revalidation: bool = False) -> dict[str, Any]:
    """Promote a live pass atomically; ``None`` means create-only, never blind overwrite.

    A new model creates its own namespace. Suite, harness, framework, grader or policy drift
    requires ``allow_revalidation=True`` and fresh passing evidence; compare-and-swap still applies.
    """
    verdict = gate_bundle(bundle, policy)
    if not verdict["promotion_eligible"]:
        raise ValueError(f"cannot promote evidence that is not a behavioral pass: {verdict['reason']}")
    identity = bundle["identity"]
    path = _current_path(Path(store), identity["subject"], identity["model"])
    receipt = {"schema_version": "ci_baseline.v1", "baseline_id": bundle_digest(bundle),
               "bundle": bundle, "policy": policy}
    with _locked(path.parent):
        revalidated = _check_previous(path, bundle, policy, expected_previous, allow_revalidation)
        if revalidated is not None:
            receipt["revalidated_from"] = revalidated
        _publish(path, receipt)
    return {**receipt, "verdict": verdict}
