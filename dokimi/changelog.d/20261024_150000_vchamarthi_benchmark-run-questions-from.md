### Fixed

- **`benchmark run --questions-from`** prints `Error: questions source not found: <path>` or
  `Error: questions source invalid: <path> (<reason>)` and exits 1 before any pipeline starts,
  when the question set is missing or not a valid question set, instead of ending in a Python
  traceback. `benchmark preflight` reports the same two messages.
