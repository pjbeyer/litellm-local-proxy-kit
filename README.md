# LiteLLM Local Proxy Kit

An experimental MIT-licensed kit for running a pinned, loopback-only LiteLLM proxy on Apple Silicon. It includes a managed installer, runtime provenance checks, a restart controller, and an original routing experiment. The public repository is source and reference material, not a supported release or a copy of a running deployment.

## Maintain an existing deployment

1. In a dedicated kit worktree, run `make help`. The installed policy selects the kit, deployment, and controller inputs. Never point installed policy at the task worktree.
2. Start with `make status` and `make health`. For a change within the installed generation, edit the reviewed deployment source, review `make preview`, then use `make apply` and `make verify`. Staging does not reload the service. Changes to the runtime path, loaded job definition, or other boot-bound inputs may require a separate owner transition and fresh qualification; successful staging does not prove eligibility.
3. If activation is needed, follow the [restart controller runbook](docs/restart-controller-operations.md). `make prepare` rehearses a managed candidate and may use real credentials, database/cache state, and inference. It is not an isolated test or authorization. The owner approves the exact fresh plan; after the matching private approval record exists, submit its request ID and asset digest. Reconcile an uncertain outcome before trying again.

## Choose the right guide

- **First install, migration, or verified rollback:** [Managed macOS service](docs/macos-managed-service.md).
- **Controller enrollment, qualification, or recovery:** [Restart controller operations](docs/restart-controller-operations.md); requirements and interfaces are in [Specs](specs/README.md).
- **Runtime build and pin evidence:** [Native runtime artifact contract](docs/native-runtime-artifact-contract.md). The [example deployment inputs](deployments/personal/) are reference artifacts, not a complete install target.
- **Optional Hermes guard:** [Lifecycle guard runbook](integrations/hermes/proxy-lifecycle-guard/README.md). The guard is separate from the proxy and controller and is not OS isolation.
- **Original callback and routing experiment:** [Routing reference](docs/routing-experiment.md) and [compatibility evidence](docs/compatibility.md). Historical provider limits and recorded test results are not current entitlements or activation instructions.

## Verify without touching a deployment

From an isolated worktree, set `TMPDIR` to a short, existing, private directory
with no symlinked ancestors (macOS `/var` is commonly a symlink), then run the
complete offline suite. No provider credentials are needed:

```sh
uv run --no-project --with pytest --with pyyaml python -m pytest -q tests
```

The suite uses fixtures; it does not qualify a live controller, prove provider access, or approve a restart. See [project tooling](docs/project-tooling.md) for contributor setup and [publication checks](docs/publication-readiness.md) before sharing artifacts. Kit Beads and Git attribution are public; keep resolved credentials, private records, generated workstation configuration, prompts, and raw response or session evidence out of public Git and its auxiliary refs.
