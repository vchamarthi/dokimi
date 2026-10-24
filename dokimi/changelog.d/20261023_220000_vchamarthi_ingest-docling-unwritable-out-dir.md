### Fixed

- **`ingest docling`** prints `Error: ingest into <out-dir> failed: <reason>` and exits 2
  when `--out-dir` is a file or cannot be written, or when reading or converting a source
  document raises an `OSError`, instead of ending in a Python traceback.
