# Native runtime artifact binding — reviewed source amendment

The source implementation and isolated deployment-layout verification implement
this CT-006 amendment. Actual library replacement, managed installation, platform
qualification and restart are separate owner/operator gates; no fixture result
or receipt grants those permissions. The installed native refusal remains until
a reviewed deployment supplies matching expectations and the verified artifact.

## Implementation boundary

`native_receipt_fact()` validates strict pin/receipt metadata without locating or
importing LiteLLM. `prove_runtime()` also verifies source, native bytes and ABI
before its final package-path import. Only the plain arm64 subtype with exactly
one valid macOS `LC_BUILD_VERSION` is supported; iOS/arm64e or inconclusive
observations refuse. See the active native runtime, controller, CLI and deployment
integration tests under `tests/`.

The installer ships `core/restart_control/runtime_identity.py` as the required
flat `runtime_identity.py` next to the installed checker and pin. Installed
checker invocation always selects that flat layout; an unexpected adjacent
`restart_control` entry refuses rather than selecting unmanaged code. Repository
checker invocation requires explicit `--repository`. Bytecode writes are disabled
before helper import. The checker compiles exact adjacent helper source bytes,
never its cached bytecode; disabling writes alone does not disable cache reads.
Symlinked or hard-linked helpers refuse in startup,
installer apply/verify and drift checks; incidental apply never repairs them.

Installer and every wrapper startup (normal or `--preflight`) run full proof
before an import-based sentinel. Existing installed native metadata and staged
pin/receipt metadata must be coherent before any apply-target writes. Native
receipt bytes or explicit absence are managed, verified and drift-checked using
the shared strict parser; stale evidence is never silently deleted to select
legacy mode. Helper and native pairing checks are non-exemptible. Drift reuses
the controller's closed input registry and checks the flat helper against its
actual nested source path, including cross-repository comparison.

Example source and installed-layout diagnostics (no listener/service control):

```sh
"$PINNED_PYTHON" -B core/check_pin.py --repository "$DEPLOY_DIR/pin.lock"
"$PINNED_PYTHON" -B "$CONFIG_DIR/check_pin.py" "$CONFIG_DIR/pin.lock"
"$PINNED_PYTHON" -B core/restart_control/runtime_identity.py --receipt-state "$DEPLOY_DIR/pin.lock"
```

Old installed layouts missing the newly required helper fail closed. Unexpected
nested layouts or incoherent old receipts require a reviewed operator transition,
not automatic migration or evidence deletion. Successful source verification is
not proof of native import compatibility in the serving process, live launchd
qualification, a previous-version rollback or restart readiness.

## Narrow acceptance

- Preserve deployed `base_version`, `patch_tip`, interpreter, Python source/blob,
  distribution mapping and active-bytecode checks. Do not bless a different HEAD.
- Permit only the fixed optional artifact
  `litellm/rust_bridge/_native.abi3.so`, on matching CPython/macOS arm64 ABI.
  Unknown, duplicate, malformed, symlinked, unpinned or mutated native code fails.
- A strict optional `native_artifact` table in the existing pin binds the fixed
  path, SHA-256, verified Rust source-tree Git object, and SHA-256 of a fixed
  adjacent `native-build.json` receipt. No RPC-supplied paths or hashes.
- The closed receipt binds source HEAD/tree, Cargo.lock, fixed build recipe,
  compiler/Cargo identities, target/features, produced artifact and build-log
  digest. It records actual executed successful build verification, not an
  observed library hash or self-consistent editable wheel metadata.
- Verify actual Rust source files against their pinned Git blobs, not just the
  tree identifier; exclude only generated Cargo target outputs from source walk.
  Undeclared source/build inputs or source drift invalidate acceptance.
- Native receipt presence/absence and bytes join the closed deployment/installed
  input digest. Installer apply/verify and drift comparison must ship/check it
  consistently. A missing receipt can never silently degrade to no-native mode.
