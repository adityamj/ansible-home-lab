# Simplification follow-up

- [x] 1. Use only the standard nftables service for firewall boot/reload; retire
      custom unit wiring in the firewall migration.
- [x] 2. Reduce firewall drift state to desired-file and applied-ruleset hashes;
      retain whole-ruleset ownership verification and validation before apply.
- [x] 3. Remove obsolete app templates, legacy manifest fixtures, and the old
      test path. All existing test suites removed per request; renderer test
      replacement deferred rather than porting tests from the old architecture.
- [ ] Add a fresh regression suite for the final compiler/renderer and executor
      contracts once the rewrite stabilizes.
- [x] 4. Remove pending deployment transactions. Keep the desired plan ephemeral
      and persist only resource ownership plus last-successful host metadata in
      one ledger. Caller controls refresh; no stored restart/reload acknowledgement.
      Pure merge/diff logic can move to Python if its complexity warrants it.
- [x] 5. Make account discovery explicit, remove duplicate bootstrap lookups,
      reconcile user managers without a preliminary status query, and batch ZFS
      mount observations. Keep live checks at activation/data-write boundaries.
      Platform reconciliation inspects and validates Podman's effective graphroot
      without relocating/chowning it; ambiguous missing subordinate mappings
      with explicit storage configuration require manual repair.
- [ ] 6. Document narrow supported migration inputs and fail on unknown layouts.

## Required host verification

- [ ] Disposable-host firewall migration, including interrupted reruns.
- [ ] Reboot and nftables reload persistence with only the standard service.
- [ ] Foreign-table drift correction and atomic invalid-policy rejection.
- [ ] Unchanged deploy idempotency; targeted deploy isolation.
- [ ] Container migration reboot safety and numeric-user volume recovery.

There is currently no automated regression suite. Local syntax checks do not
substitute for these host checks.
