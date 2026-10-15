# dokimi's own repository evals

The coding jobs agents are given on this repository, run and graded by
[repoevals](https://github.com/vchamarthi/repoevals) v0.4.1 (`repoevals.toml` at the repository root). The ten
tasks, their base commit, prompts, reference patches and hidden tests are the ones in
[intel/dokimi#57](https://github.com/intel/dokimi/pull/57). Grading is deterministic and offline; no grader calls
an LLM.

## Running

Build the image once from the repository root, then run repoevals inside it (Linux or macOS host with Docker):

```bash
docker build -f .agent-evals/Dockerfile -t dokimi-eval .
docker run --rm --network none --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:$PWD" -w "$PWD" dokimi-eval repoevals check --source .
```

`repoevals check` proves every task without an agent: the untouched workspace fails, the reference solution
passes, and the review task rejects a finding on every changed line. To run agents:

```bash
repoevals run --agent claude-code --arm guidance:raw --arm no-agent-files --repeats 3 --docker dokimi-eval
```

`no-agent-files` removes only `AGENTS.md` and `CLAUDE.md`. The built-in `guidance:none` also removes the READMEs
and CONTRIBUTING files listed in `[guidance].globs`, which `feat-static-metric` and `rename-cli-flag` read.

## Graders

| task | grader | gate |
|---|---|---|
| `fix-degrade-pre`, `fix-docker-reward`, `feat-codex-profile`, `rename-cli-flag` | `dokimi_code` | `strict_pass`, `ruff_ok`, `mypy_ok` |
| `feat-static-metric` | `dokimi_code` | `strict_pass`, `ruff_ok`, `mypy_ok`, `schema_ok`, `config_ok` |
| `legacy-keys` | `dokimi_code` | `strict_pass`, `ruff_ok`, `mypy_ok`, `compat_ok` |
| `schema-required-field` | `dokimi_code` | `strict_pass`, `compat_ok`, `ruff_ok`, `mypy_ok`, `schema_ok` |
| `docs-new-page` | `dokimi_code` | `strict_pass` |
| `review-pr-conventions` | `review_seeded` | `recall>=0.6`, `precision>=0.5`, `decoys_flagged<=0` |
| `use-repo-scan` | `dokimi_scan` | `manifest_ok`, `answer_names_claim_1`, `answer_names_claim_2` |

`dokimi_code` (`graders/dokimi.py`) is repoevals' `hidden_tests` grader plus the commands CI runs on the agent's
tree: `ruff check .`, `mypy`, `dokimi.config_check`, `dokimi.schema_check` and `dokimi.schema_compat` against the
start commit. `strict_pass` is the hidden tests and the whole suite (the `regress_pass` of #57, with the same three
deselected tests). The agent's edits to existing tests are kept, as in #57.

`dokimi_scan` grades `use-repo-scan`. repoevals has no grader family for using the tool on a user's repository, so
the checks of #57's `check.py` stay as a repository grader.

`review-pr-conventions` is scored by `review_seeded`: the agent ends its answer with a JSON array of findings, each
matched to one of five seeded defects by file, line and keyword. #57 scored recall only, by regular expressions over
the prose answer, with no pass/fail. `review_seeded` also scores precision and flags the decoy (line 71 of
`grounding.py`, the correct `< min_contexts` bound). The two scores are different metrics and are not comparable.

## What #57's files became

| #57 | here | disposition |
|---|---|---|
| `.agent-eval/repo-eval.yaml` | `repoevals.toml` | dropped |
| `.agent-eval/run.py` | repoevals, `graders/dokimi.py` | dropped |
| `.agent-eval/Dockerfile` | `Dockerfile` | kept, plus one line installing repoevals |
| `.agent-eval/README.md` | `README.md` | rewritten |
| `tasks/*/task.json` | `tasks/*/task.toml`, `tasks/*/instruction.md` | rewritten; prompts verbatim |
| `tasks/*/oracle.patch`, `setup.patch`, `review.patch` | same paths | kept |
| `tasks/fix-*/hidden/` | same paths | kept |
| `tasks/*/check.py` (six code tasks) | `tasks/*/hidden/dokimi/tests/test_hidden_*.py` | rewritten as hidden tests |
| `tasks/use-repo-scan/check.py` | `graders/dokimi.py` (`dokimi_scan`) | rewritten as a grader |
| `tasks/use-repo-scan/oracle_answer.md` | same path | kept |
| `tasks/review-pr-conventions/task.json` `findings` | `manifest.json` | rewritten as seeds and a decoy |
| `tasks/review-pr-conventions/oracle_answer.md` | `oracle_answer.json` | rewritten as findings |

Not carried over:

- Process metrics of `run.py` (`ran_pytest`, `ran_ci_local`, `committed`, `signed_off`, `artifacts_added`,
  `mypy_overrides_grew`, `read_docs`) are not scored. repoevals reports process data from the trace and its
  integrity checks flag cheating, such as edited tests or reading hidden files.
- The changed-line coverage metrics (`diffcov_*`), which no #57 gate named.
- `check.py` metrics no gate named: `flags_known`, `run_parser_ok`, `tests_added` and `docs_updated` in
  `feat-codex-profile`; `compare_diff`, `arch_listed` and `report_md` in `feat-static-metric`; `old_manifest_ok`
  in `legacy-keys`; `docs_name_new`, `tests_mention_new` and `code_free_of_old` in `rename-cli-flag`;
  `page_shows_manifest` in `docs-new-page`.
- `budget_usd` and `max_turns`. repoevals limits each attempt by `[agent].timeout_sec`; `repoevals run --max-cost`
  stops a whole run once the reported cost reaches a total, which is not a per-task budget.
