# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import pytest

from dokimi import process
from dokimi.llm import LLM


def stream(*events):
    return '\n'.join(json.dumps(e) for e in events)


MESSAGE = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'answer'}}
DONE = {'type': 'turn.completed', 'usage': {
    'input_tokens': 12, 'output_tokens': 3, 'cached_input_tokens': 4}}


@pytest.fixture(autouse=True)
def runtime_stub(monkeypatch):
    from dokimi.codex_cli import runtime_identity

    monkeypatch.setattr('dokimi.codex_cli.runtime_identity', lambda: {
        'executable': 'codex', 'cli_version': 'codex-cli 0.156.1',
        'binary_sha256': 'test-hash', 'transport_policy': 'codex-cli-clean-v2',
        'response_cache': 'disabled'})
    return runtime_identity


def test_installed_cli_config_dialect_accepts_role_instructions(monkeypatch):
    """Codex 0.156.1 rejects base_instructions before contacting a model."""
    def installed_cli(call):
        if any(arg.startswith('base_instructions=') for arg in call.argv):
            return process.reply(returncode=1, stderr='unknown configuration field base_instructions')
        assert 'developer_instructions="grade the answer"' in call.argv
        return process.reply(stream({'type': 'thread.started'}, {'type': 'turn.started'}, MESSAGE, DONE))

    monkeypatch.setattr(process, 'run', process.FakeRunner(installed_cli))
    reply = LLM('operator-model', 'codex-cli').complete('question', system='grade the answer')
    assert reply.text == 'answer' and reply.usage.total_tokens == 15


def test_subscription_clean_invocation_and_unknown_cost(monkeypatch):
    def run(call):
        assert list(Path(call.cwd).iterdir()) == []
        assert call.input == b'question'
        assert 'OPENAI_API_KEY' not in call.env
        assert call.env.get('CODEX_HOME') == '/existing/auth-home'
        return process.reply(stream({'type': 'thread.started'}, {'type': 'turn.started'}, MESSAGE, DONE))
    runner = process.FakeRunner(run)
    monkeypatch.setenv('CODEX_HOME', '/existing/auth-home')
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-leak')
    monkeypatch.setattr(process, 'run', runner)
    reply = LLM('operator-model', 'codex-cli').complete('question', system='role instruction')
    argv = runner.calls[0].argv
    assert argv[argv.index('--model') + 1] == 'operator-model'
    assert {'--ignore-user-config', '--ignore-rules', '--strict-config', '--ephemeral'} <= set(argv)
    assert argv[argv.index('-s') + 1] == 'read-only'
    assert 'approval_policy="never"' in argv and 'web_search="disabled"' in argv
    for feature in ('shell_tool', 'unified_exec', 'apps', 'browser_use', 'computer_use',
                    'multi_agent', 'memories', 'hooks', 'skill_search', 'view_image',
                    'code_mode_host', 'image_generation', 'workspace_dependencies',
                    'sleep_tool', 'plugins', 'remote_plugin'):
        assert f'features.{feature}=false' in argv
    assert 'developer_instructions="role instruction"' in argv
    assert 'features.shell_tool=false' in argv
    assert 'features.skip_host_skill_discovery=true' in argv
    assert 'suppress_unstable_features_warning=true' in argv
    assert 'features.code_mode=false' in argv
    assert 'features.code_mode_only=false' in argv
    assert 'mcp_servers={}' in argv and 'project_doc_max_bytes=0' in argv
    assert reply.text == 'answer'
    assert reply.usage.cost_usd is None
    assert reply.usage.cache_read_tokens == 4
    assert reply.usage.total_tokens == 15


@pytest.mark.parametrize('output', [
    '{bad', stream(MESSAGE), stream(DONE),
    stream(MESSAGE, {'type': 'turn.failed', 'error': {'message': 'failed'}}),
    stream({'type': 'item.started', 'item': {'type': 'command_execution'}}, MESSAGE, DONE),
    stream(MESSAGE, DONE, MESSAGE),
    stream(MESSAGE, {'type': 'turn.completed', 'usage': {}}),
])
def test_rejects_invalid_or_assisted_streams(monkeypatch, output):
    monkeypatch.setattr(process, 'run', process.FakeRunner(lambda _: process.reply(output)))
    with pytest.raises(ValueError):
        LLM('selected', 'codex-cli').complete('question')


