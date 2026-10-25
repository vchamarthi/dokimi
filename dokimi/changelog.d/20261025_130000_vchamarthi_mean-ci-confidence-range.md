### Fixed

- **`mean_ci` rejects a confidence level outside (0, 1)** — with SciPy
  installed the Student-t path passed it straight to `t.ppf` and returned `nan`
  (or a collapsed) interval bounds, where `bootstrap_ci` already raised
  `ValueError`. Both paths now raise the same error.
