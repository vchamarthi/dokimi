# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""The explicit check, measure, gate and record loop for CI consumers."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from dokimi.artifacts import load_artifact, save_artifact
from dokimi.exit_codes import usage_exit


def _positive(value: str) -> float:
    import math

    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def _source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("root", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--base-root", type=Path)
    parser.add_argument("--changed-from", default=None)
    parser.add_argument("--subject-kind", choices=("catalog", "repo"), default="catalog")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--manifest")
    parser.add_argument("--expected-head")
    parser.add_argument("--expected-base")
    parser.add_argument("--source-repository")


def _runtime(parser: argparse.ArgumentParser, positive_int) -> None:
    _source(parser)
    parser.add_argument("--max-usd", type=_positive)
    parser.add_argument("--max-calls", type=positive_int)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--judge-provider", required=True)
    parser.add_argument("--reps", type=positive_int, default=3)
    parser.add_argument("--subject", action="append")
    parser.add_argument("--harness", default="arms-runner")


def register(sub, positive_int) -> None:
    group = sub.add_parser("ci", help="Pinned CI checks, measured policy gates and behavioral baselines")
    commands = group.add_subparsers(dest="ci_command", required=True)
    check = commands.add_parser("check", help="Deterministic checks, no model calls")
    _source(check)
    run = commands.add_parser("run", help="Measure one subject on an approved suite with an explicit cap")
    _runtime(run, positive_int)
    gate = commands.add_parser("gate", help="Recompute a policy decision from complete run evidence")
    gate.add_argument("run_file", type=Path)
    gate.add_argument("--policy", type=Path)
    gate.add_argument("--out", type=Path)
    report = commands.add_parser("report", help="Render a scoped, sanitized CI report")
    report.add_argument("run_file", type=Path)
    report.add_argument("--policy", type=Path)
    report.add_argument("--out", type=Path, required=True)
    record = commands.add_parser("record", help="Promote matching, passing live evidence with compare-and-swap")
    record.add_argument("run_file", type=Path)
    record.add_argument("--root", type=Path, required=True)
    record.add_argument("--store", type=Path, required=True)
    record.add_argument("--expected-head", required=True)
    record.add_argument("--expect-previous", required=True, help="Previous baseline digest, or none for first record")
    record.add_argument("--policy", type=Path)
    record.add_argument("--revalidated", action="store_true")
    revalidate = commands.add_parser("revalidate", help="Remeasure a recorded subject at an explicit model")
    _runtime(revalidate, positive_int)
    revalidate.add_argument("--baseline", type=Path, required=True)
    for parser in (check, run, gate, report, record, revalidate):
        parser.set_defaults(func=cmd_ci)


def _provenance(args: argparse.Namespace) -> dict[str, Any]:
    return {"source_repository": args.source_repository, "expected_head": args.expected_head,
            "expected_base": args.expected_base}


def _check(args: argparse.Namespace) -> dict[str, Any]:
    from dokimi.runner.ci_pipeline import run_check

    return run_check(args.root, args.out_dir, base=args.changed_from or None, policy_path=args.policy,
                     base_root=args.base_root, config_path=args.config,
                     subject_kind=args.subject_kind, manifest=args.manifest, **_provenance(args))


def _run(args: argparse.Namespace) -> dict[str, Any]:
    from dokimi.runner.ci_pipeline import run_live

    return run_live(args.root, args.out_dir, base_root=args.base_root, base=args.changed_from or None,
                    policy_path=args.policy, config_path=args.config, subjects=args.subject,
                    model=args.model, provider=args.provider, judge_model=args.judge_model,
                    judge_provider=args.judge_provider, reps=args.reps, harness=args.harness,
                    max_usd=args.max_usd, max_calls=args.max_calls, subject_kind=args.subject_kind, manifest=args.manifest,
                    **_provenance(args))


def _gate(args: argparse.Namespace) -> dict[str, Any]:
    from dokimi.runner.ci_io import read_bundle, read_policy
    from dokimi.runner.ci_policy import gate_bundle

    verdict = gate_bundle(read_bundle(args.run_file), read_policy(args.policy))
    if args.out:
        save_artifact("ci_gate", verdict, args.out)
    return verdict


def _report(args: argparse.Namespace) -> None:
    from dokimi.runner.ci_io import read_bundle, read_policy, render
    from dokimi.runner.ci_policy import gate_bundle

    bundle = read_bundle(args.run_file)
    verdict = gate_bundle(bundle, read_policy(args.policy))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(bundle, verdict), encoding="utf-8")


def _record(args: argparse.Namespace) -> dict[str, Any]:
    from dokimi.runner.ci_io import read_bundle, read_policy
    from dokimi.runner.ci_store import record_bundle

    bundle = read_bundle(args.run_file)
    if args.expected_head != bundle["source"]["head_sha"]:
        raise ValueError("record expected-head does not match the measured candidate")
    _verify_identity(args.root, bundle)
    previous = None if args.expect_previous == "none" else args.expect_previous
    receipt = record_bundle(bundle, read_policy(args.policy), args.store, expected_previous=previous,
                             allow_revalidation=args.revalidated)
    return {"baseline_id": receipt["baseline_id"], "status": "pass", "exit_code": 0}


def _revalidate(args: argparse.Namespace) -> dict[str, Any]:
    from dokimi.runner.ci_policy import bundle_digest, gate_bundle

    from dokimi.runner.ci_io import output_directory

    output_directory(args.root, args.out_dir)
    receipt = load_artifact("ci_baseline", args.baseline)
    bundle = receipt["bundle"]
    if receipt["baseline_id"] != bundle_digest(bundle) or not gate_bundle(bundle, receipt["policy"])["promotion_eligible"]:
        raise ValueError("revalidation requires a matching, passing baseline receipt")
    _verify_identity(args.root, bundle, content_only=True)
    if args.subject and args.subject != [bundle["identity"]["subject"]]:
        raise ValueError("revalidation subject differs from the recorded subject")
    args.subject = [bundle["identity"]["subject"]]
    args.subject_kind = "repo" if bundle["identity"]["kind"] == "repo" else "catalog"
    return _run(args)


def _verify_identity(root: Path, bundle: dict[str, Any], *, content_only: bool = False) -> None:
    if bundle["identity"]["kind"] == "repo":
        from dokimi.runner.ci_repository import verify_repository_identity

        verify_repository_identity(root, bundle, content_only=content_only)
    else:
        from dokimi.runner.ci_catalog import verify_record_identity

        verify_record_identity(root, bundle, content_only=content_only)


def cmd_ci(args: argparse.Namespace) -> None:
    from dokimi.runner.ci_io import print_json

    operations = {"check": _check, "run": _run, "gate": _gate, "record": _record, "revalidate": _revalidate}
    try:
        if args.ci_command == "report":
            _report(args)
            return
        verdict = operations[args.ci_command](args)
    except (ValueError, OSError, RecursionError) as exc:
        usage_exit(f"ci {args.ci_command}: {exc}")
    print_json(verdict)
    raise SystemExit(verdict["exit_code"])
