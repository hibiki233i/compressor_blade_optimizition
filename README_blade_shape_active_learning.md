# Blade Shape Active Learning Prototype

This folder contains a first-pass blade-shape optimization loop for the current
CFturbo, TurboGrid, and CFX chain.

The loop uses real CFD results as training labels. The surrogate is only used to
screen candidates before the next CFD calls. Final Pareto rows are always taken
from successful CFD evaluations.

## Files

- `blade_shape_active_learning.py` runs DOE, GP/Kriging surrogate fitting,
  NSGA-II-style candidate search, EGO/MOEGO acquisition, CFD execution, and
  Pareto export.
- `Run-BladeShapeGeometryMeshing.ps1` writes the 12 blade-shape variables into
  the CFturbo batch XML and runs CFturbo and TurboGrid.
- `blade_shape_cfx_runner.py` imports the generated `Impeller_Mesh.gtm` into the
  CFX template, solves, runs CFX-Post, and reads `CFX_Results.txt`.
- `blade_shape_config.json` stores runtime settings, constraints, and variable
  bounds. Its `paths.*` are left empty and filled from the untracked
  `blade_shape_local.ini` beside it (copy `blade_shape_local.ini.example`); a
  non-empty JSON path takes precedence.
  `cfx_bin_dir` / `turbogrid_exe` left empty in both are derived from the
  ANSYS `AWP_ROOT<version>` variable (`[ansys] version`, or the only installed
  version); `python blade_shape_local_config.py <config>` prints each path's source.

## Variables

The current `D:\blade optizamation\Templates\0908-2.cft` template has
`SplitterBlades=False`, so this first version optimizes the actual available
main-blade mean-line shape instead of inventing splitter variables.

The 12 variables are offsets from the template baseline:

- `hub_beta_0..4_deg_offset`
- `shroud_beta_0..4_deg_offset`
- `hub_theta_deg_offset`
- `shroud_theta_deg_offset`

The beta offsets map to the five hub and five shroud mean-line beta control
values. The theta offsets map to the two stacking angles. End-point beta values
are synchronized with `Beta1` and `Beta2` in the CFturbo batch file.

Initial beta bounds are intentionally narrower than the literature hard limit of
10 degrees because the current `0908-2.cft` hub outlet segment is already steep.
The asymmetric `hub_beta_3` / `hub_beta_4` bounds avoid worsening that outlet
gradient while still allowing changes that relax it.

## Run

Static check:

```powershell
python -m py_compile blade_shape_active_learning.py blade_shape_cfx_runner.py
```

Write one candidate and validate XML generation without starting CFturbo:

```powershell
python blade_shape_active_learning.py write-candidate --index 0 --dry-run
```

Check the CFX-Pre layer without starting the solver:

```powershell
python blade_shape_cfx_runner.py check-pre --working-dir "D:\blade optizamation\blade_al_runs\cases\dry_candidate_000"
```

Run a one-case real geometry and CFD smoke test:

```powershell
python blade_shape_active_learning.py run --initial-samples 1 --iterations 0 --max-new-cfd 1
```

Run a small active-learning cycle:

```powershell
python blade_shape_active_learning.py run --initial-samples 6 --iterations 2 --batch-size 2 --max-new-cfd 10 --resume
```

## Outputs

All outputs are under `blade_al_runs` by default:

- `training_data.csv`: variables, CFD metrics, sample phase, status, failure
  stage, and case path. `sample_phase` is `doe` for initial design-of-
  experiments rows and `active_learning` for EGO/MOEGO-selected rows. Active
  learning rows also carry `al_iteration`, `batch_index`, `selection_rank`, and
  `selection_source`.
- `pareto_front.csv`: engineering-tolerance non-dominated successful CFD rows
  for Efficiency and MassFlow. The default tolerances are `0.0003` and `0.006`,
  respectively. Pressure ratio and total pressure ratio are recorded for review
  but are not optimization objectives under the current fixed-static-pressure
  operating condition.
