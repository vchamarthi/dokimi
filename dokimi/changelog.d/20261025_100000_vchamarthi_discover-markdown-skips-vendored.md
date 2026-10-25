### Fixed

- **The static benchmark no longer scores vendored markdown** — a spec without a
  `golden_manifest` discovers every `.md` under `docs/`; that now skips
  `.git`, `node_modules`, `.venv`, `venv`, `__pycache__` and `site-packages`
  directories, so a package README inside a docs tree no longer enters the
  per-document scores and their average.
