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

## Restart controller and worktree isolation

Use a dedicated Git worktree for all kit work, including read-only repository
investigation and merge verification. Do not edit the shared checkout or point
installed policy at a disposable worktree. Start with [controller operations](docs/restart-controller-operations.md)
and `make help`; use [specs/](specs/README.md) for contracts and acceptance.
Dated evidence is not a procedure or permission to retry a refused operation.

## Tracking

Use Beads for backlog and delivery status. Treat this kit's Beads records and Git attribution metadata as public; keep prompts and private records out. Keep behavior contracts and acceptance criteria under `specs/`; do not duplicate the backlog there. See [project tooling](docs/project-tooling.md) for the repository and Hermes setup boundaries.