- `pareto_front_strict.csv`: strict mathematical Pareto rows without engineering
  tolerance, kept for audit and comparison.
- `iteration_summary.csv`: per-attempt status log.
- `active_learning_diagnostics.csv`: active-learning-only selection diagnostics,
  including acquisition score, approximate EHVI, distance to existing samples,
  surrogate predictions, prediction standard deviations, true CFD objectives,
  prediction errors, and Pareto row counts before/after each selected CFD case.
- `cases/case_XXXXXX`: candidate JSON, generated CFturbo/TurboGrid/CFX files,
  and command logs.

## Notes

- `Power` is retained in `training_data.csv` for engineering review, but it is
  not an objective and is not used as a penalty.
- The preferred surrogate is Gaussian Process/Kriging via
  `sklearn.gaussian_process.GaussianProcessRegressor` with a Matern 5/2 ARD
  kernel. Acquisition uses a Monte-Carlo approximation of expected hypervolume
  improvement, so this is an EGO/MOEGO-style loop. If `sklearn` is unavailable,
  the code falls back to the local RBF-ridge ensemble.
- A future 28-variable splitter-blade version needs a CFturbo baseline with
  splitter geometry enabled and visible in the XML.

## September 2026: reliable seven-coordinate local search

This version is built on the available checkout. The unpublished Windows
`7d-active-learning` worktree source was not recoverable; this does not reproduce
its ExtraTrees model-selection procedure. Python 3.10+ is required. Dependencies
remain NumPy/Pandas and optional SciPy/scikit-learn (GP falls back to RBF).

### Search coordinates, models and data

Keep all twelve entries of `variables` in their existing geometry order. The
seven `search.active_variables` are searched; the five `fixed_variables` retain
the values of the existing fixed slice, not a moving Pareto median.

- The primary GP uses all twelve historical inputs and finite successful CFD
  labels; its candidates vary only the seven active coordinates.
- The seven-input challenger uses only successful observations on the same fixed
  slice. It is available at `challenger_min_samples=12`; this is an availability
  threshold, not evidence of predictive accuracy.
- `slice_tolerance_norm` compares Euclidean distance in normalized inactive
  coordinates. Active-space duplicates/coverage use same-slice history, so old
  off-slice observations are not treated as equivalent seven-input labels.
- Both models' predictions are saved before CFD. Predictions never enter the
  training table as successful labels or become Pareto members.

### Stable acquisition and local candidates

EHVI uses exact two-dimensional rectangle areas for each predictive draw,
replacing random target-space integration. All candidates use the same scrambled
Sobol normal draws (ordinary common normal draws if SciPy is unavailable).
The configured defaults are 256 base draws and 1024 validation draws. All
candidates get the refined estimate, so ties and candidate order do not change
because only some candidates were resampled. `ehvi_base_estimate` and
`ehvi_sampling_change` report the numerical refinement difference; this is not a
CFD uncertainty interval. The deprecated `ehvi_hv_points` setting is unused.

Ordinary AL persists an objective reference in `hypervolume_reference.json`
using initial successful DOE minima minus 5% of the DOE spans (a 1e-6 span floor),
or the first available successes if DOE is absent. Acquisition and actual
progress share this reference. The pure acquisition helper uses the current
observed range if called directly without an explicit reference.

`refinement.local_search` controls up to three regions around same-slice true
Pareto representatives: highest efficiency, highest flow and a balanced point.
Centers are successful observations inside the current search bounds; out-of-range
extension observations may train the model but do not become invalid local boxes.
Defaults: radius 0.15 of each full coordinate span, minimum 0.05, maximum 0.30.
NSGA candidates stay in these clipped regions. The additional candidate pool
requests 70% local points and fills the rest globally; infeasible local samples
may be replaced by global ones. Every candidate remains subject to geometry
rules, fixed coordinates and duplicate checks.

