# Reliable local CFD search implementation plan

**Goal:** Implement the approved findings in analysis_20260908/代码与方法复查.md while keeping full geometry, seven active coordinates, real-CFD labels and existing data.

- [x] Add failure-first regression tests: failed Solve .res cannot be accepted; verified completed Solve can resume Post; pending AL results and diagnostics survive interruptions; failure CLI exits nonzero; locks reject other writers; blocked expansion does not block singles; predictor exceptions do not block predefined trials; exact common-sample EHVI preserves equal candidates/order; configurable local steps and search regions stay feasible.
- [x] Add shared runtime primitives: atomic JSON, file identity, process-level output lock. CFX state binds inputs and stage completion, and refuses unverified legacy residue.
- [x] Add geometry completion receipts and AL pending queue. Persist inputs/predictions before evaluation; reconcile recorded outcomes and diagnostics before new candidate selection. Keep explicit pending IDs when allocating boundary IDs.
- [x] Introduce version-2 boundary plans with configured target/relative levels, min/max steps, stage validation and explicit extension control. Return completed/budget_exhausted/failed stage status. Preserve failures, require explicit new experiment for retry.
- [x] Replace spatial Monte Carlo HV integration with exact 2D per-sample gains and shared quasi-Monte Carlo normal samples. Persist a fixed reference and report base/refined estimate differences.
- [x] Add bounded local regions around same-slice true Pareto representatives, retain a configured global candidate fraction, and adjust radius using idempotently recorded true HV progress.
- [x] Verify with offline tests, syntax, synthetic candidate dry-run, CFX input generation check, real CSV copies, hashes and diff checks. Package updated files and document legacy recovery and Windows verification limits.

No CFD is authorized to run on this macOS host. Existing user changes and original CSV files remain untouched. New heuristic step sizes/radii are initial configurable values, not validated engineering optima.

Validated: 38 tests passed; syntax/diff checks; synthetic offline candidate and CFX input checks; 90-row real-data-copy candidate selection; independent read-only review findings corrected. Windows software and real CFD not executed.