- Hash the native artifact and receipt into the runtime digest, shared by primary,
  candidate, LKG, approval and rollback. Recheck before disruption. There is no
  previous-runtime rollback claim while mutable shared runtime files are used.

## Build evidence and change procedure

Use standard Cargo with the pinned Git source archive, locked dependencies, an
explicit feature/profile/target recipe, a fresh target directory, a clean scoped
build environment and bytecode writes disabled. Start offline; report missing
inputs rather than crediting a partial or failed build. Bound jobs, time and disk
usage; preserve diagnostics and only stop the retained owned build process group.
No proxy process, service label or credential source is a build input.

Verify source-tree and lock hashes before/after compilation, output architecture
and ABI export, receipt schema and output SHA. Preserve the old native bytes and
metadata before any replacement. A live file replacement is separate from build
success and must not be performed before the amended verifier is reviewed and
negative tests pass. Retain the old artifact for restoration, never delete it to
force verification success. Do not import the unverified native extension.

The trusted local build toolchain and dependency resolution are part of the
same-user operating trust boundary, not a new OS isolation or complete supply-
chain attestation claim. Record tool/config/dependency provenance and explicitly
surface any input whose verification cannot be established. A locally matching
old Cargo output alone is insufficient.

## Versioned runtime preparation

Do not qualify a new native layout by replacing libraries underneath a serving
process. That process may lazily import the replacement before the filesystem
transition finishes; restoring files cannot unload an extension already mapped.
PID/birth and HTTP health parity are not evidence of unchanged process memory.

Prepare a separate versioned source tree and virtual environment instead:

- Use independent Git objects at the unchanged pinned HEAD; do not depend on a
  shared object store that the old checkout can prune.
- Preserve both old native variants outside the **new** import roots and leave
  the serving tree untouched. Only the reviewed verified ABI3 file enters the
  new package. Never attest a legacy artifact from its observed hash.
- Reuse the exact resolved CPython binary hash, but bind the new executable path,
  prefix, package root and editable distribution metadata explicitly. Regenerate
  the new installation's RECORD, including its path configuration files. A new
  prefix or metadata mapping produces a new runtime digest; old approvals do not
  transfer to it.
- Disable site initialization when preparing the environment from an old Python;
  inspect path files as data before using them. A literal dependency-site path
  must not execute that site's old path-file hooks. Record any shared third-party
  dependencies: a read-only source/native tree is not a fully immutable environment.
- Seal the new source/environment without following links into original files.
  Run the unchanged full proof before native import, verify the exact loaded
  native path/hash, and exercise offline native behavior. Re-run the proof after
  the smoke check. Snapshot the original source, caches and native file identity
  before/after preparation; do not credit staged-tree proof as that comparison.
- Exercise real installer apply/verify and flat-layout proof in isolated targets.
  Those results qualify preparation, not the live service or its credentials.

Managed configuration changes, candidate traffic and platform/controller
qualification remain separate gates. Never attribute the staged runtime digest
to a still-serving process using the old environment. Activation requires its
own exact-runtime authorization and verified handoff; staging grants no restart
permission. Production-impacting assertion-based operator scripts must refuse
optimized Python before any work, or use unconditional checks instead.

## Required verification

Positive fixture and actual verified-build proof; missing/extra/wrong-type pin or
receipt fields; receipt mutation; wrong HEAD/tree/lock/tool/recipe; binary mutation,
wrong architecture, missing artifact, symlink and unknown extra native code;
Rust-source mutation/unregistered input; legacy deployments without native code;
managed installed-layout apply/verify/drift including receipt absence/presence;
mutation-tested native acceptance branches; primary listener preserved. Real
launchd-context qualification and exact-plan restart approval remain operator gates.

## Resolved design choices before implementation

