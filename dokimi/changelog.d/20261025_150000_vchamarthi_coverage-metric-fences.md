### Fixed

- **The static `coverage` score no longer counts `#` comment lines inside code
  blocks as headings** — a shell snippet with six `# comment` lines used to
  score like a doc with six sections. Fenced blocks are now read by markdown
  rules: `~~~` fences count, a longer fence is not closed by a shorter one, and
  an unclosed block is not counted.
