### Fixed

- **Localization scoring pools reference ranges for one file** —
  `score_localization` no longer lets a reference key such as `./a.py` replace
  the ranges of `a.py` once both normalize to the same path. Line recall,
  precision and compactness count every reference range for the file.
