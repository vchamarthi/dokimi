### Fixed

- **`repo run` on a batch whose `_eval/batch.json` is damaged exits 1 with a message.** A pin file that is not valid JSON, or not a JSON object, now prints `✗ batch <name> has an unreadable pin (<path>); use another --batch`. Before, invalid JSON printed a bare parser message without the file, and a valid non-object (`[]`, a string) ended in an `AttributeError` traceback.
