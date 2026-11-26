# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""ci.yml's label-gated checks must run again when a maintainer changes a label.

The `Fragment for package changes` and `schema_compat` steps read
`skip-changelog` and `schema-break-ok` from github.event.pull_request.labels. That
list comes from the event that started the run, and a re-run reuses the same event,
so a label added after the push starts a run only if the pull_request trigger lists
the labeled and unlabeled types.

platforms.yml runs the whole suite on Windows and macOS on a PR only while it carries
`needs-windows`; its nightly run reports into one issue per platform.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CI = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


def _ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _trigger(wf: dict) -> dict:
    return wf.get("on") or wf[True]  # PyYAML reads the bare key `on` as True


def test_pull_request_trigger_fires_on_label_changes():
    pr = _trigger(_ci())["pull_request"] or {}
    types = set(pr.get("types") or [])
    assert {"opened", "synchronize", "reopened", "labeled", "unlabeled"} <= types


def _schema_compat_step() -> str:
    steps = _ci()["jobs"]["static"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == "schema_compat vs the PR base")


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="runs the step's bash")
@pytest.mark.parametrize(("status", "printed", "label", "step_exit"), [
    (0, "", "false", 0), (0, "", "true", 0),
    (1, "BREAKING a.v1.json x", "false", 1), (1, "BREAKING a.v1.json x", "true", 0),
    (1, "", "false", 1), (1, "", "true", 1),
    (2, "", "false", 2), (2, "", "true", 2),
])
def test_the_label_overrides_a_printed_breaking_edit_and_nothing_else(
        tmp_path, status, printed, label, step_exit):
    """schema_compat exits 1 for a breaking edit (which prints a BREAKING line) and 2 for an
    unreadable base; a crash also exits 1 but prints none: only a finding may be accepted
    with ``schema-break-ok``."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "python").write_text(f"#!/bin/sh\necho '{printed}'\nexit {status}\n", encoding="utf-8")
    (stub / "python").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}", "BASE_REF": "main",
           "OVERRIDE": label, "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
    done = subprocess.run(["bash", "-c", _schema_compat_step()], cwd=tmp_path, env=env,
                          capture_output=True, text=True, check=False)
    assert done.returncode == step_exit


_LABEL_GATE = re.compile(r"contains\(github\.event\.pull_request\.labels\.\*\.name, '([^']+)'\)")
_RUN_ID, _PR = 9001, 512


def _value(expr: str, event: dict, wf: dict | None = None) -> object:
    """Evaluate a workflow expression of the operators its group and job guards use; `&&` and `||` return an operand, as in Actions."""
    ctx = {"github.workflow": (wf or _ci())["name"], "github.run_id": _RUN_ID, **event}
    py = re.sub(r"\bgithub(?:\.[\w-]+)+", lambda m: f"_ctx({m.group(0)!r})", expr.replace("labels.*.name", "labels"))
    py = re.sub(r"!(?!=)", " not ", py.replace("&&", " and ").replace("||", " or "))
    names = {"_ctx": ctx.get, "fromJSON": json.loads, "contains": lambda seq, item: item in (seq or ()), "true": True,
             "cancelled": lambda: False}
    return eval(py, {"__builtins__": {}}, names)  # nosec B307


def _group(event: dict, wf: dict | None = None) -> str:
    template = (wf or _ci())["concurrency"]["group"]
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(_value(m.group(1), event, wf)), template)


def _ran(event: dict) -> set[str]:
    """The jobs that run: a job runs when every job it needs ran and its `if` holds (no guard calls always())."""
    jobs = _ci()["jobs"]

    def runs(name: str) -> bool:
        needs = jobs[name].get("needs") or []
        cond = str(jobs[name].get("if", "true")).strip().removeprefix("${{").removesuffix("}}")
        return all(runs(n) for n in ([needs] if isinstance(needs, str) else needs)) and bool(_value(cond, event))

    return {name for name in jobs if runs(name)}


def _pr(action: str, label: str | None = None) -> dict:
    event = {"github.event_name": "pull_request", "github.event.action": action, "github.event.pull_request.number": _PR}
    return event | ({"github.event.label.name": label} if label else {})


_PR_JOBS = set(_ci()["jobs"]) - {"test-latest", "test-seeds"}


@pytest.mark.parametrize("action", ["labeled", "unlabeled"])
def test_a_label_no_step_reads_neither_cancels_the_run_nor_runs_the_jobs(action):
    """Renovate labels its PR right after opening it; that event must not cancel the push's run."""
    read = set(_LABEL_GATE.findall(CI.read_text(encoding="utf-8")))
    assert read == {"skip-changelog", "schema-break-ok"}
    ignored = _pr(action, "dependencies")
    assert (_group(ignored), _ran(ignored)) == ("ci-9001", set())
    for label in read:
        assert (_group(_pr(action, label)), _ran(_pr(action, label))) == ("ci-512", _PR_JOBS)


