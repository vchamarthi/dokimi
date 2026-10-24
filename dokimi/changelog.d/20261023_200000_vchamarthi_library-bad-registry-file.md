### Fixed

- **`library list`, `library show` and `library intents` exit 1 with a message on a
  bad `--registry` or `--intents` file** — a path that is not a file, or a file that
  is unreadable, not UTF-8 or not valid YAML, now prints `Error: ...` naming the flag
  instead of a Python traceback (or, for a missing path, a silent "Registry is empty.").
