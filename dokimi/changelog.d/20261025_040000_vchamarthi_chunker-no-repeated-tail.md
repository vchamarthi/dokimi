### Fixed

- **A document that ends inside a chunk no longer gets a second, repeated chunk** —
  `chunk_text` stopped only when its next start passed the end, so text whose
  last chunk already reached the end (an 800-character page, with the default
  size) was followed by a chunk holding only the overlap again. The `chunks`
  count in the static benchmark rows is now the number of distinct chunks.
