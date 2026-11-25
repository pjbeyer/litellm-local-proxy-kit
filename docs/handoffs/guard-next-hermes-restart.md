# Historical cutover handoff: IR-007 and IR-008

> Release-specific record, not instructions for a future restart. For current
> maintenance, see the [guard runbook](../../integrations/hermes/proxy-lifecycle-guard/README.md).
> The instructions and digests below apply only to this historical candidate.

> Use the commit containing this handoff on `feat/guard-incidence-reduction`.
> Stage and verify the three plugin source files against the digests below.
> Do not install from a moving branch, modify the live guard from within
> Hermes, or restart the LiteLLM proxy. The owner performs the cutover at the
> planned Hermes gateway restart; a source push does not schedule or activate it.
> Before cutover, confirm AC-005 passed for this candidate in the regression
> evidence. This handoff is not live activation approval.

## What is being released

IR-007 bounds direct HOME command-path resolution and still subjects the
resolved command to full guard analysis. IR-008 changes the under-limit Python
syntax-error category to `SHAPE_ENVELOPE` (`LGV-020`); malformed code remains
refused. There is no policy-schema, sink-schema, adapter, manifest, or watchdog
change. See [the regression evidence](ir007-synthetic-regression-evidence.md)
for exact test inputs and the independent-review disposition.

Run from `integrations/hermes/proxy-lifecycle-guard/` in the pinned checkout:

```sh
shasum -a 256 guard.py __init__.py plugin.yaml
```

- `guard.py`: `95934491474b7e76cdd76e933e0cd62466bdeeb2fcecc7cac1316b2d5c2d664a`
- `__init__.py`: `01d8478cd53213cdf410692551829b3f52951a0a132d76c5838d965778a9c43e`
- `plugin.yaml`: `f1a0ac7f14ddb649a5368c178b70dc062c685dcaa5d41259d1fe8dcecfb78da5`

## Owner cutover and rollback

1. From an owner shell outside the gateway, identify the intended profile's
   actual plugin directory. Verify the existing policy and integrity reference
   agree with the installed files, and check that the adapter and manifest
   match the hashes above. The expected predecessor `guard.py` is
   `9af90b1cf578bb42928a8ccfc01f417c988969e124a98b535e1e66cb1fef010a`.
   Stop on drift; do not overwrite an unexpected generation. Keep a private
   rollback copy of the predecessor evaluator and its matching integrity
   reference. Never put profile policy or integrity files in Git.
2. At the planned restart, with that Hermes gateway stopped and prevented
   from coming back until the swap is complete, replace only `guard.py` and
   update its SHA-256 in `integrity.sha256`. Preserve the adapter digest,
   policy bytes/digest, owner-only write permissions, and existing enable
   setting. Verify the entire resulting source/policy tuple before starting
   Hermes. An in-place swap while the old gateway serves calls can fail
   unrelated tools with `LGV-030`.
3. Start only the Hermes gateway. Verify the intended profile has the guard
   enabled **and loaded**, then verify a benign allow and an owner-approved
   non-executing refusal with the expected category. Confirm the refusal
   reaches the sink, proxy readiness is unchanged, and the configured
   watchdog is healthy. Report disk parity and live behavior separately:
   neither a restart nor sink activity proves the exact serving callback
   digest. If that identity can't be verified, keep AC-006 open. Do not
   resume previously denied diagnostics.
4. If checks fail, keep or return Hermes to a stopped state, restore the
   predecessor evaluator and its matching integrity reference as a pair,
   verify their consistency with the unchanged policy and adapter, then
   start Hermes and repeat the safe controls. Do not disable the guard to
   work around a failed check.

The versioned diagnostics migration and watchdog runtime-path investigation
remain separate work. Neither is needed to stage this source-only candidate.
