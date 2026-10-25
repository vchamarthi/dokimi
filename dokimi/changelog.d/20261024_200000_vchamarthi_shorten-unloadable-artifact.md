### Fixed

- **`novelty shorten`** exits with `cannot load --artifact <ref>: <reason>` when the
  artifact is missing, unreadable or not UTF-8, as `novelty analyze` does, instead of
  ending in a Python traceback.
