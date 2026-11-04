---
depends_on:
  - dokimi/dokimi/commands/ci.py
  - dokimi/dokimi/runner/ci_*.py
  - action.yml
  - .github/workflows/dokimi-*.yml
---
<!--
Copyright (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# CI integration

Content stays in its repository. Pin an installed Dokimi revision, pass the content
checkout to `dokimi ci`, and retain the resulting evidence. A deterministic check
and a measured improvement are different results: a static pass never qualifies
content for a behavioral baseline.

## Check a pull request without model credentials

Call the reusable workflow from the content repository. Replace both engine
references with a reviewed, immutable commit in the engine repository you use.
The example deliberately leaves that value as a placeholder.

```yaml
name: Dokimi
on: [pull_request, merge_group]
permissions:
  contents: read
  pull-requests: read
jobs:
  checks:
    uses: intel/dokimi/.github/workflows/dokimi-pr.yml@REVIEWED_COMMIT
    with:
      dokimi-ref: REVIEWED_COMMIT
      mode: check
      subject-kind: catalog
      config: .github/dokimi/config.yaml
      policy: .github/dokimi/policy.yaml
```

Use `dokimi-repository` to choose a fork and change the workflow repository in
`uses` to the same fork. A reusable workflow's revision is distinct from its
`dokimi-ref` input; pin both. Never use a candidate-controlled framework checkout
as a privileged publisher or baseline writer.

The free job installs the pinned engine outside the candidate checkout, checks
content as data, uploads one run receipt, and writes a scoped job summary. It
executes no candidate scripts. Candidate configuration and policy must parse;
the approved base supplies policy authority. When a first integration adds a
policy that does not exist in the base, the engine's default policy applies.
A candidate cannot lower its own thresholds.

GitHub validates permissions for every job in a called workflow before evaluating
job conditions. The caller must therefore allow pull request read access even
when selecting the free mode; the free job itself requests only content read
access.

For a repository containing agent guidance rather than a skill catalog, choose
`subject-kind: repo`. This runs repository readiness checks. It does not measure
whether the guidance helps an agent.

The local equivalent, after installing Dokimi, is:

```bash
dokimi ci check /path/to/content --changed-from BASE_SHA \
  --out-dir /path/to/receipts
```

The output must be a dedicated directory outside the source checkout. Reusing it
clears earlier receipts before the new attempt, so a failed attempt cannot leave
an earlier successful run available for upload.

## Measure one subject against its approved suite

An existing skill has three arms: no skill, approved skill, and candidate skill.
A new skill has no-skill and candidate arms. The approved base supplies the
questions, tasks, grader configuration and policy. Head-only evaluation material
cannot qualify the candidate against a yardstick it just introduced.

Use an explicit model, provider, independent judge and budget:

```bash
dokimi ci run /path/to/content --changed-from BASE_SHA \
  --base-root /path/to/clean-base --subject skill/example \
  --model MODEL --provider PROVIDER \
  --judge-model JUDGE --judge-provider JUDGE_PROVIDER \
  --reps 3 --max-usd 5 --out-dir /path/to/measured-receipts
```

The base checkout must be clean and match the pinned comparison revision. The
candidate checkout must be committed and clean. The runtime adapter verifies
content and suite digests before measurement. A run that exceeds its budget,
misses cells, cannot resolve the actual model, or encounters an infrastructure
error cannot pass the behavioral gate. An in-flight provider call can exceed the
remaining monetary cap; budget enforcement stops subsequent calls and records
the stop.

Prompt questions use `arms-runner` and measure answers against their assertions.
They do not establish successful execution of generated code. Native executable
skill tasks can use `docker-claude` through the direct CLI; they require a suitable
Docker host and a matching supported provider. Run them on the architecture and
hardware the task requires.

For a Claude subscription, see [subscription backends](subscription-backends.md).
`claude-cli` uses the authenticated Claude Code CLI and removes API credentials
from the child environment. Its reported list price is not a subscription bill;
unknown billed cost must remain unknown. Apply an explicit request limit for
subscription measurement. Do not copy a personal login into a content checkout.

```bash
dokimi ci run /path/to/content --changed-from BASE_SHA \
  --base-root /path/to/clean-base --subject skill/example \
  --model sonnet --provider claude-cli \
  --judge-model haiku --judge-provider claude-cli \
  --reps 1 --max-calls 36 --out-dir /path/to/subscription-receipts
```

For hosted subscription runs, store `DOKIMI_CLAUDE_CODE_OAUTH_TOKEN` in the
protected `dokimi-live` environment. The calling job must pass that exact secret
name, even when its value exists only in the environment:

```yaml
secrets:
  DOKIMI_CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.DOKIMI_CLAUDE_CODE_OAUTH_TOKEN }}
```

The reusable runtime job selects the environment through its `environment`
input, then prefers this named environment token. The optional
`claude-code-oauth-token` alias accepts an explicitly forwarded repository
secret as a fallback. Neither token is referenced by the free check job.
GitHub requires the caller to pass the same name before an environment secret
becomes available to a reusable workflow; an omitted name resolves to an empty
string. See [GitHub's reusable workflow secret rules](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows#using-inputs-and-secrets-in-a-reusable-workflow).

Both providers must use the subscription transport. API runs require
`--max-usd`; subscription runs require `--max-calls`. A completed run can still
fail or be inconclusive: the request cap controls usage, not the outcome.

## Gate and report the evidence

Every run directory contains `run.json`, `plan.json`, `result.json`, `gate.json`
and `report.md`. The run receipt binds source repository, head and base commits,
subject content, suite, framework, model, harness, grader and policy identities.
The versioned schemas ship in the installed wheel.

```bash
dokimi ci gate /path/to/measured-receipts/run.json --policy /path/to/approved-policy.yaml
dokimi ci report /path/to/measured-receipts/run.json \
  --policy /path/to/approved-policy.yaml --out /path/to/report.md
```

Supply the same approved policy when recomputing a decision. Repetition reduces
measurement noise; the statistical unit is the task. The gate checks task-level
regression against the approved arm and lift against the no-context arm. A
passing individual cell is not a passing comparison.

| Exit | Meaning |
|---:|---|
| 0 | Pass within the stated scope; static remains ineligible for behavioral promotion |
| 1 | Failed checks or behavioral threshold |
| 2 | Invalid command or unusable input |
| 3 | Inconclusive or not run, including a budget stop |
| 4 | Invalid measurement evidence |
| 5 | Infrastructure failure |

Missing owned tasks, imported content assessed only structurally, and head-only
new questions must not be presented as measured skill utility.

## Run models and publish results in GitHub Actions

The reusable model job requires explicit dispatch from a trusted default-branch
controller, an exact open PR number and head/base pair, a protected environment,
and an explicit budget. Pass only the selected provider secrets; do not use
`secrets: inherit`. It rechecks the current PR before starting. Its hosted paid
path supports data-only catalog questions with `arms-runner`; executable worker
isolation needs a separate deployment.

Name the trusted dispatch run exactly:

```yaml
run-name: 'dokimi:model pr=${{ inputs.pr }} head=${{ inputs.head }} base=${{ inputs.base }}'
```

The publisher authenticates the workflow run through GitHub's API, checks its
repository, workflow path, controller revision, attempt, PR and current source
revisions, and validates the bounded JSON artifact. It neither checks out nor
executes artifact content. A stale head or base requires a new measurement.

A manually dispatched model job is informational. GitHub does not attach that
job as a required check on the measured PR head. Configure a separate GitHub App
check if a required behavioral status is needed; do not treat a green dispatch
run as merge authorization.

## Record and revalidate a baseline

Only passing live evidence is promotable. The record workflow additionally
verifies that the measured PR merged and that the merged content and suite still
match the measured identity. It writes compact receipts to a dedicated baseline
branch with signed-off commits and a Git lease; it does not push the source
branch. A concurrent writer cannot silently overwrite another result.

For a local store:

```bash
dokimi ci record /path/to/measured-receipts/run.json --root /path/to/merged-content \
  --expected-head MEASURED_HEAD_SHA --policy /path/to/approved-policy.yaml \
  --store /path/to/baseline-store --expect-previous none
```

`none` means create-only. Replacing a baseline requires its exact previous
`baseline_id`. Subject and model have separate namespaces. Framework, suite,
grader, harness or policy drift requires fresh measurement and explicit
revalidation; an old score cannot certify a new model.

```bash
dokimi ci revalidate /path/to/content --baseline /path/to/current.json \
  --changed-from BASE_SHA --base-root /path/to/clean-base \
  --model NEW_MODEL --provider PROVIDER \
  --judge-model JUDGE --judge-provider JUDGE_PROVIDER \
  --reps 3 --max-usd 5 --out-dir /path/to/revalidation-receipts
```

Revalidation checks the recorded receipt before calling a model and keeps the
recorded subject content and suite fixed. Promote fresh passing evidence with
an explicit compare-and-swap token. Keep old model receipts for comparison.
