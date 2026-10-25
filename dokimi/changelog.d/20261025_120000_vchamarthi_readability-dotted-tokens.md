### Fixed

- **Dots inside hostnames, filenames and version numbers no longer count as
  sentence ends in the readability score** — `readability.score` counted every
  `.`, so a sentence mentioning `docs.example.com` or `2.5.1` looked like several
  short ones and scored far more readable than it is. A stop now ends a sentence
  only before whitespace or the end of the text (a closing quote or bracket may
  sit between).
