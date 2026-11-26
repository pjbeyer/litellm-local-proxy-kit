---
name: project-operating-baseline
description: Use when working inside a provisioned project repository to follow its operating baseline — Beads as the canonical backlog, SpecKit for consequential changes, Conventional Commits, secret hygiene, and never touching the shared Dolt service. Applies to mid-repository work, commit hygiene, and completion verification.
---

# Project operating baseline

This repository adopts the project provisioning template’s operating baseline.
Follow [the kit’s agent instructions](../../../AGENTS.md) for project-specific
requirements.

## Canonical backlog

- Use Beads as the single durable backlog. File meaningful side work as a Beads
  issue and close it with evidence rather than leaving it only in chat.

## Consequential changes

- Use SpecKit for anything that changes a contract, a source of truth, a safety
  boundary, or what an automation is permitted to do. A wording-only or
  single-guardrail fix is fine as prose plus a Beads issue.

## Commits

- Commit atomically — one logical change per commit — using Conventional
  Commits (`<type>(<scope>): <description>`), imperative, no trailing period.
- A sync or closeout commit must not become a catch-all.

## Secrets

- Never write credentials in plaintext in tracked or shared locations, logs, or
  URLs. Keep them in a secret store and inject them command-scoped. Fail closed
  when the unlock gate is unavailable.

## Shared services

- Never start, stop, restart, or reconfigure the shared Dolt service from this
  repository. Use the existing listener only.

## Scope

- This operating baseline is not application implementation; it is the agreed
  operating layer for the repository.

## Completion

- Before declaring work done: run applicable checks, commit atomically, verify
  Git and Beads/Dolt synchronization independently, and share the result — work
  is not complete until it is shipped and shared.