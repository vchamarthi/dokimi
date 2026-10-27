# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Subscription CLI transport. No SDK, auth-file access, or LLM-layer dependency."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import tempfile
from typing import Any, cast

from . import process
from .metrics.usage import UsageRecord

DISABLED_FEATURES = (
    'shell_tool', 'unified_exec', 'apps', 'browser_use', 'computer_use', 'multi_agent',
    'memories', 'hooks', 'skill_search', 'view_image', 'code_mode_host',
    'code_mode', 'code_mode_only',
    'image_generation', 'workspace_dependencies', 'sleep_tool', 'plugins',
    'remote_plugin', 'shell_snapshot', 'skill_mcp_dependency_install', 'goals',
    'in_app_browser', 'in_app_chat', 'in_app_local_automation', 'tool_suggest',
    'browser_use_external', 'browser_use_full_cdp_access', 'realtime_conversation',
)


POLICY = 'codex-cli-clean-v2'
CODE_MODE_DIAGNOSTIC = (
    'Code Mode is unavailable because code-mode host is disabled. '
    'Code mode will fail closed; enable `features.code_mode_host` and install '
    '`codex-code-mode-host`.'
)


@dataclass
class Completion:
    text: str
    usage: UsageRecord
    runtime: dict[str, Any]


def runtime_identity() -> dict[str, Any]:
    executable = shutil.which('codex')
    if executable is None:
        raise ValueError('codex-cli executable unavailable; install Codex and check PATH')
    path = Path(executable).resolve()
    stat = path.stat()
    return dict(_runtime(str(path), stat.st_dev, stat.st_ino, stat.st_size,
                         stat.st_mtime_ns, stat.st_ctime_ns))


@lru_cache(maxsize=16)
def _runtime(path: str, *identity: int) -> dict[str, Any]:
    binary_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    done = process.run([path, '--version'], timeout=10, capture_limit=4096,
                       env=process.Env.only(('PATH', 'HOME', 'SYSTEMROOT')))
    version = done.stdout.strip()
    if (done.returncode or done.timed_out or done.truncated
            or re.fullmatch(r'codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:[-+][\w.-]+)?', version) is None):
        raise ValueError('codex-cli version preflight failed; check installed CLI')
    return dict(executable=path, cli_version=version, binary_sha256=binary_hash,
                transport_policy=POLICY, response_cache='disabled')


def failure_category(raw: str) -> str:
    lowered = raw.lower()
    categories = (
        (('unknown configuration field', 'unknown field'),
         'configuration field rejected; check installed CLI compatibility'),
        (('model_not_found', 'model not found'),
         'model_not_found; select an explicitly available account model'),
        (('authentication', 'unauthorized', '401', 'not logged in'),
         'authentication failed; check Codex login'),
        (('rate limit', 'rate_limit', '429'), 'rate limit; retry after account limits reset'),
    )
    for markers, category in categories:
        if any(marker in lowered for marker in markers):
            return category
    return 'execution failed; check CLI configuration and account access'


def check_capabilities(max_tokens: int | None, temperature: float | None,
                       seed: int | None, stream: bool = False) -> None:
    if any(v is not None for v in (max_tokens, temperature, seed)) or stream:
        raise ValueError('codex-cli cannot enforce max_tokens, sampling, or streaming; '
                         'use unlimited output with no sampling controls')


