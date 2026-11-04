# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Execute the privileged publisher against fake GitHub metadata and hostile ZIPs."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HEAD = "a" * 40
BASE = "b" * 40
CONTROLLER = "c" * 40

_HARNESS = r"""
const fs = require('fs');
const c=JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const calls=[];
const github={rest:{actions:{getWorkflowRun:async()=>({data:c.run}),getWorkflow:async()=>({data:{id:19,path:'.github/workflows/caller.yml'}}),listWorkflowRunArtifacts:'artifacts',downloadArtifact:async()=>({data:fs.readFileSync(c.zip)})},pulls:{get:async()=>({data:c.pr})},issues:{listComments:'comments',createComment:async x=>calls.push(['comment',x]),updateComment:async x=>calls.push(['comment',x])}},paginate:async f=>f==='artifacts'?c.artifacts:[]};
const context={repo:{owner:'owner',repo:'repo'},payload:{}};
const core={warning:m=>calls.push(['warning',m]),info:()=>{},setFailed:m=>calls.push(['failed',m]),setOutput:(k,v)=>calls.push(['output',{[k]:v}])};
const script=JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
new (Object.getPrototypeOf(async()=>{}).constructor)('github','context','core','require',script)(github,context,core,require).then(()=>console.log(JSON.stringify(calls)),e=>console.log(JSON.stringify([['failed',e.message]])));
"""


def _run(tmp_path: Path, *, run_changes=None, pr_changes=None, result_changes=None,
         members=None, artifact_changes=None, controller="", merged=False) -> list:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    wf = yaml.safe_load((ROOT / ".github/workflows/dokimi-publish.yml").read_text())
    script = wf["jobs"]["publish"]["steps"][0]["with"]["script"]
    run = {"id":7,"run_attempt":1,"repository":{"full_name":"owner/repo"},"head_repository":{"full_name":"owner/fork"},
           "head_sha":HEAD,"head_branch":"topic","workflow_id":19,"event":"pull_request","status":"completed",
           "path":".github/workflows/caller.yml","pull_requests":[{"number":12,"head":{"sha":HEAD},"base":{"sha":BASE}}],
           "html_url":"https://github.com/owner/repo/actions/runs/7", **(run_changes or {})}
    pr = {"number":12,"state":"open","head":{"sha":HEAD,"repo":{"full_name":"owner/fork"}},
          "base":{"sha":BASE,"repo":{"full_name":"owner/repo"}}, **(pr_changes or {})}
    result = {"schema_version":"ci_run.v1","source":{"repository":"owner/repo","head_sha":HEAD,"base_sha":BASE},
              "mode":"static","policy_digest":"sha256:"+"d"*64,
              "identity":{"subject":"catalog","kind":"catalog","content_digest":"sha256:"+"d"*64,
                          "suite_digest":"sha256:"+"d"*64,"framework_digest":"sha256:"+"d"*64,
                          "grader_digest":"sha256:"+"d"*64,"model":"static","harness":"static"},
              "plan":{"schema_version":"plan.v1","subjects":[],"suites":[{"id":"s","hash":"d","reasons":[]}],
                      "models":[None],"harnesses":["static"],"grader":{"id":"static","digest":None},
                      "varied_variable":"content","cells":[{"cell_id":"x","arm":"static","model":None,"task":"x","rep":1}],
                      "budget":{"cells":1}},
              "result":{"schema_version":"result.v1","plan_digest":"sha256:"+"d"*64,"cells":[]},
              "observations":[],"budget_stopped":False, **(result_changes or {})}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        for name, data in (members or {"run.json":json.dumps(result)}).items():
            z.writestr(name,data)
    (tmp_path/"result.zip").write_bytes(buf.getvalue())
    case={"run":run,"pr":pr,"zip":str(tmp_path/"result.zip"),"artifacts":[{"id":3,"name":"dokimi-run-7-1","expired":False,
           "size_in_bytes":len(buf.getvalue()),"workflow_run":{"id":7}, **(artifact_changes or {})}]}
    for name, value in (("case.json",case),("script.json",script)):
        (tmp_path/name).write_text(json.dumps(value))
    (tmp_path/"harness.cjs").write_text(_HARNESS)
    env={**os.environ,"SOURCE_RUN":"7","SOURCE_ATTEMPT":"1","SOURCE_WORKFLOW":".github/workflows/caller.yml",
         "CONTROLLER_SHA":controller,"EXPECTED_POLICY":"sha256:"+"d"*64}
    if merged:
        env.update({"ALLOW_MERGED":"true","VERIFIED_OUT":str(tmp_path/"verified.json")})
    out=subprocess.run([node,str(tmp_path/"harness.cjs"),str(tmp_path/"case.json"),str(tmp_path/"script.json")],
                       env=env,capture_output=True,text=True,check=True)
    return json.loads(out.stdout)


