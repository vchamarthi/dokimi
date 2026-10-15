# Hidden test for the rename-cli-flag task: `benchmark run --multi-run` is now `--repeats`, in code and docs.
import contextlib
import io
import re
from pathlib import Path

import pytest

import dokimi
from dokimi.cli import build_parser
from dokimi.commands import benchmark
from dokimi.registry import ProductEntry

PROJECT = Path.cwd()
ROOT = PROJECT.parent
OLD = re.compile(r"--multi-run(?![\w-])")


def _run_benchmark(monkeypatch, tmp_path, extra):
    """Parse `benchmark run` with ``extra`` and run it with the pipeline stubbed out."""

    class Registry:
        def get(self, key):
            assert key == "onetbb", key
            return ProductEntry(key="onetbb", name="oneTBB", description="x", doc_sources=["local:docs"])

    calls = []

    def fake_single(entry, **kwargs):
        calls.append(kwargs)
        return {"library": entry.key, "name": entry.name, "status": "ok",
                "result": {"steps": {"evaluation": {"summary": {"with_avg": 80.0}}}}}

    monkeypatch.setattr(benchmark, "_load_registry", lambda args: Registry())
    monkeypatch.setattr(benchmark, "_run_single_library", fake_single)
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = str(tmp_path / "onetbb_eval")
    args = build_parser().parse_args(["benchmark", "run", "--target", "onetbb", "--output-dir", out, *extra])
    with contextlib.redirect_stdout(io.StringIO()):
        args.func(args)
    return [str(c.get("output_dir", "")).replace(out, "OUT") for c in calls]


def test_new_flag_ok(monkeypatch, tmp_path):
    assert Path(dokimi.__file__).resolve().is_relative_to(PROJECT.resolve()), dokimi.__file__
    assert _run_benchmark(monkeypatch, tmp_path / "a", ["--repeats", "3"]) == ["OUT_run1", "OUT_run2", "OUT_run3"]
    assert _run_benchmark(monkeypatch, tmp_path / "b", []) == ["OUT"]


def test_old_flag_gone():
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["benchmark", "run", "--target", "onetbb", "--multi-run", "3"])
    assert exc.value.code != 0


def test_docs_updated():
    files = sorted((PROJECT / "docs").rglob("*.md"))
    files += [PROJECT / "README.md", ROOT / "README.md", ROOT / "CONTRIBUTING.md", PROJECT / "CONTRIBUTING.md"]
    stale = [str(f.relative_to(ROOT)) for f in files if f.is_file() and OLD.search(f.read_text(encoding="utf-8"))]
    assert not stale, f"still name --multi-run: {stale}"
