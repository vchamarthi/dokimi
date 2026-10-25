### Fixed

- **A citation that quotes only the path, `` `src/a.py`:10-20 ``, is read as a
  citation** — the exploration parser used to count it as malformed inside a
  `<final_answer>` block and miss it in the loose scan, which lowered the
  validity rate and the file and line scores of an answer that located the
  right lines.
