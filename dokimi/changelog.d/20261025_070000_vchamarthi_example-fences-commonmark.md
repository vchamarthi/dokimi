### Fixed

- **`example_pass_rate` finds `~~~` fences and nested fences** — example
  extraction only knew three-backtick fences, so a `~~~python` example was never
  run (the document still scored a perfect pass rate), and a four-backtick block
  showing a fenced example was cut at the inner fence. A fence is now ``` or
  `~~~`, closed by a fence of the same character at least as long, so a shorter
  fence inside is content. Fences are still found at any indentation.
