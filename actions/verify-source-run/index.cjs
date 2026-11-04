// Copyright (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

module.exports = async (github, context, core) => {
const { inflateRawSync } = require('zlib');
const { owner, repo } = context.repo;
const repository = `${owner}/${repo}`;
const fail = why => { throw new Error(`Rejected Dokimi receipt: ${why}`); };
const sha = value => typeof value === 'string' && /^[0-9a-f]{40}$/.test(value);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const MAX = 256 * 1024;
const marker = '<!-- dokimi-ci-receipt -->';
function jsonFromZip(zip) {
  if (zip.length > MAX || zip.length < 22) fail('archive size');
  const end = zip.length - 22;
  if (zip.readUInt32LE(end) !== 0x06054b50 || zip.readUInt16LE(end + 20) !== 0) fail('ZIP end');
  if (zip.readUInt16LE(end + 8) !== 1 || zip.readUInt16LE(end + 10) !== 1) fail('ZIP member count');
  const central = zip.readUInt32LE(end + 16);
  if (central + 46 > end || zip.readUInt32LE(central) !== 0x02014b50) fail('ZIP directory');
  const length = zip.readUInt16LE(central + 28);
  const name = zip.subarray(central + 46, central + 46 + length).toString('utf8');
  if (name !== 'run.json') fail('ZIP member name');
  const size = zip.readUInt32LE(central + 24);
  const compressed = zip.readUInt32LE(central + 20);
  const local = zip.readUInt32LE(central + 42);
  if (size > MAX || compressed > MAX || local + 30 > central) fail('ZIP member size');
  if (zip.readUInt32LE(local) !== 0x04034b50 || zip.readUInt16LE(local + 6) & 1) fail('ZIP local header');
  const localLength = zip.readUInt16LE(local + 26);
  if (zip.subarray(local + 30, local + 30 + localLength).toString('utf8') !== name) fail('ZIP local name');
  const offset = local + 30 + localLength + zip.readUInt16LE(local + 28);
  if (offset + compressed > central) fail('ZIP overlap');
  const payload = zip.subarray(offset, offset + compressed);
  const method = zip.readUInt16LE(central + 10);
  let raw;
  if (method === 0) raw = payload;
  else if (method === 8) raw = inflateRawSync(payload, { maxOutputLength: MAX });
  else fail('ZIP compression');
  if (raw.length !== size) fail('ZIP decompressed size');
  return JSON.parse(raw.toString('utf8'));
}
function candidateFromRun(run) {
  if (run.event === 'pull_request') {
    if (run.pull_requests?.length !== 1) fail('PR association');
    const pr = run.pull_requests[0];
    if (pr.head?.sha !== run.head_sha) fail('run head association');
    return { number: pr.number, head: run.head_sha, base: pr.base?.sha };
  }
  if (run.event !== 'workflow_dispatch') fail('source event');
  if (!sha(process.env.CONTROLLER_SHA) || run.head_sha !== process.env.CONTROLLER_SHA) fail('controller SHA');
  if (run.head_repository?.full_name !== repository) fail('controller repository');
  const receipt = /^dokimi:model pr=([1-9][0-9]*) head=([0-9a-f]{40}) base=([0-9a-f]{40})$/.exec(run.display_title || '');
  if (!receipt) fail('controller receipt title');
  return { number: Number(receipt[1]), head: receipt[2], base: receipt[3] };
}
const schema = {"$schema":"https://json-schema.org/draft/2020-12/schema","$comment":"Copyright (C) 2026 Intel Corporation; SPDX-License-Identifier: Apache-2.0","type":"object","additionalProperties":false,"$id":"https://github.com/intel/dokimi/schemas/ci_run.v1.json","title":"CI run bundle v1","required":["schema_version","mode","policy_digest","plan","result","source","identity","observations","budget_stopped"],"properties":{"schema_version":{"const":"ci_run.v1"},"mode":{"enum":["static","live"]},"policy_digest":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"},"plan":{"$ref":"#/$defs/plan"},"result":{"$ref":"#/$defs/result"},"source":{"$schema":"https://json-schema.org/draft/2020-12/schema","$comment":"Copyright (C) 2026 Intel Corporation; SPDX-License-Identifier: Apache-2.0","type":"object","additionalProperties":false,"required":["repository","head_sha","base_sha"],"properties":{"repository":{"type":"string","minLength":1},"head_sha":{"type":"string","pattern":"^[0-9a-f]{40}$"},"base_sha":{"type":"string","pattern":"^[0-9a-f]{40}$"},"checked_sha":{"type":"string","pattern":"^[0-9a-f]{40}$"}}},"identity":{"$schema":"https://json-schema.org/draft/2020-12/schema","$comment":"Copyright (C) 2026 Intel Corporation; SPDX-License-Identifier: Apache-2.0","type":"object","additionalProperties":false,"required":["subject","kind","content_digest","suite_digest","framework_digest","model","harness","grader_digest"],"properties":{"subject":{"type":"string","pattern":"^[a-zA-Z0-9][a-zA-Z0-9_.-]*(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?$"},"kind":{"type":"string","minLength":1},"content_digest":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"},"suite_digest":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"},"framework_digest":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"},"model":{"type":["string","null"],"minLength":1},"harness":{"type":"string","minLength":1},"grader_digest":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"}}},"observations":{"type":"array","items":{"$schema":"https://json-schema.org/draft/2020-12/schema","$comment":"Copyright (C) 2026 Intel Corporation; SPDX-License-Identifier: Apache-2.0","type":"object","additionalProperties":false,"required":["cell_id","score","cost_usd","elapsed_sec","discovered"],"properties":{"cell_id":{"type":"string","minLength":1},"score":{"type":["number","null"],"minimum":0,"maximum":1},"cost_usd":{"type":["number","null"],"minimum":0},"elapsed_sec":{"type":"number","minimum":0},"discovered":{"type":["boolean","null"]},"agent_calls":{"type":"integer","minimum":0}}}},"budget_stopped":{"type":"boolean"},"runtime":{"type":"object","additionalProperties":false,"required":["requested_model","provider","judge_model","judge_provider","reps"],"properties":{"requested_model":{"type":"string","minLength":1},"provider":{"type":"string","minLength":1},"judge_model":{"type":"string","minLength":1},"judge_provider":{"type":"string","minLength":1},"reps":{"type":"integer","minimum":1},"billing":{"const":"subscription"},"transport":{"const":"claude-cli"}},"dependentRequired":{"billing":["transport"],"transport":["billing"]},"allOf":[{"if":{"required":["billing"],"properties":{"billing":{"const":"subscription"}}},"then":{"properties":{"provider":{"const":"claude-cli"},"judge_provider":{"const":"claude-cli"}}}}]},"repository_runtime":{"type":"object","additionalProperties":false,"required":["manifest","harness","harness_version","execution_id","agent_network"],"properties":{"manifest":{"type":"string","minLength":1},"harness":{"type":"string","minLength":1},"harness_version":{"type":"string","minLength":1},"execution_id":{"type":"string","minLength":1},"agent_network":{"const":"bridge"}}}},"$defs":{"plan":{"$schema":"https://json-schema.org/draft/2020-12/schema","title":"Run plan v1","description":"What a run is going to measure, declared before any paid call: the subjects and suites it reads, the models, harnesses and grader it uses, the one variable its arms vary, and every cell (arm x model x task x rep) it promises to run. A result.v1 points back at a plan by plan_digest (dokimi.runner.plan.plan_digest); completeness is computed from the plan's cells, never from what happened to finish.","type":"object","required":["schema_version","subjects","suites","models","harnesses","grader","varied_variable","cells","budget"],"additionalProperties":false,"properties":{"schema_version":{"const":"plan.v1"},"generated_at":{"type":"string","description":"Left out of plan_digest: re-stamping a plan does not change it."},"subjects":{"type":"array","description":"What is under test: a skill, repository, doc source or profile, pinned by commit and content.","items":{"type":"object","required":["id","sha","fingerprint"],"additionalProperties":false,"properties":{"id":{"type":"string","minLength":1},"kind":{"type":"string"},"sha":{"type":["string","null"],"description":"commit the subject was read at; null when it is not in git"},"fingerprint":{"type":["string","null"],"description":"digest of the subject's content as the run sees it"}}}},"suites":{"type":"array","minItems":1,"items":{"type":"object","required":["id","hash","reasons"],"additionalProperties":false,"properties":{"id":{"type":"string","minLength":1},"hash":{"type":"string","minLength":1,"description":"digest of the suite's task/question content"},"reasons":{"type":"array","items":{"type":"string"},"description":"why this suite was selected (changed file, label, policy)"}}}},"models":{"type":"array","minItems":1,"items":{"type":["string","null"]},"description":"requested model ids; null for a cell that calls no model (an oracle or probe harness)"},"harnesses":{"type":"array","minItems":1,"items":{"type":"string","minLength":1}},"grader":{"type":"object","required":["id","digest"],"additionalProperties":false,"properties":{"id":{"type":"string","minLength":1},"digest":{"type":["string","null"],"description":"digest of the verifier or judge prompt the run grades with"}}},"varied_variable":{"description":"The one thing the arms differ in. A list names a factorial design (2x2: code and guidance), not several free variables.","oneOf":[{"type":"string","minLength":1},{"type":"array","minItems":1,"uniqueItems":true,"items":{"type":"string","minLength":1}}]},"cells":{"type":"array","minItems":1,"items":{"type":"object","required":["cell_id","arm","model","task","rep"],"additionalProperties":false,"properties":{"cell_id":{"type":"string","minLength":1,"description":"dokimi.runner.plan.cell_id(arm, model, task, rep): stable across re-plans; completeness refuses an id that does not match the coordinates"},"arm":{"type":"string","minLength":1,"description":"the full condition the cell runs under. When a plan crosses several harnesses, the harness is part of the arm (e.g. docker-claude/skill), so cell ids stay distinct; a cell has no separate harness key"},"model":{"type":["string","null"]},"task":{"type":"string","minLength":1},"rep":{"type":"integer","minimum":1}}}},"budget":{"type":"object","required":["cells"],"additionalProperties":false,"properties":{"cells":{"type":"integer","minimum":0,"description":"number of distinct planned cells; completeness refuses a plan where it differs"},"agent_calls":{"type":["integer","null"],"minimum":0},"cost_usd":{"type":["number","null"],"minimum":0},"wall_sec":{"type":["number","null"],"minimum":0}}}}},"result":{"$schema":"https://json-schema.org/draft/2020-12/schema","title":"Run result v1","description":"One status per cell of the plan.v1 the run was given (plan_digest). A planned cell absent here is not_run; dokimi.runner.plan.completeness computes planned minus completed and refuses to call a run complete while any planned cell lacks a measured status (pass, fail or inconclusive).","type":"object","required":["schema_version","plan_digest","cells"],"additionalProperties":false,"properties":{"schema_version":{"const":"result.v1"},"generated_at":{"type":"string"},"plan_digest":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"},"execution_id":{"type":["string","null"],"description":"the run's execution id, when the producer has one"},"cells":{"type":"array","items":{"type":"object","required":["cell_id","status"],"additionalProperties":false,"properties":{"cell_id":{"type":"string","minLength":1},"status":{"enum":["pass","fail","inconclusive","invalid","infra_error","not_run"]},"model":{"type":["string","null"],"description":"the model that answered (resolved), which may differ from the planned id"},"detail":{"type":["string","null"]}}}}}}}};
const supported = new Set(['$schema','$comment','$id','$defs','title','description','type','const','enum',
  '$ref','oneOf','allOf','if','then','else','dependentRequired','additionalProperties','required','properties',
  'items','minLength','pattern','minimum','maximum','minItems','uniqueItems']);
function valid(value, rule) {
  if (Object.keys(rule).some(k => !supported.has(k))) return false;
  if (rule.allOf && !rule.allOf.every(r => valid(value, r))) return false;
  if (rule.if && !valid(value, valid(value, rule.if) ? (rule.then || {}) : (rule.else || {}))) return false;
  if (rule.$ref) return valid(value, rule.$ref.split('/').slice(1).reduce((s, k) => s[k], schema));
  if (rule.oneOf && rule.oneOf.filter(r => valid(value, r)).length !== 1) return false;
  if ('const' in rule && value !== rule.const) return false;
  if (rule.enum && !rule.enum.includes(value)) return false;
  const types = { object: object(value), array: Array.isArray(value), string: typeof value === 'string',
    number: typeof value === 'number' && Number.isFinite(value), integer: Number.isSafeInteger(value),
    boolean: typeof value === 'boolean', null: value === null };
  if (rule.type && ![rule.type].flat().some(t => types[t])) return false;
  if (typeof value === 'string' && (value.length < (rule.minLength || 0) || (rule.pattern && !new RegExp(rule.pattern).test(value)))) return false;
  if (typeof value === 'number' && (value < (rule.minimum ?? -Infinity) || value > (rule.maximum ?? Infinity))) return false;
  if (Array.isArray(value)) {
    if (value.length < (rule.minItems || 0)) return false;
    if (rule.uniqueItems && new Set(value.map(v => JSON.stringify(v))).size !== value.length) return false;
    if (rule.items && !value.every(v => valid(v, rule.items))) return false;
  }
  if (object(value)) {
    if (Object.entries(rule.dependentRequired || {}).some(([key, required]) =>
      Object.hasOwn(value, key) && required.some(k => !Object.hasOwn(value, k)))) return false;
    if ((rule.required || []).some(k => !Object.hasOwn(value, k))) return false;
    if (rule.additionalProperties === false && Object.keys(value).some(k => !Object.hasOwn(rule.properties || {}, k))) return false;
    if (Object.entries(rule.properties || {}).some(([k, r]) => Object.hasOwn(value, k) && !valid(value[k], r))) return false;
  }
  return true;
}
function validateResult(result, candidate) {
  if (!valid(result, schema)) fail('result schema');
  const source = result.source;
  if (source.repository !== repository || source.head_sha !== candidate.head || source.base_sha !== candidate.base) fail('result source');
  if (!['static', 'live'].includes(result.mode) || !object(result.result)) fail('result shape');
  if (!/^sha256:[0-9a-f]{64}$/.test(result.policy_digest || '')) fail('policy digest');
  if (process.env.EXPECTED_POLICY && result.policy_digest !== process.env.EXPECTED_POLICY) fail('trusted policy differs');
}
const id = Number(process.env.SOURCE_RUN);
const attempt = Number(process.env.SOURCE_ATTEMPT);
if (!Number.isSafeInteger(id) || id <= 0 || !Number.isSafeInteger(attempt) || attempt <= 0) fail('run identity');
const { data: run } = await github.rest.actions.getWorkflowRun({ owner, repo, run_id: id });
const { data: workflow } = await github.rest.actions.getWorkflow({ owner, repo, workflow_id: process.env.SOURCE_WORKFLOW });
if (run.id !== id || run.run_attempt !== attempt || run.status !== 'completed') fail('run attempt or status');
if (run.repository?.full_name !== repository || run.workflow_id !== workflow.id) fail('workflow repository or ID');
if (workflow.path !== process.env.SOURCE_WORKFLOW || run.path.split('@')[0] !== workflow.path) fail('workflow path');
if (process.env.ALLOW_MERGED === 'true' && run.event !== 'workflow_dispatch') fail('promotion requires approved controller');
const candidate = candidateFromRun(run);
if (!Number.isSafeInteger(candidate.number) || candidate.number <= 0 || !sha(candidate.head) || !sha(candidate.base)) fail('candidate identity');
const { data: pr } = await github.rest.pulls.get({ owner, repo, pull_number: candidate.number });
if (pr.base?.repo?.full_name !== repository) fail('PR repository');
if (process.env.ALLOW_MERGED === 'true' ? !pr.merged : pr.state !== 'open') fail('PR state');
if (pr.head?.sha !== candidate.head || pr.base?.sha !== candidate.base) fail('stale head or base');
if (run.event === 'pull_request' && pr.head?.repo?.full_name !== run.head_repository?.full_name) fail('head repository');
const artifacts = await github.paginate(github.rest.actions.listWorkflowRunArtifacts, { owner, repo, run_id: id });
const matches = artifacts.filter(a => a.name === `dokimi-run-${id}-${attempt}` && !a.expired && a.workflow_run?.id === id);
if (matches.length !== 1) fail('artifact identity');
const artifact = matches[0];
if (!Number.isSafeInteger(artifact.size_in_bytes) || artifact.size_in_bytes > MAX || artifact.size_in_bytes <= 0) fail('artifact size');
const { data } = await github.rest.actions.downloadArtifact({ owner, repo, artifact_id: artifact.id, archive_format: 'zip' });
const result = jsonFromZip(Buffer.from(data));
validateResult(result, candidate);
if (process.env.ALLOW_MERGED === 'true' && result.mode !== 'live') fail('promotion requires live evidence');
if (process.env.VERIFIED_OUT) {
  if (process.env.ALLOW_MERGED === 'true') {
    if (!sha(pr.merge_commit_sha)) fail('merged revision');
    core.setOutput('merged-sha', pr.merge_commit_sha);
    core.setOutput('head-sha', candidate.head);
  }
  require('fs').writeFileSync(process.env.VERIFIED_OUT, JSON.stringify(result));
  return;
}
const body = `${marker}\nDokimi received an informational result for commit \`${candidate.head}\`. ` +
  `This receipt does not certify efficacy or establish a required PR check. ` +
  `[Review the run and artifacts](https://github.com/${repository}/actions/runs/${id}/attempts/${attempt}).`;
const comments = await github.paginate(github.rest.issues.listComments, { owner, repo, issue_number: candidate.number });
const existing = comments.find(c => c.user?.login === 'github-actions[bot]' && c.body?.startsWith(marker));
if (existing) await github.rest.issues.updateComment({ owner, repo, comment_id: existing.id, body });
else await github.rest.issues.createComment({ owner, repo, issue_number: candidate.number, body });

};