After three consecutive AL outcomes without relative true HV gain above 1e-4,
the radius halves; after three improving outcomes it doubles, within its limits.
Each run ID is processed once, including recovery. This is a search heuristic,
not an engineering-significance or convergence test. With no same-slice successes,
search falls back to the active global region. The region state is written to
`local_search_state.json`; changed slice/settings reset it.

Batch roles remain `ehvi`, `uncertainty`, `diversity` and are configurable.
Uncertainty uses mean std/tolerance; diversity uses active distance to local
history and already selected batch points. This is not joint qEHVI. A batch of
three still selects before evaluating its members. `--batch-size 1` retrains
between points and uses the first configured role (default EHVI) each time.

### Prospective diagnostics

```powershell
python blade_shape_active_learning.py diagnose --config blade_shape_config.json
```

No CFD is run. Outputs:

- `training_slice_audit.csv`: finite successful history, inactive distance and
  same-slice membership.
- `role_diagnostics_history.csv`: historical primary error summaries by role.
- `role_diagnostics.csv`: current-slice/latest-primary-model primary/challenger
  MAE, RMSE, bias, +/-2sigma coverage and full interval width (4sigma), by role.
- `local_diagnostic_gate.json`: EHVI-role minimum count, MAE/tolerance and coverage
  checks. The configured recent window is 18 points and minimum count is 6.
  This is advisory for model-led decisions. It is not required for predefined
  boundary points and does not certify numerical CFD convergence.

The primary std initially has scale 1. After at least six earlier prospective
residuals of the same slice and model identifier, each objective uses
`max(1, quantile(abs(error)/raw_std, 0.95)/2)`. The scale uses earlier data only.
Small-sample coverage and a wide interval do not imply accurate mean prediction.
The challenger currently reports its own uncalibrated std. Compare model errors
on common new cases; differing availability counts are not a fair model contest.

Legacy diagnostics missing slice/model identifiers remain history; identifiers
are not invented to pass a new gate. The original `diagnostic_gate.json` is not
overwritten. No gate automatically starts a boundary experiment.

### Version-2 boundary plans: local levels and independent stages

Select a unique successful same-slice reference case. It may already be near a
boundary: the new local-step policy can move inward. The original five inactive
values remain fixed; the other three active variables outside the four-factor
experiment stay at the reference values for this experiment only.

Each boundary variable has `refinement.boundary_levels`:

- `step_deg`: requested movement toward the upper limit; if the remaining upward
  distance is below `min_step_deg`, move inward instead.
- Alternatively, `value`: an explicit trial level.
- `min_step_deg` / `max_step_deg`: allowed difference from the reference.

Initial defaults are beta step 0.5 degrees, allowed 0.1–1.0; theta step 0.25,
allowed 0.05–0.5. These are editable starting values, **not** CFD-validated optimal
steps. Global bounds and geometry constraints are unchanged. Old configurations
without boundary-level settings retain the high-endpoint policy, but a zero-step
point blocks its stage rather than running a duplicate.

```powershell
python blade_shape_active_learning.py write-boundary-plan --config blade_shape_config.json --center-run-id CENTER_RUN_ID --plan boundary_plan_v2.json
```

Replace `CENTER_RUN_ID` with a chosen real record. The file freezes the full
candidate coordinates and returns validation for each stage:

1. `singles`: common reference rerun plus four single-variable levels (5 points).
2. `pairs`: six two-variable combinations at those same levels (6 points).
3. `extension`: designated 5.0-degree hub_beta_4 experiment and an exact matching
   4.5-degree control. If an earlier reference/single point already provides that
   control, it is reused; otherwise an `extension_control` point is added. Thus
   extension costs 1 or 2 CFD attempts, and the entire plan has 12 or 13 points.

