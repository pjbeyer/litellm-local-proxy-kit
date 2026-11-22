# IR-007 synthetic fixture evidence

Source-only, fixture-only execution. This is **not** a byte-exact replay of the
stopped historical combined diagnostic. No payload was executed, no real PID was
discovered, and no installed source, policy or process was changed. The fresh
module and declared snapshot/native-operation fences are in
`tests/test_guard_quoted_control_flow.py`. The synthetic manifest is
`tests/fixtures/ir007-home-synthetic.json`; it pins the raw UTF-8 payload,
fixture policy and discovery, tool metadata, environment delta, and original
IR-008-only evaluator digest. Its only named falsification delta is `HOME` from
`/fixture/home` to the empty string. The candidate test asserts full inline-code
analysis for the allow and `VETO` for the empty value. It is not a live result.

## Baseline red

Baseline `guard.py` SHA-256:
`9427cde6b95b8feaf4b04459dbeed6566499d80fbe83e968f8d210466817de4e`
(working source already containing IR-008). On Python 3.14.10 / pytest 9.2.0,
with `XDG_STATE_HOME` and `TMPDIR` set to resolved disposable scratch paths:

```sh
XDG_STATE_HOME="$SCRATCH/state" TMPDIR="$SCRATCH/tmp" \
  .venv-tests/bin/python -m pytest -q tests/test_guard_home_resolution.py
```

Historical result: **exit 1; 23 failed, 23 passed**. This was the original
46-case test version, which was not frozen before further tests were added.
The baseline source hash and raw output remain recorded, but that original red
battery is not byte-reproducible from the final test file. Do not use this count
as the final-candidate red gate; a frozen baseline run with the final tests is
required. Subsequent tests were added during remediation. Failures included
unresolved direct HOME, unsafe HOME shadow/fallback, quoted/prefix cases, and
the synthetic pair's allow-side assertion. Baseline raw output SHA-256:
`b5c9060af317a60c673a5a313791c2d1ccd44a2d15f0afeb08b29ce05a443552`.
The file is local scratch evidence, not a published raw log.

## Focused candidate green

```sh
XDG_STATE_HOME="$SCRATCH/state" TMPDIR="$SCRATCH/tmp" \
  .venv-tests/bin/python -m pytest -q \
    tests/test_guard_home_resolution.py \
    tests/test_guard_var_defaults_and_editors.py \
    tests/test_guard_quoted_control_flow.py \
    tests/test_guard_prose_and_vars.py \
    tests/test_guard_quotation_fp.py
```

Result: **exit 0; 200 passed**. Candidate hashes at execution:

- `integrations/hermes/proxy-lifecycle-guard/guard.py`:
  `d40b2f93dff5ae366f492a48b857b53133e86f5c76d470618388aed042a51257`
- `tests/test_guard_home_resolution.py`:
  `5074a52c09bcd8f1443b1438e69398981eff2754bdb0c6b1a2f9f9de3aa0931d`
- `tests/fixtures/ir007-home-synthetic.json`:
  `97626681055531b7b542b24a0420c03d012a4c394553fee88c6ddb608393135d`
- `tests/test_guard_prose_and_vars.py`:
  `c440d02e49a7953c98993176f7edc312cfbc043049cd1f31353de3f2417f20b6`
- `tests/test_guard_quotation_fp.py`:
  `313b1cb2cc4beec0e67321f3dc60b08c729a96912476d70a1bb049e497069514`
- Focused output SHA-256:
  `f46c32b411ae44837f514c11e72e272c3360cfaa84d2ecd0f3154905f8eb357e`

Focused tests additionally exercise empty/unresolved, repeated, recursive and
over-bound bindings, unsupported wrapper shapes, safe same-payload precedence
against an unsafe inherited HOME, prefix collisions, preserved quoting, all
pipeline positions, and protected-intent controls with fixture discovery.
Assignment visibility is deliberately conservative: only a unique,
unconditional standalone HOME assignment in the leading unconditional
assignment prefix is accepted; later or conditional
assignment-like occurrences refuse instead of guessing. Other bounded variable
assignments require a leading unconditional assignment prefix. Unsupported
shapes can therefore produce additional fail-closed refusals.

Full suite, exact combined-candidate independent review and owner-led live
activation remain separate gates. This evidence does not prove production sink
non-mutation by a live comparison; the executed fixtures redirected state and
fenced native process operations.
