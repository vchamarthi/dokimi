### Fixed

- **A `SKILL.md` that is a directory is no longer read as a skill** — `arms run
  --arms skill:<dir>` now exits 1 with `No SKILL.md found in skill directory`
  instead of an `IsADirectoryError` traceback, and `skill-catalog:` discovery
  skips such an entry instead of crashing.
