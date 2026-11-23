# IR-007 synthetic fixture evidence

## Latest disposition — round-10 review: PASS

The round-9 P2 (quadratic per-match quote rescan) is remediated with a
single-pass monotonic quote-state scan; a near-limit regression (~62k
chars, 5000 quoted definition shapes) asserts the allow decision, the
absence of a HOME binding from quoted data, and a deterministic <2s scan
bound. The round-9 P3 process findings are closed: the candidate runner
restores the comparison bytes after every rebuild, and this document now
carries the round-4-9 history below.

Round-10 independent review returned **PASS** (guard and reviewed HOME
battery hashes verified; manifest 12/12; baseline comparisons present and
matching). The reviewed source is unchanged. Two optional P3 follow-ups
were applied: the near-limit regression now asserts that quoted data did
not create a HOME binding, and this document records the current evidence.
The reviewed HOME battery was `1485a00366efc20abda33ae124fc1918f009855b76a4abb013329ce38982f120`;
the tightened battery is `72ed268e84d6550668312301ea027d97b6b0b308373c6e78b29e54f56d3b764b`.
The original round-10 gates recorded 125 baseline failures / 248 passes,
520 focused passes, nine mutants killed, and 1956 full-suite passes with a
byte-identical production decision sink. Those receipts cover the reviewed
battery, not the later assertion edit. For the owner cutover and rollback,
see [the next-restart handoff](guard-next-hermes-restart.md).

**Final isolated release gate (AC-005): PASS.** The full kit suite ran
against the tightened HOME battery and final syntax-category tests:
1,956 passed, 70 subtests passed, 4 warnings, zero failures, pytest exit 0
(306.81s). All 90 recorded source and test inputs matched commit
`56da9d58bd628335055103bcd621f56f1b9a2e61` and remained unchanged. The
parent process read the production decision sink strictly before launch
and after exit; the bytes matched. Raw test output SHA-256:
`ca09096d0425886d0f2d234745ff853fb648be7f467ebdfeda9f3118d45d55c6`.
The staged plugin also passed `hermes plugins validate` in an isolated
profile; neither check establishes live activation (AC-006).

Two earlier release-gate attempts do not count as acceptance evidence. The first
hit Darwin's Unix socket path limit in controller fixtures and had a
production-sink delta of unverified origin (1,924 passed, 32 failed).
The second used a shorter path but inherited an umask of `077`, masking
permissions that three controller fixtures deliberately set (1,953
passed, 3 failed; sink byte-identical). The retry keeps the enclosing
scratch directory private, uses a short temporary path, and runs pytest
with the normal umask (`022`). Neither failure changed guard or test bytes.

The source-only candidate line has advanced through review rounds 4-9 since
the round-3 record below. Each round remediated the prior P2 red-first,
re-ran the parent gate battery with strict pre/post-launch production-sink
snapshotting in a parent-gated runner, and re-froze the candidate:

- Round 4 (guard `db9f45fe…`, tests `121a39d0…`): command-position-only
  builtin-shadow classification (round-3 P2 remediated). Gates: baseline
  red 110f/231p; focused 488; mutants a/b/d killed (8f/45f/3f); full suite
  1924 passed, sink byte-identical full window. Round-4 review
  REQUEST_CHANGES with one **pre-existing** P2: unquoted `HOME=` data
  (`echo export HOME=/fake`) still suppressed safe inherited HOME.
- Round 5 (guard `390cbd68…` → `280c6283…` → `c698f0ff…`): segments are
  classified before binding eligibility; proven-data segments prune only
  plain mentions; escaped/substitution-interior mentions stay ambiguous;
  only scan-collected positions can be re-admitted as bindings. Round-5
  review found a NEW P2: `_home_binding_shadow` returns False both for
  proven-data heads and no-head segments. A shell function could override a
  data verb and invalidate the data-only classification.
- Round 6 (guard `280c6283…`): any function-definition shape anywhere sets
  fail-closed refusal. Round-6 review found definitions after
  then/else/elif/do were missed by the position-bound regex.
