### Fixed

- **`catalog check` granularity hint** — its `next:` line named `dokimi catalog granularity`,
  a command that does not exist. It now names the paid check that does,
  `catalog routing <root> --queries-by-skill QUERIES.json --model haiku`, and says what
  `QUERIES.json` holds. The JSON `next` block reads `status: "available"` and carries `needs`.
