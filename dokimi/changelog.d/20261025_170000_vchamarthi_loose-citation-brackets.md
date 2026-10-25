### Fixed

- **A citation in parentheses or brackets names the file** — when an explorer's
  answer has no `<final_answer>` block, `parse_final_answer` scans for
  `path:line` tokens. `(src/a.py:10-20)`, `[src/a.py:5]` and `<src/a.py:1>` were
  read as the path `(src/a.py`, so the file never matched the reference and
  file and line recall scored 0. The path no longer starts with an opening bracket.
