# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Workflow summaries and artifact uploads require receipts from this run attempt."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("workflow,job", [("pr", "check"), ("pr", "run"), ("revalidate", "revalidate")])
@pytest.mark.parametrize("source,fresh", [({"run_id":7,"run_attempt":2},True),
    ({"run_id":6,"run_attempt":2},False), ({"run_id":7,"run_attempt":1},False), ({},False), (None,False)])
def test_only_current_attempt_receipts_can_be_summarized_or_uploaded(tmp_path: Path, workflow: str,
                                                                   job: str, source: dict | None, fresh: bool):
    wf = yaml.safe_load((ROOT / f".github/workflows/dokimi-{workflow}.yml").read_text())
    steps = wf["jobs"][job]["steps"]
    report = next(s for s in steps if s.get("name") == "Append report")
    upload = next(s for s in steps if s.get("name") == "Upload run JSON only")
    receipt, rendered = tmp_path / "run.json", tmp_path / "report.md"
    if source is not None:
        receipt.write_text(json.dumps({"source":source,"result":{"status":"fail"}}))
    rendered.write_text("Current failure report" if fresh else "Stale earlier success")
    summary, output = tmp_path / "summary", tmp_path / "output"
    summary.write_text("Existing summary\n")
    output.write_text("")
    env = {**os.environ, "PATH":f"{Path(sys.executable).parent}:{os.environ['PATH']}", "REPORT":str(rendered), "RUN_FILE":str(receipt), "GITHUB_RUN_ID":"7",
           "GITHUB_RUN_ATTEMPT":"2", "GITHUB_STEP_SUMMARY":str(summary), "GITHUB_OUTPUT":str(output)}
    done = subprocess.run(["bash", "-euo", "pipefail", "-c", report["run"]], env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert summary.read_text() == "Existing summary\n" + ("Current failure report" if fresh else "")
    assert ("fresh=true" in output.read_text()) is fresh
    assert f"steps.{report['id']}.outputs.fresh == 'true'" in upload["if"]
