# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json

from dokimi.artifacts import load_artifact, save_artifact
from dokimi.novelty.analyze import AnalyzeConfig, analyze
from dokimi.novelty.atoms import load_atoms
from dokimi.novelty.evidence import render_evidence_html
from test_novelty_analyze import SKILL, _clients


def test_export_rerender_and_quarantine(tmp_path):
    extractor, answerer, judge = _clients()
    original = extractor.transport

    def transport(prompt, system):
        if 'Does the source text below support' in prompt:
            return json.dumps({'verdict': 'unsupported', 'why': '<script>bad</script>'}), None
        return original(prompt, system)

    extractor.transport = transport
    result = analyze(SKILL + '\n<script>alert(1)</script>', AnalyzeConfig(
        artifact_ref='hostile', shorten_artifact=False, confirm_carriers=False),
        extractor=extractor, answerer=answerer, judge=judge)
    assert result.artifact['paired']['lenient']['n_paired'] == 0
    assert result.artifact['population']['n_trusted'] == 0
    assert result.evidence['atoms'] and result.evidence['runs'] == []
    assert all(a['faithfulness'] == 'unsupported' for a in result.evidence['atoms'])
    path = tmp_path / 'evidence.json'
    save_artifact('novelty_evidence', result.evidence, path)
    saved = load_artifact('novelty_evidence', path)
    atoms, spans = load_atoms(path)
    assert len(atoms) == len(result.atoms) and len(spans) == len(result.spans)
    html = render_evidence_html(saved)
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'Quarantined' in html and 'unmeasured' in html
    assert 'source agreement is not coding quality' in html


def test_export_every_probe_arm_and_rationale():
    extractor, answerer, judge = _clients()
    result = analyze(SKILL, AnalyzeConfig(artifact_ref='test', shorten_artifact=False,
                     confirm_carriers=False), extractor=extractor, answerer=answerer, judge=judge)
    evidence = result.evidence
    assert len(evidence['runs']) == len(evidence['probes']) * 2
    assert {r['arm'] for r in evidence['runs']} == {'full', 'no_artifact'}
    assert all(r['rationale'] == 'stub' for r in evidence['runs'])
    assert all(p['expected'] for p in evidence['probes'])
    assert {r['role'] for r in evidence['roles']} == {'extractor', 'answerer', 'judge'}
    assert evidence['source_sha256'].startswith('sha256:')
    assert evidence['config_sha256'].startswith('sha256:')
    assert evidence['roles'][0]['usd'] is None
    html = render_evidence_html(evidence)
    assert 'INFORMED answer' in html and 'I am not sure' in html and 'stub' in html


def test_native_analyze_exports_and_view_rerenders_without_models(tmp_path, monkeypatch):
    from dokimi.commands import novelty
    from test_novelty_cli import _parse

    source = tmp_path / 'SKILL.md'
    source.write_text(SKILL)
    out = tmp_path / 'run'
    monkeypatch.setattr(novelty, '_clients', lambda _: _clients())
    args = _parse('novelty', 'analyze', '--artifact', f'skill:{source}',
                  '--out-dir', str(out), '--no-shorten', '--no-confirm-carriers')
    args.func(args)
    exported = out / 'evidence.json'
    assert exported.exists()
    original = (out / 'annotated.html').read_text()
    # Removing the source demonstrates the view consumes the saved evidence only.
    source.unlink()
    rendered = tmp_path / 'rerender.html'
    args = _parse('novelty', 'view', '--novelty', str(exported), '--out', str(rendered))
    args.func(args)
    assert rendered.read_text() == original
    assert 'INFORMED answer' in original


def test_skipped_verification_is_not_trusted():
    extractor, answerer, judge = _clients()
    result = analyze(SKILL, AnalyzeConfig(artifact_ref='test', verify_faithfulness=False),
                     extractor=extractor, answerer=answerer, judge=judge)
    assert result.artifact['population']['n_trusted'] == 0
    assert result.artifact['population']['verification_enabled'] is False
    assert answerer.calls == judge.calls == 0
    assert result.candidates == []
    assert result.atoms and all(a.faithfulness == 'unchecked' for a in result.atoms)


def test_mixed_population_keeps_supported_denominator_only():
    extractor, answerer, judge = _clients()
    original = extractor.transport

    def transport(prompt, system):
        if 'Does the source text below support' in prompt and '--accel' in prompt.split('SOURCE')[0]:
            return json.dumps({'verdict': 'distorted', 'why': 'wrong scope'}), None
        return original(prompt, system)

    extractor.transport = transport
    result = analyze(SKILL, AnalyzeConfig(artifact_ref='test'),
                     extractor=extractor, answerer=answerer, judge=judge)
    population = result.artifact['population']
    assert population['n_inventory'] == 2 and population['n_trusted'] == 1
    assert {r['atom_id'] for r in result.evidence['runs']} == set(population['trusted_ids'])
    assert result.candidates == []
    assert any(a.faithfulness == 'distorted' for a in result.atoms)


