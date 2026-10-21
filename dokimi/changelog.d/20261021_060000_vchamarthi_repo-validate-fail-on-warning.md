### Added

- **`repo validate --fail-on-warning`** — exits 1 when the validation report has
  any warning, as `repo budget --fail-on-warning` does, so a CI job can gate on
  a guidance glob that matches nothing or a task base SHA that is not 40
  characters. Without the flag a warning still exits 0.