def test_the_seed_job_runs_on_schedule_and_dispatch_only_under_a_seed_it_passes_to_pytest():
    """#1059 asks for a scheduled whole-suite run on main under several random seeds."""
    for name in ("schedule", "workflow_dispatch"):
        assert "test-seeds" in _ran({"github.event_name": name})
    for event in ({"github.event_name": "push"}, _pr("synchronize"), _pr("labeled", "skip-changelog")):
        assert "test-seeds" not in _ran(event)
    assert _ci()["jobs"]["test-seeds"]["timeout-minutes"] <= 30
    runs = [str(step.get("run", "")) for step in _ci()["jobs"]["test-seeds"]["steps"]]
    assert any("pytest" in run and "-n auto" in run and '--randomly-seed="$SEED"' in run for run in runs)
    assert any("GITHUB_RUN_NUMBER" in run and "SEED=" in run and "GITHUB_ENV" in run for run in runs)
    checkout = _ci()["jobs"]["test-seeds"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["ref"] == "main"


def test_a_push_to_a_pr_supersedes_its_run_and_a_main_push_keeps_its_own():
    assert (_group(_pr("synchronize")), _ran(_pr("synchronize"))) == ("ci-512", _PR_JOBS)
    push = {"github.event_name": "push"}
    # mutation-diff mutates the lines a PR changes against its base: there is no base on a push.
    assert (_group(push), _ran(push)) == ("ci-9001", _PR_JOBS - {"mutation-diff"})


PLATFORMS = CI.parent / "platforms.yml"


def _platforms() -> dict:
    return yaml.safe_load(PLATFORMS.read_text(encoding="utf-8"))


def _platform_jobs(event: dict) -> set[str]:
    """platforms.yml's jobs that run for the event; its `report` needs `tests` but runs unless cancelled."""
    wf = _platforms()
    return {name for name, job in wf["jobs"].items()
            if _value(str(job["if"]).strip().removeprefix("${{").removesuffix("}}"), event, wf)}


def _labeled_pr(action: str, labels: list[str], label: str | None = None) -> dict:
    return _pr(action, label) | {"github.event.pull_request.labels": labels}


def test_the_platform_suite_runs_nightly_by_hand_and_on_a_pr_labeled_needs_windows():
    """A PR runs it when needs-windows is added and on each push while the label is on; another label runs nothing."""
    on = _trigger(_platforms())
    assert set(on["pull_request"]["types"]) == {"labeled", "synchronize", "reopened"}
    assert "workflow_dispatch" in on and on["schedule"][0]["cron"]
    assert _platform_jobs({"github.event_name": "schedule"}) == {"tests", "report"}
    assert _platform_jobs({"github.event_name": "workflow_dispatch"}) == {"tests"}
    assert _platform_jobs(_labeled_pr("labeled", ["needs-windows"], "needs-windows")) == {"tests"}
    assert _platform_jobs(_labeled_pr("synchronize", ["dependencies", "needs-windows"])) == {"tests"}
    assert _platform_jobs(_labeled_pr("labeled", ["needs-windows", "dependencies"], "dependencies")) == set()
    assert _platform_jobs(_labeled_pr("synchronize", ["dependencies"])) == set()


def test_another_label_on_a_needs_windows_pr_cancels_nothing():
    wf = _platforms()
    other = _labeled_pr("labeled", ["needs-windows", "dependencies"], "dependencies")
    assert _group(other, wf) == "platforms-9001"
    assert _group(_labeled_pr("labeled", ["needs-windows"], "needs-windows"), wf) == "platforms-512"
    assert _group(_labeled_pr("synchronize", ["needs-windows"]), wf) == "platforms-512"


def test_the_platform_suite_runs_on_windows_and_macos_with_skip_reasons_and_writes_only_issues():
    wf = _platforms()
    tests = wf["jobs"]["tests"]
    assert tests["strategy"]["matrix"]["os"] == ["windows-latest", "macos-latest"]
    assert tests["strategy"]["fail-fast"] is False
    suite = next(s["run"] for s in tests["steps"] if s.get("id") == "suite")
    assert "pytest -n auto" in suite and "-rfEs" in suite
    assert wf["permissions"] == {"contents": "read"} and "permissions" not in tests
    assert wf["jobs"]["report"]["permissions"] == {"issues": "write"}


def _report_script() -> str:
    steps = _platforms()["jobs"]["report"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == "Open, update or close each platform's issue")


def _run_report(tmp_path: Path, results: dict[str, tuple[str, list[str]] | None], open_issues: dict[str, str]) -> list[str]:
    """Run the report step with a stub `gh` that answers `issue list` from open_issues and logs every other call."""
    stub, res, log = tmp_path / "bin", tmp_path / "results", tmp_path / "gh.log"
    stub.mkdir()
    (stub / "gh").write_text(
        '#!/bin/sh\nif [ "$1 $2" = "issue list" ]; then\n  case "$*" in\n'
        f'    *"on Windows"*) echo "{open_issues.get("Windows", "")}" ;;\n'
        f'    *"on macOS"*) echo "{open_issues.get("macOS", "")}" ;;\n  esac\n  exit 0\nfi\n'
        f'printf "%s\\n" "$@" >> "{log}"\necho ---- >> "{log}"\n', encoding="utf-8")
    (stub / "gh").chmod(0o755)
    for os_name, result in results.items():
        if result is not None:
            (res / f"result-{os_name}").mkdir(parents=True)
            (res / f"result-{os_name}" / "outcome").write_text(result[0] + "\n", encoding="utf-8")
            (res / f"result-{os_name}" / "failed").write_text("".join(f"{i}\n" for i in result[1]), encoding="utf-8")
    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}", "RESULTS": str(res), "RUN_URL": "https://run/1"}
    subprocess.run(["bash", "-eo", "pipefail", "-c", _report_script()], cwd=tmp_path, env=env, check=True)
    return log.read_text(encoding="utf-8").split("----\n")[:-1] if log.exists() else []


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="runs the step's bash")
def test_a_green_platform_closes_its_issue_and_a_red_one_opens_one_with_the_failing_ids(tmp_path):
    calls = _run_report(tmp_path, {"Windows": ("success", []), "macOS": ("failure", ["tests/a.py::t", "tests/b.py::u"])},
                        {"Windows": "7"})
    assert calls[0] == "issue\nclose\n7\n--comment\nGreen again: https://run/1\n"
    assert calls[1].startswith("issue\ncreate\n--title\nNightly full suite fails on macOS\n--body\n")
    assert "https://run/1" in calls[1] and "Failing tests (2):\n- `tests/a.py::t`\n- `tests/b.py::u`" in calls[1]
    assert len(calls) == 2


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="runs the step's bash")
def test_a_green_platform_with_no_open_issue_makes_no_call(tmp_path):
    assert _run_report(tmp_path, {"Windows": ("success", []), "macOS": ("success", [])}, {}) == []


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="runs the step's bash")
def test_a_red_platform_with_an_open_issue_comments_and_a_missing_result_counts_as_red(tmp_path):
    ids = [f"tests/t.py::t{i}" for i in range(105)]
    calls = _run_report(tmp_path, {"Windows": ("failure", ids), "macOS": None}, {"Windows": "7", "macOS": "9"})
    assert calls[0].startswith("issue\ncomment\n7\n--body\nStill failing. ")
    assert "Failing tests (105):" in calls[0] and "tests/t.py::t99`" in calls[0] and "t100`" not in calls[0]
    assert "- and 5 more, in the run log" in calls[0]
    assert calls[1].startswith("issue\ncomment\n9\n--body\nStill failing. ")
    assert "Failing tests (0):\nNo failing test id in the log" in calls[1]
    assert len(calls) == 2
