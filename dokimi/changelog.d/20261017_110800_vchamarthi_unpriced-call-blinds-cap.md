### Fixed

- **`--max-cost-usd` stops when a call has no usage dict** — a tool-preference call that returns no `usage`, or a usage value that is not an object, is no price. The cap now stops starting further calls, the same way it already stops when `cost_usd` is null. A skipped call still does not count as unpriced.
