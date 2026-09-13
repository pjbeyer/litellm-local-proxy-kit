# Publication Readiness Checklist

This repository is an import-stage local project, not yet a public release.

## Complete for this import

- [x] Reviewed each candidate source artifact before import.
- [x] Excluded raw runtime logs, backups, personal Hermes configuration, and machine-specific launch files.
- [x] Replaced source-specific absolute paths with generic `$HOME` paths or placeholders.
- [x] Replaced the secret-shaped local test placeholder with an environment-variable reference.
- [x] Ran secret-shaped-value and private-path scans on the staged repository.
- [x] Ran offline policy tests, syntax/YAML checks, checksum verification, and independent Codex review.

## Required before a public remote or release

- [ ] Select and record an OSS license.
- [ ] Identify and pin a LiteLLM version that passes the deployment-hook lifecycle test in `docs/compatibility.md`.
- [ ] Re-run `preflight.py --router-test` with that pinned environment.
- [ ] Verify each provider/model name, quota policy, and data-handling term at release time; do not rely on source-experiment observations.
- [ ] Add a portable macOS service guide only after separate specification and review. Do not publish a workstation plist or script.
- [ ] Add dependency/install metadata and a declared supported Python/LiteLLM environment.
- [ ] Add CI that runs offline tests and publication-hygiene scans.
- [ ] Run a final independent review of the release candidate.
