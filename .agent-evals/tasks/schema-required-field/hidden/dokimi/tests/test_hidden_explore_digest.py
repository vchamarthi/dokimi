# Hidden test for the schema-required-field task: explore_runs task entries carry a required `digest`,
# added as a new schema version. The grader sets DOKIMI_EVAL_START_SHA to the commit the agent started from.
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import dokimi.artifacts as a

PROJ = Path.cwd()
WS = PROJ.parent

TASK_YAML = """id: find_scorer
product: demo
query: {query}
references:
  - path: src/score.py
    lines: [{start}, 40]
  - path: docs/scoring.md
"""


def test_new_version_registered():
    cur = a.SCHEMA_VERSIONS.get("explore_runs", "")
    m = re.fullmatch(r"explore_runs\.v(\d+)", cur)
    assert m and int(m.group(1)) >= 2, cur
    path = a.SCHEMA_DIR / f"{cur}.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema.get("properties", {}).get("schema_version", {}).get("const") == cur


def test_old_superseded():
    assert "explore_runs.v1" in a.SUPERSEDED_VERSIONS.get("explore_runs", ())
    assert (a.SCHEMA_DIR / "explore_runs.v1.json").exists()


def test_old_loads(tmp_path):
    start = os.environ["DOKIMI_EVAL_START_SHA"]
    raw = subprocess.run(["git", "-C", str(WS), "show", f"{start}:dokimi/tests/fixtures/artifacts/explore_runs.v1.json"],
                         capture_output=True, text=True, check=True).stdout
    p = tmp_path / "old.json"
    p.write_text(json.dumps(json.loads(raw)), encoding="utf-8")
    a.load_artifact("explore_runs", p)


def _explore_run(tmp, tag):
    out = tmp / f"{tag}.json"
    p = subprocess.run(
        [sys.executable, str(PROJ / "cli.py"), "explore", "run", "--all",
         "--tasks-root", str(tmp / "tasks"), "--repo-root", str(tmp / "repo"),
         "--arms", "oracle,empty", "--baseline-arm", "empty",
         "--output-dir", str(tmp / "out" / tag), "--out-json", str(out)],
        cwd=tmp, capture_output=True, text=True, timeout=300, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert p.returncode == 0, f"explore run {tag}: {p.stdout[-300:]} {p.stderr[-1500:]}"
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("explore_digest")
    (tmp / "tasks").mkdir()
    (tmp / "repo" / "src").mkdir(parents=True)
    (tmp / "repo" / "docs").mkdir(parents=True)
    (tmp / "repo" / "src" / "score.py").write_text("\n".join(f"x{i} = {i}" for i in range(60)))
    (tmp / "repo" / "docs" / "scoring.md").write_text("# Scoring\n")
    y = tmp / "tasks" / "find_scorer.yaml"
    q = "Where is the localization score computed?"
    y.write_text(TASK_YAML.format(query=q, start=10), encoding="utf-8")
    r1 = _explore_run(tmp, "r1")
    r2 = _explore_run(tmp, "r2")
    y.write_text(TASK_YAML.format(query=q, start=12), encoding="utf-8")
    r3 = _explore_run(tmp, "r3")
    y.write_text(TASK_YAML.format(query=q + " And where is it tested?", start=10), encoding="utf-8")
    r4 = _explore_run(tmp, "r4")
    return r1, r2, r3, r4


@pytest.mark.timeout(600)
def test_writer_emits_digest(runs):
    d1, d2, d3, d4 = ((r.get("tasks") or [{}])[0].get("digest") for r in runs)
    assert all(isinstance(d, str) and d for d in (d1, d2, d3, d4)), (d1, d2, d3, d4)
    assert runs[0].get("schema_version") == a.SCHEMA_VERSIONS["explore_runs"]
    assert d1 == d2 and d3 != d1 and d4 != d1 and d4 != d3, (d1, d2, d3, d4)


@pytest.mark.timeout(600)
def test_missing_rejected(runs):
    art = runs[0]
    assert art.get("tasks")
    a.validate_artifact("explore_runs", art)  # the complete artifact is valid
    broken = json.loads(json.dumps(art))
    broken["tasks"][0].pop("digest", None)
    with pytest.raises(a.ArtifactValidationError):
        a.validate_artifact("explore_runs", broken)
