# Source Import Notes

The original source kit was first stored in a macOS-protected user location. Metadata reads worked, but byte reads and copies from the Hermes process failed with `Resource deadlock avoided` until the source directory was moved to a project-accessible location.

## Import safeguards applied

- The coherent `auto-free-kit` implementation was inspected before import.
- Source logs, backups, duplicate working files, personal agent configuration, and service/LaunchAgent artifacts were excluded.
- Absolute user paths were replaced with `$HOME` paths where needed.
- The local endpoint key placeholder was replaced with the environment reference `os.environ/LOCAL_OMLX_API_KEY`.
- Checksums were regenerated after sanitization.

## Reuse rule

For future imports, move or copy source artifacts to an accessible staging location first, inspect contents, and run no-secret/no-private-path scans before staging files in this repository.
