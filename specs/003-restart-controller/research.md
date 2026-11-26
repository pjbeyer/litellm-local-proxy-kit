# Research and decisions

- Keep one runbook and reuse `make` + `owner_maintenance.py`: the dispatcher already fronts the installer, health and installed client. A new runner would duplicate routing and stage/restart separation.
- The install-only utility already supports `--verify` using the bound source and intended policy/plist. Keep that as the documented bundle verification path. A one-target offline binding diagnostic is a proposed convenience, not a reimplementation of qualification or a replacement for it.
- Move controller-specific contracts and historical plans under this feature. Leave shared deployment, native provenance and guard interception contracts with their owners. Preserve dated incident evidence instead of importing an unreviewed, machine-local reconciliation packet.
- Retain approval, qualification, service activation and recovery authority with the operator. Automated tests use disposable inputs; they do not create live authorization.