An added 4.5 control too far from the reference under the configured maximum step
blocks extension. It does not block valid singles. Every point still undergoes
Python geometry-rule checks. Invalid points/reasons stay in the plan, and a
blocked stage cannot execute. No limits are relaxed to make a stage pass.

The global hub_beta_4 upper bound remains 4.5. Only the explicit extension case
uses the 5.0 upper bound. Its other eleven coordinates are identical to its
control. These experiments give local contrasts, not a complete four-factor
quadratic response surface or a physical stability certificate.

Plans have a content hash and physical input signature (settings and available
source/template hashes). They are not overwritten or silently edited. Version-1
plans are deliberately rejected; retain their files and generate a new plan.
Changing a blocked extension's geometry/configuration requires a new reviewed
plan, not editing its signature or setting `valid=true` manually.

### Run a selected stage and resume

These commands launch real external software on the configured Windows machine:

```powershell
python blade_shape_active_learning.py run-boundary --config blade_shape_config.json --plan boundary_plan_v2.json --stage singles --max-new-cfd 5
python blade_shape_active_learning.py run-boundary --config blade_shape_config.json --plan boundary_plan_v2.json --stage singles --max-new-cfd 2 --resume

# After checking singles outcomes:
python blade_shape_active_learning.py run-boundary --config blade_shape_config.json --plan boundary_plan_v2.json --stage pairs --max-new-cfd 6 --resume

# After checking all prerequisite outcomes; inspect whether extension has 1 or 2 points:
python blade_shape_active_learning.py run-boundary --config blade_shape_config.json --plan boundary_plan_v2.json --stage extension --max-new-cfd 2 --resume
```

`stage_status` is `completed`, `budget_exhausted`, or `failed`. Failed CFD returns
nonzero command exit status. Budget exhaustion is a normal resumable stop.
Each invocation consumes at most its budget of evaluation attempts. Resuming a
case whose verified files have not reached CSV can use one attempt slot even
when cached Post recovery avoids a new solver launch. Recorded
completed points are reconciled without repeating CFD. Failed points stop the
stage and are not automatically retried. Plan validation errors also stop before
CFD. Completion of prerequisites is not proof of physical/numerical quality;
inspect results before explicitly starting the next stage.

`boundary_plan_v2.state.json` stores per-point status, predictions and progress.
The shared `case_reservations.json` persists IDs before separate checkpointing,
so an interruption cannot let another task reuse the reserved ID. Recovery can
reconstruct a missing point checkpoint from this registry. Keep both with the run
folder. `boundary_diagnostics.csv` records predictions and true outcomes. Empty
or unreadable optional AL diagnostics, or a fitting failure, are recorded as
prediction unavailable and do not block these predefined coordinates.

### Ordinary AL recovery and single-writer protection

```powershell
python blade_shape_active_learning.py run --config blade_shape_config.json --resume --iterations 2 --batch-size 3 --max-new-cfd 6
```

`pending_evaluations.json` registers DOE/AL selections, full coordinates, input
identity, sample metadata and predictions before evaluation. `--resume` first
processes pending entries with their reserved IDs. If CSV was appended before
interruption, predictions/progress/diagnostics are reconciled without another
CFD call. Queued batch points retain their original predictions. Remaining budget
can then select new points. `--iterations 0` resumes pending entries without
starting new AL iterations. With `--max-new-cfd 0`, already recorded pending rows
can be finalized but no new evaluation is started.

Case numbering includes the training CSV, existing directories, ordinary queue
and boundary reservations. Explicit sample phases and unknown CSV columns are
preserved. `sample_phase=boundary` records retain `experiment_id` and
`design_role`; successful real outcomes may train models and join Pareto.

OS-level `.optimizer.lock` locks protect the run folder and the CFX case. Other
processes, including the standalone `check-pre` command, cannot write to a locked
case. Locks release when their owning process exits; the lock file remains and
should not be deleted as a way to unlock an active calculation.

### What a recoverable CFD result means

