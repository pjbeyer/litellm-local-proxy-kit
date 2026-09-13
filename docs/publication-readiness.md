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
- [ ] Replace the validation-only candidate and disposable `requirements-pilot.txt` manifest with reproducible, stable-release dependency/install metadata and a declared supported Python/LiteLLM environment.
- [ ] Resolve the RC proxy authentication dependency gap documented in `docs/isolated-rc-pilot-result.md`; the current manifest is not a production lockfile.
- [ ] Repeat and pass every remaining pilot gate recorded in `docs/isolated-rc-pilot-result.md`, including approved provider validation and a client-executed function-tool round trip, before release consideration.
- [ ] Resolve the local forced-tool incompatibility documented in `docs/local-tool-compatibility.md` and add a loopback regression before representing the local route as tool capable.
- [ ] Add CI that runs offline tests and publication-hygiene scans.
- [ ] Run a final independent review of the release candidate.
