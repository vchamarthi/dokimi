# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""`eval grounding`: an unwritable --output exits 1 with a message."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dokimi.cli import build_parser


def _grounding(tmp_path: Path, output: Path) -> None:
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"answers": [
        {"question_id": "q1", "question_text": "How do I loop?",
         "with_docs": {"answer": "use parallel_for",
                       "retrieved_docs": [{"snippet": "parallel_for runs tasks"}]}},
    ]}), encoding="utf-8")
    args = build_parser().parse_args(["eval", "grounding", "--answers", str(answers), "--output", str(output)])
    args.func(args)


def test_grounding_report_is_written(tmp_path):
    out = tmp_path / "nested" / "grounding.json"
    _grounding(tmp_path, out)
    assert json.loads(out.read_text(encoding="utf-8"))["schema_version"] == "grounding.v1"


def test_output_below_a_file_exits_1_naming_the_path(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")  # a regular file: nothing can be created below it
    target = blocker / "grounding.json"
    with pytest.raises(SystemExit) as exc:
        _grounding(tmp_path, target)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert f"Error: cannot write {target}" in err
    assert "Traceback" not in err


def test_output_that_is_a_directory_exits_1_naming_the_path(tmp_path, capsys):
    target = tmp_path / "is-a-dir"
    target.mkdir()
    with pytest.raises(SystemExit) as exc:
        _grounding(tmp_path, target)
    assert exc.value.code == 1
    assert f"Error: cannot write {target}" in capsys.readouterr().err