1. **Trusted build issuer.** The operator-controlled fixed recipe consumes a fresh
   archive of the already pinned Git HEAD and validates every archived Git blob.
   It uses a new target directory, no incremental artifacts, locked offline Cargo,
   default crate features plus `extension-module`, `dev` profile with debug info
   disabled, explicit `aarch64-apple-darwin` target and the pinned PyO3 interpreter.
   It verifies unchanged source bytes after exit zero, Mach-O architecture and the
   `PyInit__native` export, copies only that produced file, and hashes the retained
   private build log. A separate recount validates the successful raw build record
   and artifact before issuing the normalized receipt. Build log/script/raw record
   remain durable private audit evidence; the service does not accept their paths
   or execute a builder. The pin/receipt are trusted operator-issued expectations,
   not a cryptographic defense against same-user forgery.
2. **Source closure.** Supported inputs are every pinned Git blob under
   `litellm-rust/` and `.cargo/`, including workspace/member manifests, lockfile,
   build scripts, included data and root Cargo config. The fixed recipe uses only
   workspace-local path dependencies; reject escapes or unregistered local inputs.
   Recompute the actual file-map digest and compare Git blob bytes before/after
   build and during runtime verification. Ignore only the workspace's root Cargo
   `target/` output directory, not arbitrary ignored sources. The build environment
   is explicit and scoped; global Cargo config is absent for this recipe. Record
   tool executable hashes/versions and lock/config digests. Cargo's locked
   dependency resolution and the local toolchain remain the stated trusted scope.
3. **Baseline.** Before changing any pin or runtime file, save and hash the actual
   deployed pin, verify its byte equality with the deployment input, and record its
   canonical parsed map. Only adding `native_artifact` is authorized; all previous
   fields must remain canonically identical. The receipt and native table bind
   `baseline_pin_digest`, recomputed from the pin without the new table. This
   deployment's existing full source HEAD is recorded in private evidence, not
   selected by a new native table. Receipt HEAD must equal that verified full HEAD
   and match the unchanged deployed tip. All pin gates use strict TOML, rejecting
   duplicate keys; there is no second permissive/regex native interpretation.
4. **Exact states and paths.** The only managed receipt name is
   `native-build.json`, adjacent to each deployment/installed `pin.lock`.
   Valid states are (a) no native table, no receipt and no package native artifact,
   or (b) one native table, receipt and the fixed artifact, all matching. Every
   mixed state refuses. Installer staging, verify, drift and controller freeze
   include bytes or explicit absence. A downgrade with a stale receipt blocks and
   requires an explicit reviewed operator transition; installer never silently
   deletes evidence to select legacy mode. LKG copies the receipt only when present
   and checks absence otherwise, using that same frozen-state registry.
5. **Pre-import order and shared proof.** Reuse the standalone runtime identity
   module as an installed flat helper, registered in the closed helper/input lists.
   `check_pin.py` delegates to it rather than importing LiteLLM first. Installer and
   wrapper run that proof before any import-based sentinel. The proof validates
   native policy, receipt, pinned source/lock/config, Mach-O/export and artifact
   bytes before its final LiteLLM path check. Candidate and pre-disruption freeze
   rerun the identical proof. No import serves as provenance evidence.
6. **Native inventory.** Only the exact relative `_native.abi3.so` path is eligible.
   It is an owned, regular single-link non-symlink Mach-O 64-bit arm64 shared
   library with a verified `PyInit__native` export. Reject any other importable
   native extension suffix, `.so`, `.pyd` or `.dylib` under verified package roots,
   plus unknown native symlinks. A missing or malformed ABI observation refuses.

### Strict pin and normalized receipt schemas

The optional `native_artifact` table has exactly: integer `version = 1`, fixed
`path`, lowercase 64-hex `sha256`, 40-hex `source_tree`, lowercase 64-hex
`receipt_sha256`, and lowercase 64-hex `baseline_pin_digest`. No extra keys,
coercion, duplicate tables, extra artifacts or arbitrary file paths are accepted.

