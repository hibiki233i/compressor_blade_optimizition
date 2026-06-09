# Blade Shape Active Learning Prototype

This folder contains a first-pass blade-shape optimization loop for the current
CFturbo, TurboGrid, and CFX chain.

The loop uses real CFD results as training labels. The surrogate is only used to
screen candidates before the next CFD calls. Final Pareto rows are always taken
from successful CFD evaluations.

## Files

- `blade_shape_active_learning.py` runs DOE, surrogate fitting, NSGA-II-style
  candidate search, acquisition, CFD execution, and Pareto export.
- `Run-BladeShapeGeometryMeshing.ps1` writes the 12 blade-shape variables into
  the CFturbo batch XML and runs CFturbo and TurboGrid.
- `blade_shape_cfx_runner.py` imports the generated `Impeller_Mesh.gtm` into the
  CFX template, solves, runs CFX-Post, and reads `CFX_Results.txt`.
- `blade_shape_config.json` stores paths, runtime settings, constraints, and
  variable bounds.

## Variables

The current `F:\optimazition\Templates\0908-2.cft` template has
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

## Run

Static check:

```powershell
python -m py_compile blade_shape_active_learning.py blade_shape_cfx_runner.py
```

Write one candidate and validate XML generation without starting CFturbo:

```powershell
python blade_shape_active_learning.py write-candidate --index 0 --dry-run
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

- `training_data.csv`: variables, CFD metrics, status, failure stage, case path.
- `pareto_front.csv`: non-dominated successful CFD rows for Efficiency,
  PressureRatio, and MassFlow.
- `iteration_summary.csv`: per-attempt status log.
- `cases/case_XXXXXX`: candidate JSON, generated CFturbo/TurboGrid/CFX files,
  and command logs.

## Notes

- `Power` is retained in `training_data.csv` for engineering review, but it is
  not an objective and is not used as a penalty.
- The first version uses a local RBF-ridge ensemble implemented with NumPy so it
  does not require `sklearn`, `torch`, or `pymoo`.
- A future 28-variable splitter-blade version needs a CFturbo baseline with
  splitter geometry enabled and visible in the XML.
