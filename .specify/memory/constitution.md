# LiteLLM Local Proxy Kit Constitution

## I. Public source, private deployments

Ship reviewed source, contracts, examples, and public project records. Keep
resolved credentials, private records, raw session evidence, and workstation
configuration out of all public artifacts. Repo visibility never authorizes
publishing deployment secrets or changing a live service.

## II. Preserve operator authority

Use a dedicated worktree. Do not bind installed policy to it or mutate shared
services as part of development. Stage, qualify, approve, and activate as
separate steps. Only the supported controller flow or an explicitly authorized
operator transition may change a managed deployment.

## III. Specify consequential changes and verify offline

Use Beads for backlog and status; use `specs/` for contracts and acceptance.
For consequential changes, keep requirements, plan, tasks, and applicable
validation gates in agreement. Missing approval evidence is not approval.
Cover refusals as well as success with offline tests and get independent review
of substantive imports or behavior changes. Do not claim live acceptance from
an offline test.

## IV. Keep maintenance simple

Prefer existing tools and small, atomic changes. Update operational guidance
with the behavior it describes. A release PR qualifies source for publication,
not a build for installation.

## Governance

[AGENTS.md](../../AGENTS.md) governs day-to-day work; feature contracts add
specific gates. Amend principles through a reviewed change and document any
migration of affected specs or gates.

**Version**: 1.0.0 | **Last Amended**: 2026-11-26
