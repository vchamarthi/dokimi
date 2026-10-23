### Fixed

- **`personas approve` exits 1 with a message on a file it cannot validate** —
  a persona file that is not a JSON object, whose `personas` is not a list of
  objects, that is not valid UTF-8, or that cannot be read (for example a
  directory) now prints `✗ ...` and exits 1 instead of ending in a Python
  traceback.