- Round 7 (guard `c698f0ff…`): control-flow positions added. Round-7
  review found grouping-brace/case-body definitions missed and quoted
  definition SHAPES falsely refused.
- Round 8 (guard `6308a16c…`): position-free quote-aware scan. Round-8
  review found an escaped quote outside a quoted region misread as a quote
  opener (`echo \"; echo() {...}` must refuse).
- Round 9 (guard `57fc14be…`, tests `4e7039f7…`): escaped characters
  outside quotes are skipped as literals. Round-9 review found the
  per-match quote rescan quadratic on a near-limit input (P2, in flight:
  single-pass monotonic quote state implemented, near-limit regression
  added with a deterministic work bound) and a P3 process finding
  (comparison files must survive candidate rebuilds; runner patched).

The pre-performance-fix candidate (guard `57fc14be…`, tests
`4e7039f7…`) recorded 125 baseline failures / 247 passes, 519 focused
passes, nine mutants killed, and 1955 full-suite passes (310s). The
production sink was byte-identical across the strict pre-launch/post-exit
window. These are historical receipts, not the release candidate's gates.

## Round-3 disposition (historical, superseded by the rounds above)

The source-only `f2acfe552f…` candidate passed the fresh complete suite:
**1759 passed, 4 warnings, 70 subtests passed, 0 failures, pytest exit 0**
in 319.54s. The +71 tests reconcile with the preceding 1688-pass receipt
through the HOME battery's increase from 105 to 176 cases. All 13 frozen
review-manifest inputs still match their original hashes and the worktree.
Full output SHA-256:
`a4b9bb665b84f26461c192a8c5037660369651c035f5d86c96002ca0fc5c1d5b`.

Isolation correction: disposable XDG state and resolved scratch TMPDIR were
set for the suite, and both captured production snapshots have 1503 rows,
SHA-256 `3f5225d59b63e51adc1f132c5a828ed38894b58b73823bd2dc52e13ce184ade5`.
However, the early snapshot was taken **after pytest launch**, so equality
proves only the captured partial window. The chat's full-window isolation
claim was overstated. A final-candidate run must capture production bytes
before launching pytest and after it exits; this receipt alone does not
clear the full-window AC-005 gate. The parent receipt explicitly preserves
this limitation (`ir-r3-parent-gates-receipt.json`, local scratch).

Independent static review returned **REQUEST_CHANGES** (Codex 0.171.0,
banner-confirmed gpt-6-luna/medium; exact manifest and baseline hashes
verified). The round-2 quoted-data finding is resolved, but a new **P2**
remains: the builtin-shadow pass tests every decoded token rather than
command position. Thus `echo export "HOME=/fake"; $HOME/bin/python3 -c
"print(1)"` falsely refuses with safe inherited HOME; `export` is an echo
argument, not an environment-changing command. Add inherited-HOME and
valid same-payload-HOME allow/full-analysis tests, while retaining actual
`export "HOME=..."` refusal controls. Red-first remediation is delegated;
its implementation, complete-suite isolation and independent review remain
pending. Review output SHA-256:
`e7cf8797605f7c3d4cf6ea5fa06fc5986a903cfd5a014cc42879ad37ae69e50e`.

Supplemental parent probes returned 11/12 against their original
expectations. The one mismatch was an invalid expectation, not a guard
failure: `echo ho\me="HOME=/fake"` contains no HOME binding, so inherited
HOME remains eligible. The raw failing probe log is preserved; no corrected
12/12 execution is claimed. These supplemental probes are not a substitute
for the native-operation-fenced regression battery.

No new behavioral commit, push, installed-source change, activation or
restart has occurred. All receipts below retain their own candidate scope;
none qualifies the pending remediation.

## Round-2 candidate and correction record (historical)

The round-3 section below identifies the current candidate. This section preserves
round-2 observations and supersedes earlier round-1 claims only; its hashes and
results do not qualify the later round-3 source.

- Final source candidate guard SHA-256:
  `e603515c1648abb5acc0c497f5ae41a145e76c62b46c4ff9c1e444bc3066e4af`.
