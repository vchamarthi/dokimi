# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""The credential-free pinned CLI installer must produce a runnable native binary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("workflow,job_id", [("pr", "run"), ("revalidate", "revalidate")])
def test_claude_native_installer_runs_explicitly_without_credentials(tmp_path: Path, workflow: str, job_id: str):
    if shutil.which("node") is None:
        pytest.skip("native installer fixture requires Node.js")
    wf = yaml.safe_load((ROOT / f".github/workflows/dokimi-{workflow}.yml").read_text())
    job = wf["jobs"][job_id]
    steps = job["steps"]
    install = next(s for s in steps if s.get("name") == "Install pinned Claude subscription CLI")
    runtime = next(s for s in steps if "CLAUDE_CODE_OAUTH_TOKEN" in s.get("env", {}))
    assert steps.index(install) < steps.index(runtime)
    assert "secrets." not in str(install) + str(job.get("env", {})) + str(wf.get("env", {}))
    tools = tmp_path / "bin"
    tools.mkdir()
    npm_root = tmp_path / "node_modules"
    package = npm_root / "@anthropic-ai/claude-code"
    package.mkdir(parents=True)
    (package / "install.cjs").write_text(r"""
const fs = require('fs');
const path = require('path');
const exposed = Object.keys(process.env).filter(k => /(?:TOKEN|API_KEY)$/.test(k));
if (exposed.length) throw new Error('Credentials were supplied to the installer');
const binary = path.join(__dirname, 'native-cli');
fs.writeFileSync(binary, '#!/bin/sh\nprintf "2.1.306 (Claude Code)\\n"\n');
fs.chmodSync(binary, 0o755);
fs.writeFileSync(path.join(__dirname, 'installer-environment.json'), JSON.stringify(Object.keys(process.env)));
""")
    npm = tools / "npm"
    npm.write_text("#!/usr/bin/env python3\nimport os,pathlib,sys\n"
                   "root=pathlib.Path(os.environ['FIXTURE_NPM_ROOT'])\n"
                   "if sys.argv[1:]==['root','--global']: print(root)\n"
                   "elif sys.argv[1:]!=['install','--global','--ignore-scripts','@anthropic-ai/claude-code@2.1.306']: sys.exit(2)\n"
                   "elif (root/'@anthropic-ai/claude-code/native-cli').exists(): sys.exit(3)\n")
    npm.chmod(0o755)
    claude = tools / "claude"
    claude.write_text('#!/bin/sh\nexec "$FIXTURE_NPM_ROOT/@anthropic-ai/claude-code/native-cli" "$@"\n')
    claude.chmod(0o755)
    env = {"PATH": f"{tools}:{os.environ['PATH']}", "FIXTURE_NPM_ROOT": str(npm_root)}
    absent = subprocess.run([str(claude), "--version"], env=env, capture_output=True, text=True)
    assert absent.returncode != 0
    installed = subprocess.run(["bash", "-euo", "pipefail", "-c", install["run"]],
                               env=env, capture_output=True, text=True)
    assert installed.returncode == 0, installed.stderr
    assert "2.1.306 (Claude Code)" in installed.stdout
    observed = json.loads((package / "installer-environment.json").read_text())
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in observed and "ANTHROPIC_API_KEY" not in observed
