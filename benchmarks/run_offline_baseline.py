"""Run the real optimizer with synthetic external stages, then check its reference."""
from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import blade_shape_active_learning as blade
from benchmarks.compare_offline_baseline import compare_outputs
from tests.synthetic_pipeline import fake_geometry, synthetic_cfx

DEFAULT_CONFIG = ROOT / 'benchmarks/offline_baseline/config.json'


def run_baseline(output_dir: Path, config_path: Path = DEFAULT_CONFIG) -> Path:
    """Start a fresh, isolated synthetic run without reading local INI settings."""
    # SciPy is required here: the optimizer otherwise switches LHS/Sobol to RNG sampling.
    import scipy.stats  # noqa: F401

    output_dir = Path(output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f'Baseline requires a new or empty output directory: {output_dir}')
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    config['paths'] = {key: '' for key in config['paths']}
    config['paths'].update(output_dir=str(output_dir), cft_batch_template=str(
        ROOT / 'tests/fixtures/synthetic_meanline.cft-batch'))
    output_dir.mkdir(parents=True, exist_ok=True)
    marker = {
        'kind': 'synthetic_offline_baseline',
        'seed': config['runtime']['seed'],
        'python': platform.python_version(),
        'dependencies': {name: version(name) for name in ('numpy', 'pandas', 'scipy')},
    }
    (output_dir / 'benchmark.json').write_text(json.dumps(marker, indent=2) + '\n', encoding='utf-8')
    args = blade.build_parser().parse_args(['run', '--config', str(config_path)])
    # Only I/O boundaries are replaced; sampling, constraints, signatures, pending
    # queue, fitting, selection, diagnostics and Pareto export remain the real code.
    with patch.object(blade, 'load_config', return_value=config), \
            patch.object(blade, 'run_geometry', side_effect=fake_geometry), \
            patch.object(blade, 'run_cfx_pipeline', side_effect=synthetic_cfx):
        blade.run_loop(args)
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--output-dir', type=Path, help='New or empty directory; default: a new runs/run-* directory.')
    args = parser.parse_args()
    out = args.output_dir
    if out is None:
        runs = DEFAULT_CONFIG.parent / 'runs'
        runs.mkdir(parents=True, exist_ok=True)
        out = Path(tempfile.mkdtemp(prefix='run-', dir=runs))
    try:
        run_baseline(out, args.config)
        compare_outputs(out)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Offline baseline FAILED: {exc}\n')
    print(f'Offline baseline matches reference. Synthetic outputs: {out.resolve()}')


if __name__ == '__main__':
    main()
