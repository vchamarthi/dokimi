# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Portable novelty evidence and its model-free, escaped HTML view."""

from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from dataclasses import asdict
from html import escape

from .report import population_description


def digest(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def build_evidence(text, atoms, spans, probes, measurement, artifact, clients, extraction,
                   source_bundle=None) -> dict:
    from .measure import atom_outcomes

    roles = [dict(c.cost_summary(), requested_model=c.model, served_model=None,
                  max_tokens=c.max_tokens, output_cap_enforced=(c.max_tokens is not None
                                       and c.provider != 'codex-cli'),
                  runtime=deepcopy(c.runtime) or None,
                  transport_policy='codex-cli-clean-v2' if c.provider == 'codex-cli' else None,
                  usage=[asdict(u) for u in c.usage]) for c in clients]
    config = dict(artifact['run_config'], roles=[{k: row[k] for k in (
        'role', 'provider', 'requested_model', 'max_tokens', 'output_cap_enforced',
        'transport_policy', 'runtime')}
        for row in roles])
    return {
        'source_text': text, 'source_sha256': digest(text),
        'config': config, 'config_sha256': digest(json.dumps(config, sort_keys=True)),
        'extractor': clients[0].model, 'bundle': artifact.get('bundle', {}),
        'cap': config['max_atoms'], 'extraction': extraction,
        'atoms': [a.as_dict() for a in atoms],
        'spans': [dict(s.as_dict(), source=source_bundle.source_of(s.start)
                       if source_bundle else artifact['artifact']['ref']) for s in spans],
        'outcomes': atom_outcomes(measurement.runs, with_arm=measurement.with_arm),
        'probes': [p.as_dict() for p in probes],
        'runs': [r.as_dict() for r in measurement.runs], 'arms': list(measurement.arms),
        'roles': roles, 'population': artifact['population'], 'novelty': artifact,
    }


def _id(kind: str, value: str) -> str:
    return kind + '-' + hashlib.sha256(value.encode()).hexdigest()[:20]


def _text(value) -> str:
    return escape(str(value), quote=True)


def _probe_rows(probe: dict, runs: list[dict], arms: list[str]) -> str:
    cells = []
    for arm in arms:
        matches = [r for r in runs if (r['atom_id'], r['probe_kind'], r['question'], r['arm'])
                   == (probe['atom_id'], probe['kind'], probe['question'], arm)]
        content = '<p>Missing / unmeasured</p>'
        if matches:
            content = ''.join('<pre>' + _text(r['response']) + '</pre><p>Grade: '
                              + _text(r['verdict']) + '</p><p>Rationale: '
                              + _text(r.get('rationale', 'unavailable')) + '</p>' for r in matches)
        cells.append('<td>' + content + '</td>')
    return '<tr>' + ''.join(cells) + '</tr>'


def _atom_html(atom: dict, evidence: dict) -> str:
    state = 'Trusted' if atom['id'] in evidence['population']['trusted_ids'] else 'Quarantined'
    parts = [f'<section id="{_id("atom", atom["id"])}"><h3>{_text(atom["statement"])}</h3>',
             f'<p>{state}: {_text(atom["faithfulness"])}</p>',
             '<p>Verification: ' + _text(atom.get('meta', {}).get('verification',
                                                        'unavailable / skipped')) + '</p>',
             '<p>Outcome: ' + _text(evidence.get('outcomes', {}).get(atom['id'],
                                                                    'unmeasured')) + '</p>',
             '<p>Expected answer: ' + _text(atom['answer']) + '</p>']
    probes = [p for p in evidence['probes'] if p['atom_id'] == atom['id']]
    if not probes:
        parts.append('<p>Missing / unmeasured: no trusted probes.</p>')
    for probe in probes:
        probe_id = _id('probe', json.dumps(probe, sort_keys=True))
        parts += [f'<h4 id="{probe_id}">' + _text(probe['kind']) + ': ' + _text(probe['question']) + '</h4>',
                  '<p>Expected: ' + _text(probe['expected']) + '</p>',
                  '<p>Expect refusal: ' + _text(probe['expect_refusal']) + '</p>',
                  '<table><tr>' + ''.join('<th>' + _text({'full': 'With artifact',
                                        'no_artifact': 'Without artifact'}.get(a, a)) + '</th>'
                                        for a in evidence['arms']) + '</tr>',
                  _probe_rows(probe, evidence['runs'], evidence['arms']), '</table>']
    return ''.join(parts) + '</section>'


def render_evidence_html(evidence: dict) -> str:
    """Render all source, inventory and probe arms, never choosing a favorable example."""
    parts = ['<!doctype html><html><head><meta charset="utf-8"><title>Novelty evidence</title>',
             '<style>body{max-width:1100px;margin:auto;font-family:system-ui} '
             'pre{white-space:pre-wrap;overflow-wrap:anywhere}td,th{vertical-align:top;'
             'border:1px solid #999;padding:12px}table{width:100%;table-layout:fixed}'
             'section{border-top:1px solid #999;padding:16px}</style></head><body>',
             '<h1>Novelty evidence</h1><p>These grades measure agreement with the source: '
             'source agreement is not coding quality. No skill edits are automatic.</p>',
             '<p>' + _text(population_description(evidence['population'])) + '</p>',
             '<details><summary>Provenance and usage</summary><pre>'
             + _text(json.dumps({k: evidence[k] for k in ('source_sha256', 'config_sha256',
                                                        'config', 'roles')}, indent=2))
             + '</pre></details>']
    for span in evidence['spans']:
        parts.append(f'<section id="{_id("span", span["id"])}"><p>'
                     + _text(span.get("source", "source"))
                     + f' · offsets {_text(span["start"])}–{_text(span["end"])}</p>'
                     + f'<pre>{_text(span["text"])}</pre>')
        linked = [a for a in evidence['atoms'] if span['id'] in a['span_ids']]
        for atom in linked:
            parts.append(f'<p><a href="#{_id("atom", atom["id"])}">'
                         + _text(atom['question']) + '</a></p>')
        if not linked:
            parts.append('<p>Unmeasured: no extracted atom.</p>')
        parts.append('</section>')
    parts.extend(_atom_html(a, evidence) for a in evidence['atoms'])
    return ''.join(parts) + '</body></html>'


def view_saved(args) -> None:
    """Prefer embedded evidence; retain the legacy two-file view interface."""
    from pathlib import Path

    from ..artifacts import load_artifact
    from .atoms import load_atoms
    from .attribute import attribute_spans
    from .report import render_annotated_html, render_annotated_markdown

    raw = json.loads(Path(args.novelty).read_text(encoding='utf-8'))
    out = Path(args.out or f'annotated.{args.format}')
    if raw.get('schema_version') == 'novelty_evidence.v1':
        evidence = load_artifact('novelty_evidence', args.novelty)
        if args.format != 'html':
            raise SystemExit('Evidence view requires --format html')
        rendered = render_evidence_html(evidence)
    else:
        artifact = load_artifact('novelty', args.novelty)
        if not args.atoms:
            raise SystemExit('Legacy novelty artifact needs --atoms; use evidence.json for full evidence')
        atoms, spans = load_atoms(args.atoms)
        if any(s.unread for s in spans):
            print('WARNING unread spans render as `unread`, not as text carrying nothing',
                  file=sys.stderr)
        render = render_annotated_html if args.format == 'html' else render_annotated_markdown
        rendered = render(attribute_spans(spans, atoms), title=artifact['artifact']['ref'])
        print('NOTE colours here come from spans only; legacy view has no saved probe outcomes.')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding='utf-8')
    print(f'OK Saved annotated view: {out}')


def trusted_population(inventory, verification_enabled, warnings):
    trusted = [a for a in inventory if a.faithfulness == "supported" and verification_enabled]
    population = {
        "policy": "supported-only-v1", "verification_enabled": verification_enabled,
        "n_inventory": len(inventory), "n_trusted": len(trusted),
        "n_quarantined": len(inventory) - len(trusted),
        "trusted_ids": [a.id for a in trusted],
        "quarantined_ids": [a.id for a in inventory if a not in trusted],
    }
    if population["n_quarantined"]:
        warnings.append("Unverified or unsupported atoms are quarantined; primary rates use "
                        "only supported atoms. No shortening candidate is promotable.")
    return trusted, population
