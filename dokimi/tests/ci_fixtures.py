# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Synthetic, paired CI measurements; no model or network calls."""

from dokimi.runner.plan import plan_cells, plan_digest
from dokimi.runner.ci_policy import DEFAULT_POLICY, policy_digest

DIGEST = "sha256:" + "a" * 64


def ci_bundle(*, tasks=6, reps=1, mode="live", scores=None):
    arms = ["none", "approved", "candidate"] if mode == "live" else ["candidate"]
    cells = plan_cells(arms, ["model-a"], [f"task-{i}" for i in range(tasks)], reps)
    plan = {
        "schema_version": "plan.v1",
        "subjects": [{"id": "demo", "kind": "skill", "sha": "1" * 40, "fingerprint": DIGEST}],
        "suites": [{"id": "suite", "hash": DIGEST, "reasons": ["changed subject"]}],
        "models": ["model-a"], "harnesses": ["fixture"],
        "grader": {"id": "verifier", "digest": DIGEST}, "varied_variable": "skill",
        "cells": cells, "budget": {"cells": len(cells), "cost_usd": 10, "wall_sec": 1000},
    }
    defaults = {"none": 0.2, "approved": 0.7, "candidate": 0.8}
    defaults.update(scores or {})
    return {
        "schema_version": "ci_run.v1", "mode": mode, "plan": plan,
        "policy_digest": policy_digest(DEFAULT_POLICY),
        "result": {"schema_version": "result.v1", "plan_digest": plan_digest(plan),
                   "cells": [{"cell_id": c["cell_id"], "status": "pass", "model": "model-a"} for c in cells]},
        "source": {"repository": "owner/repository", "head_sha": "1" * 40, "base_sha": "2" * 40},
        "identity": {"subject": "demo", "kind": "skill", "content_digest": DIGEST,
                     "suite_digest": DIGEST, "framework_digest": DIGEST, "model": "model-a",
                     "harness": "fixture", "grader_digest": DIGEST},
        "observations": [{"cell_id": c["cell_id"], "score": defaults[c["arm"]], "cost_usd": 0.01,
                          "elapsed_sec": 1, "discovered": c["arm"] != "none"} for c in cells],
        "budget_stopped": False,
    }