def test_capped_extraction_does_not_offer_shortening_from_sampled_claims():
    extractor, answerer, judge = _clients()
    result = analyze(SKILL, AnalyzeConfig(artifact_ref='test', max_atoms=1),
                     extractor=extractor, answerer=answerer, judge=judge)
    assert result.artifact['population']['n_trusted'] == 1
    assert result.candidates == []
    assert result.artifact['shortening'] == []
    assert any('shortening' in warning and 'cap' in warning
               for warning in result.artifact['warnings'])


def test_unavailable_verification_retains_inventory():
    extractor, answerer, judge = _clients()
    original = extractor.transport

    def transport(prompt, system):
        if 'Does the source text below support' in prompt:
            raise ValueError('unavailable')
        return original(prompt, system)

    extractor.transport = transport
    result = analyze(SKILL, AnalyzeConfig(artifact_ref='test'),
                     extractor=extractor, answerer=answerer, judge=judge)
    assert result.artifact['population']['n_trusted'] == 0
    assert len(result.evidence['atoms']) == 2
    assert all(a['meta']['verification']['why'] == 'verification unavailable'
               for a in result.evidence['atoms'])
    assert result.candidates == []


def test_partial_usage_never_reports_priced_subtotal_as_total():
    from dokimi.metrics.usage import UsageRecord
    from dokimi.novelty.client import LLMClient

    responses = iter([('a', UsageRecord(cost_usd=1.0)), ('b', None)])
    client = LLMClient('model', transport=lambda *_: next(responses))
    client.text('first')
    client.text('second')
    assert client.cost_summary()['usd'] is None
    assert client.cost_summary()['llm_calls'] == 2


def test_all_probe_kinds_missing_arms_and_hostile_evidence_are_visible():
    from pathlib import Path

    fixture = Path(__file__).parent / 'fixtures/artifacts/novelty_evidence.v1.json'
    evidence = load_artifact('novelty_evidence', fixture)
    assert {'direct', 'paraphrase', 'reversal'} == {p['kind'] for p in evidence['probes']}
    hostile = '<img src=x onerror="alert(1)">'
    evidence['runs'][0]['response'] = hostile
    evidence['runs'][0]['rationale'] = hostile
    evidence['probes'][0]['expected'] = hostile
    evidence['atoms'][0]['question'] = hostile
    missing = evidence['runs'].pop()
    html = render_evidence_html(evidence)
    assert '<img' not in html and '&lt;img' in html
    assert 'Missing / unmeasured' in html
    assert missing['question'] in html
    assert 'With artifact' in html and 'Without artifact' in html
    assert html.count('Expect refusal:') == len(evidence['probes'])


def test_legacy_html_escapes_saved_span_ids():
    from dokimi.novelty.attribute import attribute_spans
    from dokimi.novelty.report import render_annotated_html
    from dokimi.novelty.spans import segment

    spans = segment('Example text')
    spans[0].id = '<script>alert(1)</script>'
    html = render_annotated_html(attribute_spans(spans, []), title='example')
    assert '<script>' not in html
    assert '&lt;script&gt;' in html


def test_failed_call_leaves_cost_unknown_even_when_other_calls_are_priced():
    import pytest

    from dokimi.metrics.usage import UsageRecord
    from dokimi.novelty.client import LLMClient

    def transport(prompt, system):
        if prompt == 'fail':
            raise ValueError('stream incomplete')
        return 'answer', UsageRecord(cost_usd=1.0)

    client = LLMClient('model', transport=transport)
    with pytest.raises(ValueError):
        client.text('fail')
    client.text('success')
    assert client.cost_summary()['llm_calls'] == 2
    assert client.cost_summary()['usd'] is None


def test_unrequested_cap_and_injected_runtime_remain_unknown():
    extractor, answerer, judge = _clients()
    for client in (extractor, answerer, judge):
        client.max_tokens = None
    result = analyze(SKILL, AnalyzeConfig(artifact_ref='test', shorten_artifact=False,
                     confirm_carriers=False), extractor=extractor, answerer=answerer, judge=judge)
    assert all(not role['output_cap_enforced'] for role in result.evidence['roles'])
    assert all(role['runtime'] is None for role in result.evidence['roles'])


def test_runtime_and_diagnostics_are_part_of_configuration_hash():
    from dokimi.novelty.evidence import build_evidence

    clients = _clients()
    result = analyze(SKILL, AnalyzeConfig(artifact_ref='test', shorten_artifact=False,
                     confirm_carriers=False), extractor=clients[0], answerer=clients[1],
                     judge=clients[2])
    clients[1].runtime.append({'cli_version': 'codex-cli 0.156.1',
                              'binary_sha256': 'a', 'startup_diagnostics': []})
    def evidence():
        return build_evidence(SKILL, result.atoms, result.spans, [], result.measure_result,
                              result.artifact, clients, {})

    first = evidence()
    clients[1].runtime[0]['binary_sha256'] = 'b'
    second = evidence()
    assert first['config_sha256'] != second['config_sha256']
    clients[1].runtime[0]['startup_diagnostics'] = ['accepted diagnostic']
    third = evidence()
    assert second['config_sha256'] != third['config_sha256']
    assert third['roles'][1]['served_model'] is None
