# Agent2 Edit Contract

- Prefer YAML `edits` with `replace_function` for small, reviewable changes.
- A replacement function must keep the original callable surface compatible.
- A full-file `files` package is allowed only when it preserves imports, entrypoint behavior, output contract, and copied dependency imports.
- Rejected edits should be repaired using the exact rejection reason before changing strategy.