- HOME battery (105 cases) SHA-256:
  `826260b8ce4c480a4b71ac745367dd3a004c2cb95aae89dac046b518980fe924`.
- Syntax battery (8 cases) SHA-256:
  `3f9c1e6177c8ebb00e1a3c3c3cb7a8d77c5b64e027941e08d1e7e2148112b41b`.
- Synthetic manifest SHA-256:
  `242d132ab3fccdb381de1f4b6cbb6339ae537e51273f6f654495750c96e07788`.

Round-1 independent review returned REQUEST_CHANGES. The P2 finding was accepted:
restricting assignments to the leading assignment-only prefix narrowed the
existing bounded model. The remediation now admits unique literal standalone
assignments after earlier ordinary commands, before any use, with plain
semicolon/newline separation. Conditional, inline, pipeline-body, duplicate,
unresolved, quoted-lookalike, future, and grouping/function-scope assignments
remain unprovable and refuse. Resolved commands still receive full protection
analysis. The earlier leading-prefix-only prose below describes the rejected
candidate, not the current contract or implementation.

Both P3 findings were also accepted: the manifest now records the actual before
HOME value, and the after verdict asserts LGV-001; a malformed literal exec
operand test observes the retained shell fallback and pins SHAPE_ENVELOPE /
LGV-020. Parent verification against the original pre-IR-008 source passed this
exec-operand control (1 passed, exit 0), so it is a retained-behavior control,
not a newly corrected parse site.

Parent ran the exact frozen final batteries using CPython 3.14.10, pytest 9.2.0,
explicit disposable XDG_STATE_HOME, and resolved scratch TMPDIR. The runner
captures subprocess return status without a pipe and hashes inputs before and
after execution:

- Pre-IR-007 guard `9427cde6…`, final HOME tests: **50 failed, 55 passed,
  exit 1**. Output SHA-256
  `acec20007888ab61111e80020de1ee30094e5422b5dc3c7a38cd7e8de410f24b`.
- Pre-P2 guard `d40b2f93…`, final HOME tests: **7 failed, 98 passed,
  exit 1**. Output SHA-256
  `e3ed40fd03f27d94806117dc0e40e25421ffe5e275af7fca390a92f2f3f39606`.
- Current guard, all six focused modules (the five listed below plus
  test_guard_syntax_category.py): **233 passed, exit 0**. Output SHA-256
  `627747edd833594feba0decf7c0e799c85201157adf22bd7a71c5aa7f737206e`.
- Pre-IR-008 exec-operand control output SHA-256:
  `5b19689a97d618d5cdfce632de67285a8efa3c57100a80350a5bb7ca9e05ae34`.

All four parent runs reported unchanged input hashes and a byte-identical
production sink before and after (1500 rows, SHA-256
`8d436c8dab19661d5a446d7223dfdc7169528cc1c9eb5b6d3413fad52550f248`).
The child's first P2 setup attempt (25 errors due to missing adapter
__init__.py in its scratch copy) is invalid red evidence; its corrected targeted
red was 7 failed / 18 passed / 80 deselected. Parent's full 105-case red above
replaces that subset for final comparison.

Correction to the older full-suite isolation conclusion below: a row count alone
cannot rule out one synthetic append, and timestamp matching alone does not
identify every writer. The 23:43:38Z row is corroborated by a serving-gateway log
and another session's terminal invocation, supporting concurrent live traffic;
the receipt still says production_unchanged=false. The earlier 23:26:48Z row has
weaker timestamp-only corroboration and is not conclusively attributed here.
Do not use the old zero-synthetic-append assertion as final AC-005 proof. A fresh
full-suite receipt on the current candidate and independent exact-input review
remain required. No production activation or release approval is claimed.

Local commit 977e50f preceded completion of independent review; it is retained
as a premature draft, not an accepted release candidate. Remediation must pass
independent review before another behavioral commit.

## Round-2 independent review (2026-11-22, current candidate)

