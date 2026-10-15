# Hidden test for the docs-new-page task: the page exists, runs `repo scan`, and is in the nav and the docs index.
import re
from pathlib import Path

PAGE = "repo-scan-quickstart.md"


def _page() -> str:
    page = Path("docs") / PAGE
    return page.read_text(encoding="utf-8") if page.exists() else ""


def test_page_exists():
    assert len(_page().strip()) > 200


def test_page_runs_repo_scan():
    assert re.search(r"repo scan\b", _page())


def test_in_mkdocs_nav():
    nav = Path("mkdocs.yml").read_text(encoding="utf-8")
    assert "\nnav:" in nav
    assert PAGE in nav[nav.find("\nnav:"):]


def test_in_docs_index():
    index = Path("docs/README.md").read_text(encoding="utf-8")
    assert re.search(r"\]\(\.?/?" + re.escape(PAGE) + r"\)", index)
