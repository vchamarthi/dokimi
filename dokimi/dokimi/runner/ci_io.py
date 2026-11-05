# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Dedicated CI outputs and schema-checked, explicitly scoped human reports."""

from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from dokimi.artifacts import load_artifact, save_artifact, validate_artifact
from dokimi.runner.ci_policy import DEFAULT_POLICY, gate_bundle

FILES = frozenset({"run.json", "plan.json", "result.json", "gate.json", "report.md", "catalog.json", "repo-readiness.json"})
RUN_FILES = FILES - {"catalog.json", "repo-readiness.json"}


def output_directory(root: Path, out: Path) -> Path:
    if any(p.is_symlink() for p in (out, *out.parents)):
        raise ValueError("CI output directory must not traverse a symlink")
    root, out = root.resolve(), out.resolve()
    if root.is_relative_to(out) or out.is_relative_to(root):
        raise ValueError("CI output must be outside the source checkout and its ancestors")
    if out.exists():
        if not out.is_dir() or any(p.name not in FILES or not p.is_file() or p.is_symlink() for p in out.iterdir()):
            _invalidate_prior_run(out)
            raise ValueError("CI output must be a dedicated empty or earlier CI receipt directory")
        for path in out.iterdir():
            path.unlink()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _invalidate_prior_run(out: Path) -> None:
    marker = out / "run.json"
    if not out.is_dir() or marker.is_symlink() or not marker.is_file():
        return
    try:
        if marker.stat().st_size > 2 * 1024 * 1024:
            return
        load_artifact("ci_run", marker)
    except (ValueError, OSError, UnicodeError):
        return
    marker.unlink()


def read_policy(path: Path | None = None) -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY) if path is None else yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_artifact("ci_policy", policy)
    return policy


def read_bundle(path: Path) -> dict[str, Any]:
    return load_artifact("ci_run", path / "run.json" if path.is_dir() else path)


def _text(value: Any) -> str:
    text = html.escape(str(value), quote=False).replace("@", "＠").replace("\r", " ").replace("\n", " ")
    for character, entity in (("`", "&#96;"), ("[", "&#91;"), ("]", "&#93;"), ("|", "&#124;")):
        text = text.replace(character, entity)
    return text


def render(bundle: dict[str, Any], verdict: dict[str, Any]) -> str:
    identity, source = bundle["identity"], bundle["source"]
    lines = ["# Dokimi CI", "", f"**{_text(verdict['status']).upper()}** — {_text(verdict['scope'])}", "",
             f"Subject: `{_text(identity['subject'])}`", f"Source: `{_text(source['repository'])}@{source['head_sha']}`",
             f"Comparison base: `{source['base_sha']}`", "", _text(verdict["reason"]), ""]
    if bundle["mode"] == "static":
        lines.append("Deterministic checks only. Task outcomes, skill utility and hardware performance were not measured.")
    else:
        lines.extend(_text(reason) for suite in bundle["plan"]["suites"] for reason in suite["reasons"])
        lines.append("")
        lines.extend([f"Model: `{_text(identity['model'])}`; harness: `{_text(identity['harness'])}`", "",
                      "| Task | Candidate minus approved | Candidate minus none |", "|---|---:|---:|"])
        for effect in verdict.get("task_effects", []):
            lines.append(f"| {_text(effect.get('task', ''))} | {_text(effect.get('regression', ''))} | {_text(effect.get('lift', ''))} |")
    lines.extend(["", f"Planned cells: {len(bundle['plan']['cells'])}; recorded results: {len(bundle['result']['cells'])}.",
                  f"Budget stopped: {bundle['budget_stopped']}. Promotion eligible: {verdict['promotion_eligible']}.", ""])
    return "\n".join(lines)


def write_bundle(out: Path, bundle: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    verdict = gate_bundle(bundle, policy)
    with tempfile.TemporaryDirectory(prefix="dokimi-ci-save-", dir=out.parent) as tmp:
        stage = Path(tmp)
        for kind, data, name in (("plan", bundle["plan"], "plan.json"),
                                 ("result", bundle["result"], "result.json"),
                                 ("ci_gate", verdict, "gate.json"), ("ci_run", bundle, "run.json")):
            save_artifact(kind, data, stage / name)
        (stage / "report.md").write_text(render(bundle, verdict), encoding="utf-8")
        for name in sorted(RUN_FILES - {"run.json"}) + ["run.json"]:
            os.replace(stage / name, out / name)
    return verdict


def print_json(value: dict[str, Any], out: Path | None = None) -> None:
    text = json.dumps(value, indent=2, allow_nan=False) + "\n"
    if out is None:
        print(text, end="")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
