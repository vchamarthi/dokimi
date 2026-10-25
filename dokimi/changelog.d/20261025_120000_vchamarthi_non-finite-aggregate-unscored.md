### Fixed

- **A `NaN` or `Infinity` aggregate is unscored** — the records reader no longer
  returns a non-finite aggregate (which `json.loads` accepts) as a score, so one
  such value cannot turn a report's mean into `nan`. It reads as unscored, like
  any other non-numeric aggregate. Finite scores are unchanged.