@pytest.mark.parametrize('kwargs', [{'max_tokens': 10}, {'temperature': 0}, {'seed': 3}])
def test_rejects_unsupported_sampling(kwargs):
    with pytest.raises(ValueError, match='codex-cli'):
        LLM('selected', 'codex-cli', **kwargs).complete('question')


def test_rejects_tool_chat():
    with pytest.raises(ValueError, match='codex-cli'):
        LLM('selected', 'codex-cli').chat([{'role': 'user', 'content': 'hi'}])


@pytest.mark.parametrize('failure', [process.reply(returncode=1), process.reply(timed_out=True)])
def test_rejects_process_failure(monkeypatch, failure):
    monkeypatch.setattr(process, 'run', process.FakeRunner(lambda _: failure))
    with pytest.raises(ValueError, match='codex-cli'):
        LLM('selected', 'codex-cli').complete('question')


def test_truncated_capture_is_rejected(monkeypatch):
    from dataclasses import replace

    done = replace(process.reply(stream({'type': 'thread.started'}, {'type': 'turn.started'}, MESSAGE, DONE)), truncated=True)
    monkeypatch.setattr(process, 'run', process.FakeRunner(lambda _: done))
    with pytest.raises(ValueError, match='truncated'):
        LLM('selected', 'codex-cli').complete('question')


def test_cli_requires_explicit_roles_and_unlimited_output():
    from dokimi.commands.novelty import _clients
    from test_novelty_cli import _parse

    base = ['novelty', 'analyze', '--artifact', 'skill:unused', '--provider', 'codex-cli',
            '--model', 'chosen-answerer']
    with pytest.raises(SystemExit, match='explicit --judge-model'):
        _clients(_parse(*base))
    with pytest.raises(SystemExit, match='unlimited-output'):
        _clients(_parse(*base, '--judge-model', 'chosen-judge'))
    clients = _clients(_parse(*base, '--judge-model', 'chosen-judge',
                             '--extract-model', 'chosen-extractor', '--unlimited-output'))
    assert [(c.model, c.provider, c.role) for c in clients] == [
        ('chosen-extractor', 'codex-cli', 'extractor'),
        ('chosen-answerer', 'codex-cli', 'answerer'),
        ('chosen-judge', 'codex-cli', 'judge')]


THREAD = {'type': 'thread.started', 'thread_id': 'test'}
TURN = {'type': 'turn.started'}
CODE_MODE_MESSAGE = (
    'Code Mode is unavailable because code-mode host is disabled. '
    'Code mode will fail closed; enable `features.code_mode_host` and install '
    '`codex-code-mode-host`.'
)
DIAGNOSTIC = {'type': 'item.completed', 'item': {'type': 'error',
                                               'message': CODE_MODE_MESSAGE}}


def test_known_startup_diagnostic_is_recorded():
    from dokimi.codex_cli import parse_stream

    diagnostics = []
    answer, usage = parse_stream(stream(THREAD, DIAGNOSTIC, TURN, MESSAGE, DONE),
                                 diagnostics=diagnostics)
    assert answer == 'answer' and usage['total_tokens'] == 15
    assert diagnostics == [CODE_MODE_MESSAGE]


@pytest.mark.parametrize('events', [
    (DIAGNOSTIC, THREAD, TURN, MESSAGE, DONE),
    (THREAD, TURN, DIAGNOSTIC, MESSAGE, DONE),
    (THREAD, TURN, MESSAGE, DONE, DIAGNOSTIC),
    (THREAD, {'type': 'item.completed', 'item': {'type': 'error',
      'message': CODE_MODE_MESSAGE + ' unknown'}}, TURN, MESSAGE, DONE),
    (THREAD, {'type': 'item.completed', 'item': {'type': 'error',
      'message': 'authentication failed'}}, TURN, MESSAGE, DONE),
    (THREAD, MESSAGE, TURN, DONE),
])
def test_startup_exception_is_exact_and_position_bound(events):
    from dokimi.codex_cli import parse_stream

    with pytest.raises(ValueError):
        parse_stream(stream(*events))


@pytest.mark.parametrize(('message', 'category'), [
    ('unknown configuration field secret', 'configuration field rejected'),
    ('model_not_found secret', 'model_not_found'),
    ('authentication failed secret', 'authentication'),
    ('rate limit exceeded secret', 'rate limit'),
])
def test_failure_categories_do_not_echo_provider_secrets(monkeypatch, message, category):
    monkeypatch.setattr(process, 'run', process.FakeRunner(
        lambda _: process.reply(returncode=1, stderr=message)))
    with pytest.raises(ValueError) as error:
        LLM('selected', 'codex-cli').complete('question')
    assert category in str(error.value)
    assert 'secret' not in str(error.value)