- `geometry_state.json` binds the expected candidate geometry, full input
  configuration and mesh identity. Existing candidate parameters **and geometry**
  must match. A matching completed mesh is reused, not regenerated before CFX
  recovery. Missing/failed/incomplete receipts require inspection.
- `cfx_state.json` binds mesh/candidate/template identities and operating settings
  to recorded Pre/Solve/Post completion. Generated .def and CCL identities are
  checked when resuming between Pre and Solve.
- A `.res` file alone, or an unverified `CFX_Results.txt`, cannot become success.
  Only a recorded successful Solve with an unchanged result can proceed to Post.
  Completed verified Post outputs may be recovered. Failed/uncommitted Post output
  is preserved under an `unverified` filename before rerunning Post.
- Interrupted/failed Solve is not automatically reclassified or rerun. A process
  exit can occur before a completion receipt is durable; such artifacts need
  inspection even if a result file exists. A running solver child may still need
  operator attention after its parent is interrupted.

Existing 90 historical CSV rows are not rewritten or retrospectively invalidated.
Old orphan case directories without queue/provenance are reported before selecting
new CFD. Keep their files/logs for review. Do not manufacture success receipts to
import them; if the actual input/completion cannot be established, use a new case
instead of accepting the residue. These checks certify recorded execution and
input identity, **not** residual convergence, conservation, mesh uncertainty or
physical reliability.

### New fields and offline verification

AL diagnostic fields include roles/slice/model identity, history and slice train
counts, raw/calibrated std and calibration count, challenger predictions/status,
`ehvi_samples`, `ehvi_base_samples`, `ehvi_base_estimate`, `ehvi_sampling_change`,
`local_region_count`, `local_radius_norm`, `hv_before`, `hv_after`, `hv_gain`,
`delta_best_*` and `engineering_nondominated`. New fields are padded when appending,
and old extra fields are retained. Fixed-reference exact HV measures two-objective
front progress in CSV units, not efficiency percentage or numerical significance.

```powershell
python -m py_compile blade_shape_active_learning.py blade_shape_refinement.py blade_shape_cfx_runner.py blade_shape_runtime.py blade_shape_pending.py blade_shape_acquisition.py
python -m unittest discover -s tests -v
python blade_shape_active_learning.py write-candidate --config blade_shape_config.json --index 0 --dry-run
python blade_shape_cfx_runner.py check-pre --config blade_shape_config.json --working-dir CHECK_DIRECTORY
```

Use an independent check directory for `check-pre`. It generates input text and
checks required file paths; it does not run CFX-Pre. The Windows candidate dry-run
writes CFturbo XML/TurboGrid scripts but does not run those engineering programs.
`--offline` skips PowerShell and validates Python candidate rules only. The XML in
`tests/fixtures` is labelled synthetic and is not an engineering template.
macOS tests use temporary directories and external-call test doubles. A real
Windows single-case smoke test is still required before treating the external
pipeline as verified.

## Desktop GUI console (`blade_gui`)

`blade_gui/` is a PySide6 desktop front end over the command line described
above. It is deliberately non-invasive: it reads artefacts through this module's
own loaders and executes every action by spawning the original CLI in a child
process, so no optimizer, acquisition or CFD logic is duplicated or replaced.

- `blade_gui/project.py` - Qt-free data access (config, CSVs, `cases/`, summary).
- `blade_gui/commands.py` - pure builders for the exact CLI argv of each action.
- `blade_gui/runner.py` - `QProcess` wrapper streaming merged stdout/stderr.
- `blade_gui/charts.py` - QPainter charts (scatter/line/step/parity/bars) with
  hover tooltips, wheel zoom and click-to-drill-down.
- `blade_gui/pages/` - dashboard, analytics, config editor, run control, cases.

Install and run:

