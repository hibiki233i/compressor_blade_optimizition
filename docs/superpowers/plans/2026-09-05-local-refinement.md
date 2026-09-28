# Local CFD refinement implementation plan

> **For agentic workers:** Execute inline with test-first checkpoints; do not alter the external geometry/CFX chain.

**Goal:** Preserve 12-value geometry while searching seven coordinates, compare conditional full-input and same-slice models, and execute a frozen boundary design in resumable stages.

**Architecture:** Add `blade_shape_refinement.py` for subspace/data/diagnostic/plan functions. Integrate through the existing entry point, keeping old CSV columns and solver interfaces. No recovered Windows implementation exists in the available Git objects.

**Tech Stack:** Existing Python/numpy/pandas/scipy/sklearn; unittest for offline verification.

**Spec:** User-approved changes in the current conversation; `AGENTS.md`.

## Constraints

- Success labels and Pareto members remain real CFD outcomes only.
- Preserve unrelated modifications in PROJECT_CONVERSATION_SUMMARY_2026-06-15.md.
- Configuration remains the source of variable bounds and geometry constraints.
- No real CFD in this macOS session. Synthetic fixtures and external-call test doubles are explicitly labelled.
- Keep all 12 geometry coordinates in original order; keep the existing frozen vector, not a recomputed median.

## Tasks and interfaces

- [x] Tests: add `tests/test_refinement.py`. Use `unittest discover -s tests -v` to demonstrate missing features before implementation. Cases: fixed coordinates survive sampling; same-active/different-fixed history is not a duplicate; malformed configuration fails; boundary 5/6/1 stages share center; extension has identical-coordinate 4.5 control; CSV extra columns survive; role diagnostics exclude failures; exact HV; budget/resume skips completed CFD and retains reserved case IDs.
- [x] Subspace: `active_indices(config)`, `enforce_fixed(config, x)`, `slice_distance(config, x)`, `same_slice_mask(config, x)`, `training_partition(config, frame)`. Validate active/fixed partition and bounds. Add a factory wrapper using full12 primary and slice7 challenger with configurable minimum samples. Keep errors in metadata when challenger is unavailable.
- [x] Integration: apply active coordinates in LHS/random/mutation/distances; preserve legacy columns during append; keep explicit sample phases; continue AL iteration numbers on resume. Store role and both predictions before CFD, plus true HV/delta diagnostics afterwards.
- [x] Diagnostics: persist reference point derived from the initial successful DOE; aggregate prediction MAE, RMSE, coverage and interval width by role on same-slice, same-model rows. Keep a local gate distinct from predefined boundary experiments.
- [x] Boundary plan: `write_boundary_plan(config, center_run_id, path)` freezes a successful same-slice center, four controls, six pair combinations and one extension, with config/template fingerprints. `run_boundary(config, path, stage, max_new, resume)` executes only explicitly selected stages, checkpoints reserved case IDs before CFD, reconciles saved training rows after interruption, and stops on failures. No implicit retry of failed expensive cases.
- [x] Commands: `diagnose`, `write-boundary-plan`, `run-boundary`; add Python-only `--offline` to candidate dry-run for fixture validation without PowerShell. The Windows dry-run retains its existing behavior.
- [x] Documentation: describe config, compatibility, exact commands, model availability limits and validation boundaries. Run syntax checks, offline candidate generation, tests, actual CSV read-only analysis and `git diff --check`.

Verification completed with 18 offline tests, Python syntax checks, candidate dry-run using an explicitly synthetic fixture, real CSV copies (90 history / 12 same slice), and unchanged original CSV hashes. Windows geometry/CFX runtime remains untested.