def test_valid_pr_result_posts_only_an_informational_receipt(tmp_path: Path) -> None:
    calls=_run(tmp_path)
    comment=next(data for kind,data in calls if kind=="comment")
    assert comment["issue_number"]==12
    assert "informational" in comment["body"] and "efficacy" in comment["body"]
    assert "pass" not in comment["body"].lower()


@pytest.mark.parametrize("kwargs", [
    {"run_changes":{"repository":{"full_name":"attacker/repo"}}},
    {"run_changes":{"run_attempt":2}},
    {"run_changes":{"workflow_id":99}},
    {"run_changes":{"pull_requests":[]}},
    {"pr_changes":{"head":{"sha":"e"*40}}},
    {"pr_changes":{"base":{"sha":"e"*40,"repo":{"full_name":"owner/repo"}}}},
    {"artifact_changes":{"size_in_bytes":10**8}},
    {"result_changes":{"policy_digest":"sha256:"+"f"*64}},
    {"result_changes":{"source":{"repository":"other/repo","head_sha":HEAD,"base_sha":BASE}}},
    {"members":{"../run.json":"{}"}},
    {"members":{"run.json":"x"*300000}},
    {"members":{"run.json":"null"}},
    {"result_changes":{"observations":[{"cell_id":"x","score":2,"cost_usd":0,"elapsed_sec":0,"discovered":None}]}},
    {"result_changes":{"pr_number":999}},
    {"result_changes":{"runtime":{"requested_model":"m","provider":"openai","judge_model":"j",
        "judge_provider":"openai","reps":1,"billing":"subscription","transport":"claude-cli"}}},
    {"result_changes":{"runtime":{"requested_model":"m","provider":"claude-cli","judge_model":"j",
        "judge_provider":"claude-cli","reps":1,"billing":"subscription"}}},
])
def test_adversarial_results_never_post(tmp_path: Path, kwargs: dict) -> None:
    calls=_run(tmp_path,**kwargs)
    assert not [data for kind,data in calls if kind=="comment"]
    assert any(kind=="failed" for kind,_ in calls)


def test_paid_controller_receipt_uses_metadata_not_artifact_pr(tmp_path: Path) -> None:
    calls=_run(tmp_path,controller=CONTROLLER,run_changes={"event":"workflow_dispatch","head_sha":CONTROLLER,
       "head_repository":{"full_name":"owner/repo"},"pull_requests":[],
       "display_title":f"dokimi:model pr=12 head={HEAD} base={BASE}"},result_changes={"mode":"live"})
    assert next(data for kind,data in calls if kind=="comment")["issue_number"]==12


@pytest.mark.parametrize("controller,title", [("",f"dokimi:model pr=12 head={HEAD} base={BASE}"),
    ("e"*40,f"dokimi:model pr=12 head={HEAD} base={BASE}"),(CONTROLLER,"arbitrary")])
def test_dispatch_cannot_supply_its_own_controller_authority(tmp_path: Path,controller: str,title: str) -> None:
    calls=_run(tmp_path,controller=controller,run_changes={"event":"workflow_dispatch","head_sha":CONTROLLER,
        "display_title":title,"pull_requests":[]})
    assert not [data for kind,data in calls if kind=="comment"]


def test_publisher_and_verifier_use_the_packaged_run_schema() -> None:
    import re

    schema = json.loads((ROOT / "dokimi/dokimi/schemas/ci_run.v1.json").read_text())
    wf = yaml.safe_load((ROOT / ".github/workflows/dokimi-publish.yml").read_text())
    script = wf["jobs"]["publish"]["steps"][0]["with"]["script"]
    embedded = re.search(r"^const schema = (.+);$", script, re.MULTILINE)
    assert embedded is not None
    assert json.loads(embedded.group(1)) == schema
    module = (ROOT / "actions/verify-source-run/index.cjs").read_text()
    assert script.rstrip() in module


def test_merged_result_exports_only_api_authenticated_revisions(tmp_path: Path) -> None:
    calls = _run(tmp_path, merged=True, controller=CONTROLLER,
        run_changes={"event":"workflow_dispatch","head_sha":CONTROLLER,
                     "head_repository":{"full_name":"owner/repo"},"pull_requests":[],
                     "display_title":f"dokimi:model pr=12 head={HEAD} base={BASE}"},
        pr_changes={"state":"closed","merged":True,"merge_commit_sha":"e"*40},
        result_changes={"mode":"live"})
    assert [data for kind,data in calls if kind=="output"] == [{"merged-sha":"e"*40},{"head-sha":HEAD}]
    assert json.loads((tmp_path/"verified.json").read_text())["source"]["head_sha"] == HEAD
    assert not [data for kind,data in calls if kind=="comment"]


def test_unapproved_pr_job_cannot_be_promoted_after_merge(tmp_path: Path) -> None:
    calls = _run(tmp_path, merged=True,
                 pr_changes={"state":"closed","merged":True,"merge_commit_sha":"e"*40},
                 result_changes={"mode":"live"})
    assert any(kind=="failed" and "approved controller" in data for kind,data in calls)
    assert not (tmp_path/"verified.json").exists()
