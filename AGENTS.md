# Agent Instructions — LiteLLM Local Proxy Kit

This is an exploratory, OSS-oriented repository for a local LiteLLM proxy methodology and configuration kit.

## Scope

- Document reproducible local LiteLLM proxy setup and verification on Apple Silicon.
- Preserve only reviewed, sanitized artifacts from the original local experiment.
- Keep publication hygiene front-and-center: no secrets, no private identifiers, no raw logs, and no machine-specific service configuration.

## Rules

- Do not copy source artifacts without content review and a no-secret/no-private-path scan.
- Do not commit `.env`, API keys, LiteLLM master keys, proxy secrets, raw request logs, or profile-local credentials.
- Use environment-variable references in examples; never put resolved credential values in configuration.
- Keep LaunchAgent/service setup out of scope unless a separate portable, reviewed guide is added.
- Run local scans before staging: secrets, private paths, merge markers, and zero-byte artifacts.
- Run independent Codex review before committing a substantive import or behavioral change.

## Tracking

Until this repository has its own tracker, keep project planning in the designated external issue tracker rather than duplicating a markdown backlog here.
