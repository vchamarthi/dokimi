### Fixed

- **An intent domain with a blank `products:` or `name:` is listed** —
  `library intents` and `library list --domain` dropped a domain whose
  `products:` was left empty in `intents.yaml` (YAML null) as if it were
  malformed, and a blank `name:` printed as `None`. A blank field now reads as
  unset: the domain is listed with no products, and its name falls back to its
  key. A `products:` that is not a list is still skipped with a warning.