def complete(model: str, messages: list[dict[str, Any]]) -> Completion:
    """Run one clean, tool-free turn, retaining answer, usage, and runtime provenance."""
    if not model.strip():
        raise ValueError('codex-cli requires an explicit model')
    system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
    users = [m['content'] for m in messages if m['role'] == 'user']
    if len(users) != 1 or any(m['role'] not in ('system', 'user') for m in messages):
        raise ValueError('codex-cli supports a single text prompt, not tool-chat')
    config = ['approval_policy="never"', 'project_doc_max_bytes=0',
              'web_search="disabled"', 'mcp_servers={}',
              'developer_instructions=' + json.dumps(system, ensure_ascii=False),
              'features.skip_host_skill_discovery=true', 'suppress_unstable_features_warning=true']
    config += [f'features.{name}=false' for name in DISABLED_FEATURES]
    runtime = runtime_identity()
    argv = [runtime['executable'], 'exec', '--strict-config', '--ignore-user-config', '--ephemeral',
            '--skip-git-repo-check', '--ignore-rules', '-s', 'read-only', '--json', '--model', model]
    for value in config:
        argv.extend(['-c', value])
    argv.append('-')
    # Keep only CLI authentication discovery and executable/runtime necessities.
    # Never read credentials or redirect CODEX_HOME; API keys are deliberately absent.
    env = process.Env.only(('HOME', 'CODEX_HOME', 'PATH', 'TMPDIR', 'LANG', 'SYSTEMROOT'))
    with tempfile.TemporaryDirectory(prefix='dokimi-codex-') as cwd:
        done = process.run(argv, cwd=cwd, env=env, input=users[0], timeout=600,
                           capture_limit=8 * 1024 * 1024)
    if done.returncode or done.timed_out or done.truncated:
        raise ValueError('codex-cli timed out or truncated stream' if done.timed_out or done.truncated
                         else 'codex-cli ' + failure_category(done.stdout + done.stderr))
    diagnostics: list[str] = []
    text, usage = parse_stream(done.stdout, diagnostics=diagnostics)
    runtime['startup_diagnostics'] = diagnostics
    return Completion(text, UsageRecord(prompt_tokens=usage['prompt_tokens'],
                             completion_tokens=usage['completion_tokens'],
                             total_tokens=usage['total_tokens'],
                             cache_read_tokens=usage['cache_read_tokens'],
                             model=model, provider='codex-cli', latency_sec=done.elapsed_sec), runtime)


def _item(event: dict) -> str | None:
    item = event.get('item')
    if not isinstance(item, dict) or item.get('type') not in ('agent_message', 'reasoning'):
        raise ValueError('codex-cli tool or unknown item rejected')
    if event['type'] == 'item.completed' and item['type'] == 'agent_message':
        text = item.get('text')
        if not isinstance(text, str):
            raise ValueError('codex-cli malformed agent message')
        return text
    return None


def _usage(event: dict) -> dict[str, int]:
    raw = event.get('usage')
    if not isinstance(raw, dict):
        raise ValueError('codex-cli missing usage')
    values = [raw.get(k) for k in ('input_tokens', 'output_tokens', 'cached_input_tokens')]
    if any(type(v) is not int or v < 0 for v in values):
        raise ValueError('codex-cli malformed usage')
    incoming, outgoing, cached = cast(list[int], values)
    return dict(prompt_tokens=incoming, completion_tokens=outgoing,
                total_tokens=incoming + outgoing, cache_read_tokens=cached)


def parse_stream(raw: str, *, diagnostics: list[str] | None = None) -> tuple[str, dict[str, int]]:
    """Fail closed: no tools, unknown events, failed or incomplete turns."""
    answers: list[str] = []
    usage = None
    state = 'initial'
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict) or usage is not None:
            raise ValueError('codex-cli malformed stream or data after terminal turn')
        kind = event.get('type', '')
        state = _transition(event, state, diagnostics)
        if kind in ('item.started', 'item.updated', 'item.completed'):
            if state == 'thread':
                continue
            answer = _item(event)
            if answer:
                answers.append(answer)
        elif kind == 'turn.completed':
            usage = _usage(event)
        elif kind not in ('thread.started', 'turn.started'):
            raise ValueError('codex-cli failed or unknown event')
    text = '\n'.join(answers).strip()
    if usage is None or not text:
        raise ValueError('codex-cli missing terminal turn or empty answer')
    return text, usage


def _transition(event: dict, state: str, diagnostics: list[str] | None) -> str:
    kind = event.get('type')
    if kind == 'thread.started' and state == 'initial':
        return 'thread'
    if kind == 'turn.started' and state == 'thread':
        return 'turn'
    if state == 'thread' and kind == 'item.completed':
        item = event.get('item', {})
        if (isinstance(item, dict) and item.get('type') == 'error'
                and item.get('message') == CODE_MODE_DIAGNOSTIC):
            if diagnostics is not None:
                diagnostics.append(CODE_MODE_DIAGNOSTIC)
            return state
    if state == 'turn' and kind in ('item.started', 'item.updated', 'item.completed',
                                    'turn.completed'):
        return state
    raise ValueError('codex-cli unexpected event or startup diagnostic')
