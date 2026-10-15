# Hidden test for the legacy-keys task: user-facing output says treatment/control; stored keys stay
# with_docs/without_docs. The grader sets DOKIMI_EVAL_START_SHA to the commit the agent started from.
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJ = Path.cwd()
WS = PROJ.parent
KEYS = {"with_docs", "without_docs"}
SCHEMA_DIR = "dokimi/dokimi/schemas"


def _git(*args):
    return subprocess.run(["git", "-C", str(WS), *args], capture_output=True, text=True, check=True).stdout


def _cli(args, cwd, timeout=300):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "GEMINI_API_KEY"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, str(PROJ / "cli.py"), *args], cwd=cwd, capture_output=True,
                       text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def _key_paths(node, path=""):
    out = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("properties", "$defs", "definitions", "patternProperties") and isinstance(v, dict):
                for name, sub in v.items():
                    p = f"{path}/{k}/{name}"
                    if name in KEYS:
                        out.add(p)
                    out |= _key_paths(sub, p)
            elif isinstance(v, (dict, list)):
                out |= _key_paths(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, x in enumerate(node):
            out |= _key_paths(x, f"{path}/{i}")
    return out


def test_schema_keys_kept():
    from dokimi.artifacts import SCHEMA_VERSIONS

    start = os.environ["DOKIMI_EVAL_START_SHA"]
    names = [n.split("/")[-1] for n in _git("ls-tree", "--name-only", f"{start}:{SCHEMA_DIR}").split()
             if n.endswith(".json")]
    base_paths = {}
    for n in names:
        paths = _key_paths(json.loads(_git("show", f"{start}:{SCHEMA_DIR}/{n}")))
        if paths:
            base_paths[n] = paths
    assert base_paths
    cur_dir = PROJ / "dokimi" / "schemas"
    kinds = {n.split(".v")[0]: paths for n, paths in base_paths.items()}
    problems = []
    # every start schema that had the keys still has them, at the same places
    for n, paths in base_paths.items():
        cur = cur_dir / n
        if not cur.exists():
            problems.append(f"schema {n} removed")
        elif paths - _key_paths(json.loads(cur.read_text(encoding="utf-8"))):
            problems.append(f"schema {n} lost keys")
    # a schema added since start for one of those kinds keeps them too (a new version renaming them is the same break)
    for cur in sorted(cur_dir.glob("*.json")):
        kind = cur.name.split(".v")[0]
        if cur.name not in base_paths and kind in kinds and kinds[kind] - _key_paths(
                json.loads(cur.read_text(encoding="utf-8"))):
            problems.append(f"new schema {cur.name} drops keys")
    # and the version the writers stamp is one of those
    for kind in ("answers", "eval"):
        cur = cur_dir / f"{SCHEMA_VERSIONS[kind]}.json"
        if kinds.get(kind, set()) - _key_paths(json.loads(cur.read_text(encoding="utf-8"))):
            problems.append(f"current {kind} schema {cur.name} drops keys")
    assert not problems, problems


@pytest.mark.timeout(300)
def test_writer_keys_kept(tmp_path):
    p = subprocess.run([sys.executable, str(PROJ / "tests" / "hidden_legacy_keys_writer.py"), str(tmp_path)],
                       cwd=PROJ, capture_output=True, text=True, timeout=300,
                       env=dict(os.environ, PYTHONPATH=str(PROJ), PYTHONDONTWRITEBYTECODE="1"))
    assert p.returncode == 0, (p.stdout + p.stderr)[-3000:]


def _sample_eval():
    def ev(i, w, wo, diff):
        return {"question_id": f"q{i}", "question_text": f"How do I do thing {i}?", "difficulty": diff,
                "category": "usage", "with_docs": {"aggregate": w}, "without_docs": {"aggregate": wo},
                "delta": w - wo}
    return {"schema_version": "eval.v1", "evaluated_at": "2026-01-01T00:00:00Z", "judge_model": "j",
            "judge_provider": "openai", "total_evaluations": 4,
            "evaluations": [ev(1, 80.0, 60.0, "beginner"), ev(2, 50.0, 70.0, "advanced"),
                            ev(3, 90.0, 40.0, "beginner"), ev(4, 65.0, 65.0, "intermediate")]}


def _table_headers(md):
    lines = md.splitlines()
    cells = []
    for i, line in enumerate(lines[:-1]):
        nxt = lines[i + 1].strip()
        if line.strip().startswith("|") and nxt.startswith("|") and set(nxt) <= set("|-: ") and "-" in nxt:
            cells += [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def _old_label(cell):
    c = cell.lower()
    if "treatment" in c or "control" in c:  # the new label, perhaps with the stored key beside it
        return False
    return ("baseline" in c or "with_docs" in c or "without_docs" in c
            or ("context" in c and "treatment" not in c)
            or re.fullmatch(r"(with|without)( avg| score)?", c) is not None)


def _md_problems(md, name):
    cells = _table_headers(md)
    headings = [line for line in md.splitlines() if line.startswith("#")]
    problems = []
    if not any("treatment" in c.lower() for c in cells):
        problems.append(f"{name}: no treatment header")
    if not any("control" in c.lower() for c in cells):
        problems.append(f"{name}: no control header")
    old = [c for c in cells if _old_label(c)] + [h for h in headings if "baseline" in h.lower()]
    if old:
        problems.append(f"{name}: old labels left {old[:8]}")
    return problems


def _both(text):
    t = text.lower()
    return "treatment" in t and "control" in t


@pytest.fixture(scope="module")
def surfaces(tmp_path_factory):
    """Run every surface the prompt names once; return (display problems, reports read stored runs)."""
    tmp = tmp_path_factory.mktemp("legacy_keys")
    (tmp / "eval").mkdir()
    ev_path = tmp / "eval" / "X.json"
    ev_path.write_text(json.dumps(_sample_eval()), encoding="utf-8")
    qs_path = tmp / "questions.json"
    qs_path.write_text(json.dumps({"questions": [
        {"id": f"q{i}", "text": f"How do I do thing {i}?", "category": "usage"} for i in range(1, 5)]}),
        encoding="utf-8")
    problems, reads = [], True

    rc, out, err = _cli(["report", "eval", "--product", "X", "--eval", str(ev_path), "--out", str(tmp / "r_eval.md")], tmp)
    md = (tmp / "r_eval.md").read_text(encoding="utf-8") if rc == 0 else ""
    problems += [f"report eval rc={rc}: {err[-800:]}"] if rc else _md_problems(md, "report eval")
    summary_line = [line for line in out.splitlines() if "delta" in line.lower()]
    if not any(_both(line) for line in summary_line) or re.search(r"\bwith=|\bwithout=", out):
        problems.append(f"report eval console summary: {summary_line}")
    reads &= rc == 0 and "71.2" in md and "58.8" in md

    rc, out, err = _cli(["report", "generate", "--product", "X", "--eval", str(ev_path),
                         "--questions", str(qs_path), "--output", str(tmp / "r_gen.md")], tmp)
    md = (tmp / "r_gen.md").read_text(encoding="utf-8") if rc == 0 else ""
    problems += [f"report generate rc={rc}: {err[-800:]}"] if rc else _md_problems(md, "report generate")
    reads &= rc == 0 and "71.2" in md and "58.8" in md

    rc1, out1, _ = _cli(["baseline", "save", "--from-eval", str(ev_path), "--product", "X", "--name", "b1"], tmp)
    rc2, out2, _ = _cli(["baseline", "list"], tmp)
    rc3, out3, _ = _cli(["baseline", "compare", "--eval", str(ev_path), "--baseline", "b1"], tmp)
    if not (rc1 == 0 and _both(out1) and not re.search(r"\bwith=|\bwithout=", out1)):
        problems.append(f"baseline save rc={rc1}: {out1[-400:]}")
    header = next((line for line in out2.splitlines() if "Name" in line and "Product" in line), "")
    if not (rc2 == 0 and _both(header) and not re.search(r"\bwith\b|\bwithout\b", header)):
        problems.append(f"baseline list rc={rc2}: {header}")
    if not (rc3 == 0 and _both(out3) and not re.search(r"baseline avg|context avg|context arm", out3, re.I)):
        problems.append(f"baseline compare rc={rc3}: {out3[-400:]}")

    # benchmark preflight exits 1 without API keys, after printing the resolved plan. The stored key may stay
    # beside the new label ("control (without_docs)"): users must see treatment/control.
    rc, out, _ = _cli(["benchmark", "preflight", "--library", "onetbb", "--doc-source",
                       "url:http://127.0.0.1:9/docs", "--output-dir", str(tmp / "bench")], tmp)
    if not ("Resolved benchmark plan" in out and _both(out)):
        problems.append(f"benchmark preflight rc={rc}: {out[-1200:]}")
    return problems, reads


@pytest.mark.timeout(600)
def test_reads_stored_runs(surfaces):
    assert surfaces[1]


@pytest.mark.timeout(600)
def test_display_changed(surfaces):
    assert not surfaces[0], surfaces[0]
