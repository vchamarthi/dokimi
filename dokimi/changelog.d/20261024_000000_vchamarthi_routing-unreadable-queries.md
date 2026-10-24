### Fixed

- **`novelty routing --queries`** exits with `cannot read --queries <file>: <reason>`
  when the file is missing, unreadable or not UTF-8, instead of ending in a Python
  traceback.
