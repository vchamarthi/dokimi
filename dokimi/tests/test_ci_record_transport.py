# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Exercise the workflow's Git CAS transport; the policy CLI is stubbed explicitly."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _prepare(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "baselines"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.name", "test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "main-only").write_text("must not enter the baseline branch")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "main")
    subprocess.run(["git", "clone", str(origin), str(checkout)], check=True, capture_output=True)
    return origin, seed, checkout


def _run(tmp_path: Path, *, race: bool) -> tuple[subprocess.CompletedProcess[str], Path]:
    origin, seed, checkout = _prepare(tmp_path)
    tools = tmp_path / "bin"
    tools.mkdir()
    dokimi = tools / "dokimi"
    dokimi.write_text("#!/usr/bin/env python3\nimport pathlib,sys\na=sys.argv\n"
                      "p=pathlib.Path(a[a.index('--store')+1]);p.mkdir(exist_ok=True)\n"
                      "(p/'ours.json').write_text('approved')\n")
    dokimi.chmod(0o755)
    real_git = shutil.which("git")
    if not real_git:
        pytest.skip("git required")
    if race:
        race_script = tmp_path / "race.sh"
        race_script.write_text('''set -eu
cd "$SEED"
"$REAL_GIT" switch --orphan racer
mkdir records
printf competing > records/theirs.json
"$REAL_GIT" add records
"$REAL_GIT" commit -m competing
"$REAL_GIT" push origin HEAD:refs/heads/dokimi-baselines
''')
        wrapper = tools / "git"
        wrapper.write_text('''#!/bin/bash
if [[ "$1" == -c && ! -f "$RACE_MARKER" ]]; then
  touch "$RACE_MARKER"
  bash "$RACE_SCRIPT"
fi
exec "$REAL_GIT" "$@"
''')
        wrapper.chmod(0o755)
    workflow = yaml.safe_load((ROOT / ".github/workflows/dokimi-record.yml").read_text())
    script = next(s["run"] for s in workflow["jobs"]["record"]["steps"]
                  if s.get("name") == "Commit baseline with compare-and-swap")
    env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "REAL_GIT": real_git,
           "TOKEN": "local-test", "REPOSITORY": "local/repo", "RUN_FILE": "approved.json",
           "MERGED_ROOT": str(tmp_path), "HEAD": "a" * 40, "PREVIOUS": "none", "POLICY": "policy.json",
           "CONFIG": "", "COMMIT_AUTHOR": "vchamarthi", "COMMIT_EMAIL": "test@example.invalid",
           "SEED": str(seed), "RACE_SCRIPT": str(tmp_path / "race.sh"), "RACE_MARKER": str(tmp_path / "raced")}
    # Linux uses base64 -w0; this local adapter keeps the workflow transport runnable on macOS.
    base64 = tools / "base64"
    base64.write_text("#!/usr/bin/env python3\nimport base64,sys\nsys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())\n")
    base64.chmod(0o755)
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=checkout,
                            env=env, capture_output=True, text=True)
    return result, origin


def test_first_record_creates_only_the_baseline_branch(tmp_path: Path) -> None:
    result, origin = _run(tmp_path, race=False)
    assert result.returncode == 0, result.stderr
    assert _git(origin, "ls-tree", "--name-only", "dokimi-baselines") == "records"
    assert _git(origin, "show", "dokimi-baselines:records/ours.json") == "approved"
    assert _git(origin, "show", "main:main-only") == "must not enter the baseline branch"
    assert "Signed-off-by: vchamarthi" in _git(origin, "show", "-s", "--format=%B", "dokimi-baselines")


def test_racing_first_writer_is_preserved_after_retry(tmp_path: Path) -> None:
    result, origin = _run(tmp_path, race=True)
    assert result.returncode == 0, result.stderr
    assert _git(origin, "show", "dokimi-baselines:records/theirs.json") == "competing"
    assert _git(origin, "show", "dokimi-baselines:records/ours.json") == "approved"
    assert "stale info" in result.stderr
