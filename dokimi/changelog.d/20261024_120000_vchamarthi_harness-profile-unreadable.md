### Fixed

- **`repo budget` and `repo reach`** print `✗ <profile>: cannot read: <reason>` and exit 1
  when `--harness` names a profile file that is not UTF-8 or cannot be read, instead of
  ending in a Python traceback. `dokimi.repos.harness_profile.load_profile` raises
  `ProfileError` for these files, where it raised `OSError` or `UnicodeDecodeError` before.
