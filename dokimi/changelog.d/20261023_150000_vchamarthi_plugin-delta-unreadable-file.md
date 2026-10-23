### Fixed

- **`arms plugin-delta`** exits 1 with a message naming the file, not a traceback, when
  `--baseline-json` or `--plugin-json` cannot be read (a directory, or no permission); only
  a missing file used to get the message.
