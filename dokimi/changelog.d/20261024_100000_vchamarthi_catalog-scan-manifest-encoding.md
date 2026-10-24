### Fixed

- **`catalog scan`** names the manifest file when `skills.yaml` or `evaluation/harbor/suites.json`
  is not UTF-8 (`invalid skills.yaml at <path>: ...`), and reports a manifest it cannot read
  (`cannot read skills.yaml at <path>: ...`), instead of an unattributed decode error.