The normalized JSON receipt has exactly: `version`, `source_head`, `source_tree`,
`source_digest`, `source_files`, `cargo_lock_sha256`, `root_cargo_config_sha256`,
`baseline_pin_digest`, `artifact_path`, `artifact_sha256`, `recipe`, `profile`,
`features`, `default_features`, `target`, `cargo_sha256`, `rustc_sha256`,
`cargo_version`, `rustc_version`, `build_recipe_sha256` and `build_log_sha256`.
Hashes are lowercase hex of the stated algorithm; HEAD/tree are complete Git
SHA-1 values for this supported source repository. `version` is integer 1 and
`source_files` a positive integer, never booleans. `default_features` is true;
`features` is exactly `["extension-module"]`, `profile` is `dev`, target is
`aarch64-apple-darwin`, and recipe is `cargo-locked-offline-dev-abi3-arm64-v1`.
Version strings are bounded single-line Cargo/rustc version outputs. Artifact path
is fixed. Reject duplicate JSON keys, nonfinite numbers, unknown fields and
missing fields. The receipt's bytes must match the pin's expected digest before
any claims within it are used. Build origin is established at issuance by the
actual verified fixed build; runtime independently rechecks these pinned claims
and the artifact/source state, not the historical execution event or process memory.

### Encoding and ABI refinements

`source_tree` is exactly the Git tree object at `HEAD:litellm-rust`. The source
map covers every tracked blob under both `litellm-rust/` and `.cargo/`, mapping
unaltered UTF-8 repository-relative paths to `{"git_mode": "100644" or "100755",
"sha256": <file bytes hash>}`. Symlinks and non-blob entries in this supported
build closure refuse. `source_digest` is SHA-256 of that map encoded with
`json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=True,
allow_nan=False).encode('utf-8')`. Actual execute bits must match Git modes.
`baseline_pin_digest` uses the identical canonical encoding of the entire parsed
pre-amendment TOML map, not the original text. Save original text separately for
restoration. Compare that saved map/digest with the amended map after removing
only `native_artifact`; version/tip and every pre-existing value must be unchanged.

The trusted issuer must also verify the fresh target's resolved PyO3 feature
fingerprint includes `abi3`, `abi3-py310`, and `extension-module`, and both PyO3
and PyO3-FFI build-script outputs enable `Py_LIMITED_API` for the 3.10 minimum.
Preserve those raw files privately. The normalized receipt additionally requires
exact `abi_tag = "cp310-abi3"` and SHA-256 fields `pyo3_cfg_sha256`,
`pyo3_ffi_cfg_sha256`, `pyo3_features_sha256`. At runtime, require this verified-
build receipt/expected artifact binding, CPython >=3.10 on arm64 macOS, the native
Mach-O/export checks and no version-specific libpython dependency before import.
Mach-O architecture plus a renamed filename alone is never ABI3 evidence.

Inventory reads file magic as well as importable suffixes throughout verified
package roots, rejecting unregistered Mach-O thin/fat content even with `.bundle`,
unknown or absent extensions. Only the one pinned thin arm64 shared library is
eligible. Unknown symlinks still refuse; tracked package links are resolved and
verified as in the existing proof. Resource limits and inconclusive observations
fail closed rather than skipping the scan.

For version 1, dynamic load dependencies are limited to these OS-controlled
platform libraries: `/usr/lib/libSystem.B.dylib`, `/usr/lib/libiconv.2.dylib`,
`/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation`,
and `/System/Library/Frameworks/Security.framework/Versions/A/Security`.
Parse Mach-O load commands directly with bounded sizes/counts before import.
Reject every other library name (including generic or versioned libpython),
relative/search-path dependency, `LC_RPATH`, dyld-environment or custom-linker
command. No mutable non-system dependency is accepted in this version. Library
install-name metadata is not a dependency edge. OS shared-cache/system libraries
remain the existing trusted OS boundary, not a claim to hash all OS code.