```powershell
python -m pip install -r requirements-gui.txt   # PySide6 only
python -m blade_gui                             # or double-click run_gui.bat
python -m blade_gui --config path\to\cfg.json --data-dir path\to\blade_al_runs
```

`--data-dir` overlays `paths.output_dir` read-only, which is how results copied
off the CFD host are inspected. Pages cover the Pareto front and convergence,
variable correlations and surrogate parity, validated editing of
`blade_shape_config.json` (atomic write plus timestamped backup), one-click
`run` / `diagnose` / `write-boundary-plan` / `run-boundary` / `write-candidate`
execution with live logs, and per-case file/log/candidate inspection.

The GUI is a viewer and launcher, not a source of truth: predictions are never
shown as CFD results, and the training table, Pareto files and receipts remain
the only records the optimizer reads.

```powershell
python -m unittest tests.test_gui -v   # runs offscreen; widget tests skip without PySide6
```

### Mandatory final RMS acceptance and one bounded continuation

`cfx_convergence` is the common CLI/GUI configuration source:

```json
"cfx_convergence": {
  "rms_target": 0.00001,
  "restart_iterations": 2000,
  "flow_analysis": "Flow Analysis 1"
}
```

Old configs without this section use these defaults. The RMS target must be positive
and no looser than 1e-5; the extra budget must be an integer between 1500 and 2000.
The flow name must match the actual CFX analysis in the template. The first solve
keeps the template iteration limit and explicitly applies the RMS target using a
separate `convergence.ccl` overlay.

After a zero-exit Solve, the runner requires one new nonempty `.res` and its matching,
new/updated same-stem `.out`. It reads the **final outer iteration** RMS column (not
Max Res or the minimum residual earlier in the run). U/V/W momentum, continuity and
energy must be present; every reported equation, including turbulence, must meet
the threshold. Missing/malformed/nonfinite output or a missing solver-finished
marker is rejected. Equations present in the preceding iteration may not disappear
from the final table. This reader targets this project's steady 3D compressor runs.

If the final RMS is too high, a continuation is allowed only when the output reports
an iteration-limit termination and the current-run counter has reached the declared
maximum iteration count. A generic process failure, user interruption, unknown stop,
missing limit declaration or truncated output never triggers an automatic restart.

The continuation reads the last `.res` using `-initial-file`, with the same `.def`
and boundary-condition overlay. It **resets the iteration counter**, rather than
using `-continue-from-file` to inherit the previous history. This bounds the new run
to exactly the configured maximum of 1500–2000 additional iterations, independent
of old accumulated counters. `restart_convergence.ccl` sets that limit and the RMS
target. The flow field is reused; previous solver monitor/iteration history is not
continued. Early convergence may stop the new run before its maximum.

Only a converged final result reaches CFX-Post. If the continuation still fails the
RMS criterion, the point is recorded as `status=failed`, `failure_stage=solve`; logs
and both result files remain for inspection, but the point cannot enter surrogate
training or Pareto exports. One design attempt may therefore run the solver twice;
`--max-new-cfd` counts design attempts, not solver launches. The normal optimizer
can proceed to other points; existing boundary/validation fail-stop rules remain.

`cfx_state.json` version 2 records policy, both solve attempts, paired `.out` identities,
final equation residuals and generated control-file identities. A resumed completed
result must still have valid residual evidence. Interrupted/failed attempts do not
get another automatic continuation. Old version-1 receipts are not silently promoted
to numerical acceptance; historical training CSV rows are unchanged. Use a fresh,
explicitly reviewed case if an old case needs recalculation. Newly frozen plans include
the convergence policy in their physical signature.

`check-pre` also emits the initial and restart convergence CCL files. This is an input
inspection, not a solver execution. RMS acceptance does not replace conservation,
mesh independence, operating-condition or engineering review.

References: [CFX output tables](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfx_solv/i1299644.html),
[initial-file versus continued history](https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/cfx_mod/mod_ic_continuinghistory.html).
