# Project provisioning baseline

## Scope

Retrofit the operating files of the generic project template in the existing
public kit. Preserve application behavior, the Beads instance, and deployment
authority. This is not a new-origin provisioning run.

## Requirements and acceptance

- **FR-001 — Reproducible development baseline.** Record the immutable template
  and Spec Kit sources; carry the Hermes shell scaffold, first-party extension,
  and constitution. On a clean checkout the pinned CLI must recognize the
  integration and extension, and the prerequisite script must resolve a real
  feature without an approval receipt or workstation configuration.
- **FR-002 — Public project, private deployment.** Keep existing tracking and
  attribution mechanisms. Do not reinitialize Beads, enroll services, ship
  private state, or change operator/restart authority. Validate the changed
  tree and offline suite with no deployment credentials.
- **FR-003 — Review-gated releases.** Pin CI actions to full commits. Release
  automation may write only after owner opt-in on `main`; a source release
  requires a separately reviewed and merged release PR. Maintain the `simple`
  manifest and version file without updating deployment pins or approvals.
  Verify the config against the schema used by the pinned action, lint the
  workflows, and test the default-disabled and `main`-only conditions.
  Do not create a test release in the public repository.