def test_runtime_preflight_is_lazy_and_invalidates_on_binary_change(
        tmp_path, monkeypatch, runtime_stub):
    import hashlib

    from dokimi import codex_cli

    executable = tmp_path / 'codex'
    executable.write_bytes(b'first binary')
    monkeypatch.setattr(codex_cli.shutil, 'which', lambda _: str(executable))
    runner = process.FakeRunner(lambda _: process.reply('codex-cli 0.156.1\n'))
    monkeypatch.setattr(process, 'run', runner)
    codex_cli._runtime.cache_clear()
    LLM('selected', 'codex-cli')
    assert runner.calls == []
    identity = runtime_stub
    first = identity()
    assert first['binary_sha256'] == hashlib.sha256(b'first binary').hexdigest()
    assert first['cli_version'] == 'codex-cli 0.156.1'
    assert identity() == first and len(runner.calls) == 1
    executable.write_bytes(b'updated binary')
    assert identity()['binary_sha256'] != first['binary_sha256']
    assert len(runner.calls) == 2


def test_native_metadata_and_no_cross_runtime_response_replay(monkeypatch, tmp_path):
    from dokimi.novelty.client import LLMClient

    monkeypatch.setenv('DOKIMI_RESPONSE_CACHE', str(tmp_path))

    def old_cache_read(*args):
        raise AssertionError('unqualified cache must not be read')

    monkeypatch.setattr('dokimi.caching.load_response', old_cache_read)
    runner = process.FakeRunner(lambda _: process.reply(
        stream(THREAD, DIAGNOSTIC, TURN, MESSAGE, DONE)))
    monkeypatch.setattr(process, 'run', runner)
    client = LLMClient('selected', 'codex-cli')
    assert client.runtime == []
    assert client.text('question') == client.text('question') == 'answer'
    assert len(runner.calls) == 2
    assert client.runtime[0]['startup_diagnostics'] == [CODE_MODE_MESSAGE]
    assert client.runtime[0]['binary_sha256'] == 'test-hash'
    assert client.cost_summary()['usd'] is None


@pytest.mark.parametrize('event', [
    {'type': 'item.completed', 'item': {'type': 'command_execution'}},
    {'type': 'item.completed', 'item': {'type': 'mcp_tool_call'}},
    {'type': 'error', 'message': 'authentication failed'},
])
def test_tools_and_errors_rejected_during_valid_turn(event):
    from dokimi.codex_cli import parse_stream

    with pytest.raises(ValueError):
        parse_stream(stream(THREAD, TURN, event, MESSAGE, DONE))


def test_explicit_executable_takes_precedence_over_an_old_path_install(
        tmp_path, monkeypatch, runtime_stub):
    from dokimi import codex_cli

    old, selected = tmp_path / 'old', tmp_path / 'selected'
    old.write_bytes(b'old version')
    selected.write_bytes(b'chosen version')
    monkeypatch.setenv('DOKIMI_CODEX_EXECUTABLE', str(selected))
    monkeypatch.setattr(codex_cli.shutil, 'which',
                        lambda name: str(selected if name == str(selected) else old))
    runner = process.FakeRunner(lambda _: process.reply('codex-cli 0.164.0-alpha.11.1\n'))
    monkeypatch.setattr(process, 'run', runner)
    codex_cli._runtime.cache_clear()
    identity = runtime_stub()
    assert identity['executable'] == str(selected.resolve())
    assert runner.calls[0].argv[0] == str(selected.resolve())


def test_invalid_explicit_executable_never_falls_back_to_path(monkeypatch, runtime_stub):
    from dokimi import codex_cli

    monkeypatch.setenv('DOKIMI_CODEX_EXECUTABLE', 'unavailable-selected-cli')
    monkeypatch.setattr(codex_cli.shutil, 'which', lambda name: '/old/codex' if name == 'codex' else None)
    runner = process.FakeRunner()
    monkeypatch.setattr(process, 'run', runner)
    with pytest.raises(ValueError, match='executable unavailable'):
        runtime_stub()
    assert runner.calls == []
