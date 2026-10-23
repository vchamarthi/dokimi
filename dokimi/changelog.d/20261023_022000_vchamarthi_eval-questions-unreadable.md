### Fixed

- **`eval checklist` and `eval cross-judge --grounded` exit 2 on an unreadable
  `--questions` file** — a missing, malformed or non-UTF-8 questions file now
  prints `Error: ...` naming the file and exits 2 (bad input, not a failed
  gate) instead of ending in a Python traceback.
