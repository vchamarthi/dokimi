# Hidden test for the feat-static-metric task: a `code_block_lang` static metric, wired where the other metrics are.
import copy
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROJ = Path.cwd()
M = "code_block_lang"
TOL = 1.5e-3

DOC_A = """# Alpha

Some prose about the tool.

```bash
ls -la
```

```python
print("hi")
```

```
plain block
```

```yaml
key: value
```
"""  # 3 of 4 labelled -> 0.75

DOC_B = """# Beta

Text.

```
one
```

```console
$ echo two
```

```
three
```
"""  # 1 of 3 labelled -> 0.3333

EXPECT = {"docs/a.md": 0.75, "docs/b.md": 1 / 3}
SUMMARY_OK = ((0.75 + 1 / 3) / 2, 4 / 7)


def _close(v, target):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and abs(float(v) - target) <= TOL


def _metric_of(d):
    if not isinstance(d, dict):
        return None
    if M in d:
        return d[M]
    for k in ("metrics", "scores"):
        if isinstance(d.get(k), dict) and M in d[k]:
            return d[k][M]
    return None


def _spec():
    return yaml.safe_load((PROJ / "benchmarks" / "spec.v1.yaml").read_text(encoding="utf-8"))


def test_module_ok():
    score = importlib.import_module(f"dokimi.metrics.{M}").score
    cases = [(DOC_A, 0.75), (DOC_B, 1 / 3), ("```sh\nx\n```\n", 1.0), ("```\nx\n```\n\n```\ny\n```\n", 0.0)]
    for text, want in cases:
        assert _close(score(text), want), (text, score(text), want)


def test_spec_wired():
    spec = _spec()
    m = spec.get("metrics", {}).get(M)
    assert M in spec.get("weights", {}) and isinstance(m, dict) and m.get("enabled", False)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("code_block_lang")
    root = tmp / "root"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text(DOC_A, encoding="utf-8")
    (root / "docs" / "b.md").write_text(DOC_B, encoding="utf-8")
    s = copy.deepcopy(_spec())
    s["golden_manifest"] = {"min_docs": 1, "max_docs": 100, "include": ["docs/**/*.md"], "exclude": []}
    ex = s.get("metrics", {}).get("example_pass_rate")
    if isinstance(ex, dict):
        ex["enabled"] = False  # never execute fixture code blocks
    sp = tmp / "spec.yaml"
    sp.write_text(yaml.safe_dump(s, sort_keys=False), encoding="utf-8")
    js = tmp / "snap.json"
    p = subprocess.run([sys.executable, "cli.py", "run", "--root", str(root), "--spec", str(sp),
                        "--out-json", str(js), "--out-md", str(tmp / "run.md")],
                       cwd=PROJ, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, (p.stdout + p.stderr)[-1500:]
    return json.loads(js.read_text(encoding="utf-8"))


@pytest.mark.timeout(300)
def test_run_per_doc(snapshot):
    docs = {d.get("path"): d for d in snapshot.get("docs", [])}
    for path, want in EXPECT.items():
        assert _close(_metric_of(docs.get(path)), want), (path, docs.get(path))


@pytest.mark.timeout(300)
def test_run_summary(snapshot):
    sv = _metric_of(snapshot.get("summary", {}))
    assert any(_close(sv, t) for t in SUMMARY_OK), sv


def test_readme_listed():
    text = (PROJ / "README.md").read_text(encoding="utf-8")
    m = re.search(r"^#+\s*Static benchmark metrics\s*$(.*?)(?=^#{1,2}\s)", text, re.M | re.S | re.I)
    section = m.group(1) if m else ""
    assert f"`{M}`" in section or re.search(rf"^\|.*\b{M}\b.*\|", section, re.M)


def test_test_refs():
    assert any(M in p.read_text(encoding="utf-8", errors="replace")
               for p in (PROJ / "tests").rglob("*.py") if not p.name.startswith("test_hidden_"))
