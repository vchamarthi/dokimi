# Hidden test for the feat-codex-profile task: a built-in `codex` harness profile that drives `codex exec`.
import contextlib
import io
import json
from pathlib import Path

import yaml

import dokimi
from dokimi.cli import build_parser
from dokimi.repos.events import TRACE_FORMATS
from dokimi.repos.harness_profile import PROFILE_DIR, builtin_profiles, load_profile


def test_profile_loads():
    assert Path(dokimi.__file__).resolve().is_relative_to(Path.cwd().resolve()), dokimi.__file__
    p = load_profile("codex")  # validates against harness_profile.v1 and the loader's own rules
    assert Path(p.path).resolve().parent == PROFILE_DIR.resolve(), p.path
    assert "codex" in builtin_profiles()
    assert p.name == "codex", p.name
    assert p.trace in TRACE_FORMATS, p.trace


def test_guidance_agents_md():
    names = load_profile("codex").guidance_names
    assert "AGENTS.md" in names and "CLAUDE.md" not in names


def test_invoke_ok():
    p = load_profile("codex")
    inv = list(p.invoke)
    assert inv.count("{tools}") == 1
    assert any("{prompt}" in e for e in inv) and any("{model}" in e for e in inv)
    prompt = 'say "hi"; {model} --x'
    for mode in ("none", "read", "all"):  # `repo run` defaults to --tools all
        argv = p.argv(prompt=prompt, model="gpt-x", tools=mode, bin_path="/opt/codex")
        assert argv[0] == "/opt/codex" and prompt in argv and "gpt-x" in " ".join(argv), (mode, argv)


def test_exec_mode():
    inv = list(load_profile("codex").invoke)
    pi = next(i for i, e in enumerate(inv) if "{prompt}" in e)
    assert "exec" in inv[:pi]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_reach_dry_run(tmp_path):
    root = tmp_path / "repo"
    _write(root, "AGENTS.md", "# root agents\n")
    _write(root, "CLAUDE.md", "# root claude\n")
    _write(root, "sub/AGENTS.md", "# sub agents\n")
    _write(root, "sub/CLAUDE.md", "# sub claude\n")
    _write(root, "docs/guide.md", "# guide\n")
    _write(root, "evals/Dockerfile", "FROM scratch\n")
    _write(root, "evals/tasks/.keep", "")
    _write(root, "evals/repo-eval.yaml", yaml.safe_dump({
        "schema": "repo_manifest.v1", "id": "toy", "eval_dir": "evals",
        "guidance": {"globs": ["docs/guide.md"]},
        "entry_points": {"grade": "python3 evals/grade.py {run_dir}"}, "tasks": "evals/tasks"}))
    args = build_parser().parse_args(["repo", "reach", str(root), "--manifest", "evals/repo-eval.yaml",
                                      "--harness", "codex", "--model", "m", "--dry-run"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        args.func(args)
    plan = json.loads(out.getvalue())
    files = set(plan["files"])
    assert {"AGENTS.md", "sub/AGENTS.md", "docs/guide.md"} <= files, files
    assert not files & {"CLAUDE.md", "sub/CLAUDE.md"}, files
    assert plan["runs"] and all(r["argv"][0] == "codex" for r in plan["runs"]), plan["runs"][:1]
