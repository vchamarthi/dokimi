### Fixed

- **`explore list` and `explore run` exit with a message on a malformed task file** —
  a task YAML that is not valid YAML or UTF-8, is not a mapping, lacks `id`,
  `query` or `references`, or has `references` or `repo_root` of the wrong shape
  now names the file instead of ending in a Python traceback.