Round-2 read-only review (result transcript SHA-256
`2499864973850ca89a56e812cfd20740985a5ece24aef2570be5e9dae3a898fa`, final
verdict REQUEST_CHANGES) re-adjudicated and **resolved** the round-1 P2:
the rewritten collector admits later standalone unconditional bindings with
no laundering path found, and IR-008 plus both round-1 P3 fixes are confirmed.

Two findings remain open:

1. **P2 (pre-existing, not a new regression): quoted `HOME=` lookalikes
   suppress safe inherited HOME.** The `home_mentions` scan matches `HOME=`
   inside quoted echo data, so `echo "HOME=/fake"; $HOME/bin/python3 -c
   "print(1)"` with safe inherited `HOME=/fixture/home` incorrectly refuses
   instead of using the inherited value and analyzing the code. Remediation
   (owner-approved offline fixture context) is in flight: quote-aware
   assignment-word recognition, red-first, with safe-inherited controls,
   true-assignment-plus-lookalike controls, and retained negative controls
   (actual quoted-unsafe values must still refuse; inline/duplicate/empty/
   conditional/future/grouping bindings stay conservative).
2. **P3: this evidence file was stale for round 2** — the reviewer read a
   snapshot recording the earlier `d40b2f93…` candidate and pre-P2 receipts.
   The reviewer also correctly noted `review-input-hashes-2.json` was absent
   from its review directory (the parent wrote it to scratch, not the frozen
   candidate); the reviewer independently calculated all actual file hashes
   and they match the parent's records, so no input-integrity gap results,
   but the next review round must ship the manifest inside the frozen
   candidate.

Full-suite receipt on the current candidate (`e603515c…`): **1688 passed,
4 warnings, 70 subtests, 0 failures, exit 0** (301.57s), run in the frozen
candidate copy with disposable state. Input hashes unchanged across the run;
production sink byte-identical before and after (1500 rows, SHA-256
`8d436c8dab19661d5a446d7223dfdc7169528cc1c9eb5b6d3413fad52550f248`, zero
appended bytes). Output SHA-256:
`26287a051d91c385d44eaf9d1258a2cdf4a9a7887384b44bedc7aa85493b81fa`.
The count reconciles with the earlier 1662-pass run: +25 HOME-battery cases
and +1 exec-operand control. That receipt covered the pre-lookalike-fix
candidate only; the quoted-lookalike fix below supersedes it as the current
candidate and requires its own full-suite receipt.

## Round-3 quoted-lookalike remediation (current candidate)

The round-2 P2 fix landed source-only in the worktree. Current candidate
guard SHA-256:
`f2acfe552f57421d612aa9c35c888327a270370a431f2264ece2464b7b38629b`;
HOME battery (176 cases) SHA-256:
`5516b4e151c34c08456923e16d01408a67fe208eeb0b773117f1bc8cf59770ef`.
The worktree byte-matches the green freeze.

Implementation: HOME-assignment mention counting is now quote-aware —
mentions inside quoted data are not bindings; substitution interiors are
conservatively treated as potential mentions; escaped forms stay ambiguous.
A bounded second pass recognizes `export/readonly/declare/typeset/local/env`
operands that carry a quoted `HOME=` token (ambiguous shadow → refuse, never
a trusted value). Parent's independent offline probes confirmed the round-2
case (quoted lookalike + safe inherited HOME → allow and analyze; real
assignment + unrelated quoted data → allow; quoted-unsafe real value →
refuse), and the previously-wrong safe-inherited expectation in the old
`test_later_ambiguous_home_binding_never_launders` parameterization was
split and corrected.

Red-first receipts (child-frozen, parent-verified against worktree bytes):

- Baseline red (pre-lookalike-fix guard `e603515c…`, final 176-case HOME
  battery): **54 failed, 122 passed, exit 1**. Receipt
  `ir-round3-complete-baseline-red/red-receipt.json`; output SHA-256
  `af292a2ceff278bceb3e81792056c9e807ed38059af8fb940b1cd431c0f560d7`.
