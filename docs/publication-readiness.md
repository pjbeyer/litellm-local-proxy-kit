# Publication and release checks

This repository is public source under the MIT license. A public source revision is not a release qualification or evidence of a live deployment.

## Before publishing a branch, tag, or release

1. Before staging, scan the exact candidate for secret material, private paths, merge markers and empty artifacts. Review changes and every artifact that becomes reachable. Exclude resolved credentials and real credential references, workstation configuration, raw logs or responses, private tracker records, and agent prompts or session metadata. Put deployment-specific material in a separate private repository.
2. Review the public repository beyond the working tree: branches, tags, pull-request refs, notes, database refs, releases, and Actions artifacts. This repository intentionally publishes kit Beads records and Git attribution metadata. Treat both as public from creation; never mirror a private tracker or publish prompts, transcripts, or sensitive data in attribution notes. A cleanup commit does not erase earlier commits or data still available through other refs.
3. Run the complete offline suite and an independent review of the exact release candidate. Keep reports redacted. Treat findings in history or auxiliary refs as exposure to contain, not as issues fixed by adding patterns to `.gitignore`.
4. If releasing a new runtime, declare its interpreter, source pin, dependencies and native receipt where relevant. Run `preflight.py --router-test` with that runtime, and perform separately authorized auth, inference and required tool/MCP checks. Revalidate provider limits and data-handling terms. Document unresolved compatibility limits instead of implying support.

Use the [managed service guide](macos-managed-service.md) for deployment and the [compatibility evidence](compatibility.md) for qualified claims about specific test runs.