- Intermediate regression honestly captured red-first: an initial
  quote-aware rewrite failed open for builtin shadows
  (`export HOME=…`-class, 10 failed, exit 1, receipt
  `ir-round3-quoted-builtin-red/red-receipt.json`, guard
  `c068794e0528478e6594571806edbf9ff442424a02be30591d5093b11792e826`)
  before the builtin-shadow guard was added.
- Focused green on the final candidate: **323 passed, exit 0** across the
  six focused modules plus `test_guard_substitution_nesting.py` (tracked,
  unmodified; included because the new quote-aware scanner shares
  `_command_substitutions` spans with it). Output SHA-256
  `2b092557aa4ab229c552bda586263551150a1d706c67e5650a66876dec987932`.

Parent-owned gates still pending on this candidate: fresh frozen red
against the round-2 candidate (the child's baseline red used the final tests
against `e603515c…` and is the round-2 red), a fresh full-suite receipt
with production sink isolation, and independent round-3 review by exact
hashes with the hash manifest inside the frozen candidate. No behavioral
commit, push, activation, or release approval is claimed; local commit
977e50f remains a premature draft.

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

### Frozen final-battery red (2026-11-22)

The 80-case battery (`tests/test_guard_home_resolution.py` SHA-256
`5074a52c09bcd8f1443b1438e69398981eff2754bdb0c6b1a2f9f9de3aa0931d`) was
rerun against a frozen `git archive` extraction of the pre-IR-007 baseline
(`guard.py` SHA-256 `9427cde6b95b8feaf4b04459dbeed6566499d80fbe83e968f8d210466817de4e`),
with `XDG_STATE_HOME`/`TMPDIR` redirected to disposable scratch:

**exit 1; 43 failed, 37 passed.** Receipt: production decision sink
byte-identical before and after (1499 rows, SHA-256
`5000eac7219312a23cffa2c89152445c5664eec30812d22d15d5d7d17f272806`),
raw output SHA-256 `ccb11e08a5796cb47fa4500ed9f0022d1dfc12291ea20772fef6d4635847156d`.
This supersedes the unfrozen historical 23/23 count as the red gate. The
battery will be re-frozen once the in-flight assignment-visibility (P2)
remediation lands its own reds.

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

## Combined full-suite run (frozen candidate, pre-P2 remediation)

The IR-008 + IR-007 candidate was frozen to a scratch copy (`guard.py`
`d40b2f93…`, HOME battery `5074a52c…`) and the complete suite was run with
`XDG_STATE_HOME` and `TMPDIR` redirected to disposable scratch:

**exit 0; 1662 passed, 4 warnings, 70 subtests; 0 failures** (299.27s).
Raw output SHA-256:
`41c386e110496af3b35e5f9ec7d5878721a89d19e0236e4364306a88d4646101`.

Production sink across the run window: 1499 → 1500 rows. The single new
row (`BOUND_SIZE`/`LGV-010`, tool=terminal, 23:43:38Z) is attributed to
live concurrent traffic, not a synthetic append: the serving gateway log
records a live guard decision at the same timestamp, on a terminal call of
another concurrent session that completed 25 ms later with a block-shaped
175-char result. A suite isolation failure would append hundreds of rows
(every refused case writes one); exactly one row, timestamp-matched to a
logged live decision from another session, is live traffic. Zero
synthetic appends reached the production sink.

The same attribution method resolves the earlier 1498 → 1499 question:
that `VETO`/`LGV-001` row (23:26:48Z) is timestamp-matched to a live
gateway guard decision in the same log. Both rows are live-session
vetoes, not test writes.

This run predates the assignment-visibility (P2) remediation now in
flight, and the round-1 review's two P3 test/manifest fixes (syntax
battery now 8 tests; manifest `HOME` pinned to the actual before value,
after-case asserts `LGV-001`). The final combined candidate therefore
requires a fresh frozen full-suite run, fresh focused batteries, and
independent review by exact hashes before any release claim.

Full suite, exact combined-candidate independent review and owner-led live
activation remain separate gates. The executed fixtures redirected state and
fenced native process operations; the full-suite window above additionally
compared the production sink live and attributed its single new row to
concurrent traffic rather than suite execution.
