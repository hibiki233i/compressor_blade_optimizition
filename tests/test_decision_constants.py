"""Named decision constants keep today's numbers; seeds, margins and limits live in one place."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import blade_shape_acquisition as acquisition
import blade_shape_active_learning as b
import blade_shape_convergence as convergence
import blade_shape_refinement as refinement
from blade_shape_runtime import SEED_STREAMS, stream_seed
from tests.test_gui import PYSIDE_AVAILABLE, ProjectFixture

HAS_SCIPY = importlib.util.find_spec('scipy') is not None
FIXED = [1, 2, 6, 9, 11]


def fake_geometry(config, candidate_path, dry_run=False):
    (candidate_path.parent/'Impeller_Mesh.gtm').write_text('synthetic test mesh')
    return True, 'ok'


class Outcome:
    success = True
    message = 'synthetic test only'
    metrics = {'Efficiency': .762, 'MassFlow': 4.25, 'PressureRatio': 2., 'Power': 1., 'totalpressureratio': 2.1}


def synthetic_config(root: Path) -> dict:
    config = json.loads(Path('blade_shape_config.json').read_text())
    config['paths']['output_dir'] = str(root)
    config['surrogate']['model'] = 'rbf'
    names = b.variable_names(config)
    fixed = [names[i] for i in FIXED]
    config['search'] = {'active_variables': [n for n in names if n not in fixed],
                        'fixed_variables': {n: 0.0 for n in fixed}, 'slice_tolerance_norm': 1e-8}
    config['refinement']['challenger_min_samples'] = 4
    config['runtime'].update(initial_samples=0, nsga2_pop_size=6, nsga2_generations=2, candidate_pool_size=12, seed=42)
    config['surrogate'].update(ehvi_y_samples=3)
    return config


def selected_vectors(history: int) -> np.ndarray:
    """New candidates chosen by one fixed-seed active-learning iteration on synthetic data."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = synthetic_config(root)
        centre = np.zeros(12)
        centre[4] = 2.0
        for i in range(history):
            x = centre.copy()
            x[0] = i*.2
            b.append_row(config, {**b.vector_to_sample(config, x), 'run_id': f'case_{i:06d}', 'status': 'success',
                                  'sample_phase': 'active_learning', 'al_iteration': 13,
                                  'Efficiency': .760+i*.0001, 'MassFlow': 4.20+i*.01})
        path = root/'config.json'
        path.write_text(json.dumps(config))
        args = b.build_parser().parse_args(['run', '--config', str(path), '--resume', '--iterations', '1',
                                            '--max-new-cfd', '3'])
        baseline = b.BaselineShape(np.ones(5), np.ones(5), np.linspace(0, 1, 5), np.linspace(0, 1, 5), 0., 0.)
        with patch.object(b, 'extract_baseline', return_value=baseline), \
                patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', return_value=Outcome()):
            b.run_loop(args)
        training = b.load_training(config)
        return training.tail(3)[b.variable_names(config)].to_numpy(float)


# Recorded with the pre-refactor code (raw seed offsets), SciPy 1.17, rbf surrogate.
FALLBACK_GOLDEN = [
    [-0.485727985, 0.0, 0.0, 2.49412545, -1.176572833, 1.005017121, 0.0, -1.734198796, 0.737397447, 0.0, 1.360604206, 0.0],
    [-0.066566618, 0.0, 0.0, 1.691096049, 4.186885696, -2.47422949, 0.0, 0.662988674, -0.587440562, 0.0, -0.156411941, 0.0],
    [-1.387106381, 0.0, 0.0, -2.554795336, -1.797790687, -0.207563442, 0.0, 0.307688413, -0.283910166, 0.0, -1.431203974, 0.0]]
SURROGATE_GOLDEN = [
    [1.209711792, 0.0, 0.0, -0.165690181, 2.083397404, 0.094542733, 0.0, -0.090784907, -0.268740965, 0.0, 0.06264972, 0.0],
    [1.040641829, 0.0, 0.0, -0.187920987, 1.578473018, 0.368671768, 0.0, 0.005186152, 0.040856398, 0.0, -0.074333984, 0.0],
    [2.878100551, 0.0, 0.0, -0.328627685, 4.19855733, -1.760609964, 0.0, -2.028399444, -1.241214367, 0.0, -1.476189326, 0.0]]


class SeedStreamTests(unittest.TestCase):
    def test_offsets_are_the_historical_values(self):
        seed, it = 42, 5
        expected = {'doe_fallback': seed+1000, 'surrogate': seed+it, 'nsga2': seed+it*17,
                    'candidate_pool': seed+it*31, 'pool_global': seed+1, 'fallback_random': seed+it*97,
                    'ehvi_normals': seed+707, 'write_candidate': seed+it+100}
        self.assertEqual(set(SEED_STREAMS), set(expected))
        index = {'doe_fallback': 0, 'pool_global': 0, 'ehvi_normals': 0}
        for name, value in expected.items():
            self.assertEqual(stream_seed(seed, name, index.get(name, it)), value, name)


@unittest.skipUnless(HAS_SCIPY, 'golden values were recorded with SciPy LHS/Sobol')
class FixedSeedSelectionTests(unittest.TestCase):
    """One active-learning iteration with a fixed seed picks the same candidates as before."""

    def test_surrogate_iteration_selection_is_unchanged(self):
        np.testing.assert_allclose(selected_vectors(4), SURROGATE_GOLDEN, rtol=0, atol=1e-8)

    def test_fallback_random_selection_is_unchanged(self):
        np.testing.assert_allclose(selected_vectors(2), FALLBACK_GOLDEN, rtol=0, atol=1e-8)


class HypervolumeReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = synthetic_config(Path(self.tmp.name))

    def frame(self, y):
        names = b.variable_names(self.config)
        rows = [{**dict.fromkeys(names, 0.0), 'run_id': f'case_{i:06d}', 'status': 'success', 'sample_phase': 'doe',
                 'Efficiency': e, 'MassFlow': m} for i, (e, m) in enumerate(y)]
        return pd.DataFrame(rows)

    def test_both_modules_use_the_historical_formula(self):
        self.assertEqual(acquisition.HV_REFERENCE_MARGIN, 0.05)
        y = np.array([[.76, 4.2], [.765, 4.1], [.761, 4.3]])
        old = y.min(axis=0)-.05*np.maximum(np.ptp(y, axis=0), 1e-6)
        persisted = refinement.hv_reference(self.config, self.frame(y))
        self.assertTrue(np.array_equal(persisted, old))
        result = acquisition.expected_hvi(self.config, y, y[:1], np.full((1, 2), .01))
        self.assertTrue(np.array_equal(result['reference'], old))
        flat = np.array([[.76, 4.2], [.76, 4.2]])
        self.assertTrue(np.array_equal(acquisition.hv_reference_point(flat),
                                       flat.min(axis=0)-.05*np.maximum(np.ptp(flat, axis=0), 1e-6)))

    def test_persisted_reference_is_not_moved_by_the_constant(self):
        y = np.array([[.76, 4.2], [.765, 4.1]])
        first = refinement.hv_reference(self.config, self.frame(y))
        with patch.object(acquisition, 'HV_REFERENCE_MARGIN', .5):
            self.assertTrue(np.array_equal(refinement.hv_reference(self.config, self.frame(y)), first))


class DiagnosticGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = synthetic_config(self.root)
        self.config['refinement'].pop('diagnostic_gate', None)

    def write(self, misses, n=6):
        sid = refinement.slice_id(self.config)
        rows = [dict(run_id=f'case_{i:06d}', status='success', slice_id=sid, surrogate_model='m', candidate_role='ehvi',
                     pred_Efficiency=.76, true_Efficiency=.76+(.01 if i < misses else 0), std_Efficiency=.001,
                     pred_MassFlow=4.2, true_MassFlow=4.2, std_MassFlow=.01) for i in range(n)]
        pd.DataFrame(rows).to_csv(self.root/'active_learning_diagnostics.csv', index=False)
        self.config['pareto']['tolerances']['Efficiency'] = 1.0
        return refinement.write_diagnostics(self.config)

    def test_missing_key_keeps_five_sixths(self):
        self.assertEqual(refinement.diagnostic_min_coverage(self.config), 5/6)
        gate = self.write(1)
        self.assertTrue(gate['objectives']['Efficiency']['passed'])
        self.assertEqual(gate['min_coverage_2sigma'], 5/6)
        self.assertEqual(json.loads((self.root/'local_diagnostic_gate.json').read_text())['min_coverage_2sigma'], 5/6)
        self.assertFalse(self.write(2)['objectives']['Efficiency']['passed'])

    def test_configured_threshold_is_used_and_recorded(self):
        self.config['refinement']['diagnostic_gate'] = {'min_coverage_2sigma': 1.0}
        gate = self.write(1)
        self.assertFalse(gate['objectives']['Efficiency']['passed'])
        self.assertEqual(gate['min_coverage_2sigma'], 1.0)
        self.assertTrue(self.write(0)['objectives']['Efficiency']['passed'])

    def test_tracked_config_value_is_exactly_five_sixths(self):
        tracked = json.loads(Path('blade_shape_config.json').read_text())
        self.assertEqual(tracked['refinement']['diagnostic_gate']['min_coverage_2sigma'], 5/6)

    def test_invalid_thresholds_are_rejected_on_load(self):
        from blade_gui.project import validate_config
        for bad in (0, 1.5, 'high', True, None, -0.1):
            with self.subTest(bad=bad):
                self.config['refinement']['diagnostic_gate'] = {'min_coverage_2sigma': bad}
                with self.assertRaises(ValueError):
                    refinement.diagnostic_min_coverage(self.config)
                self.assertTrue(any('诊断门' in issue.message for issue in validate_config(self.config)))
        path = self.root/'config.json'
        path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, 'min_coverage_2sigma'):
            b.load_config(path)

    def test_calibration_and_interval_constants_reproduce_the_old_numbers(self):
        self.assertEqual((refinement.CALIBRATION_COVERAGE, refinement.INTERVAL_SIGMAS), (.95, 2.0))
        rng = np.random.default_rng(3)
        e, std = pd.Series(rng.normal(size=9)), pd.Series(rng.uniform(.5, 2, size=9))
        frame = pd.DataFrame({'status': 'success', 'run_id': [f'r{i}' for i in range(9)], 'candidate_role': 'ehvi',
                              'pred_Efficiency': 0., 'true_Efficiency': e, 'std_Efficiency': std})
        row = refinement.role_diagnostics(frame).iloc[0]
        self.assertEqual(row.coverage_2sigma, np.mean(e.abs() <= 2*std))
        self.assertEqual(row.mean_interval_width_2sigma, (4*std).mean())

        class Model:
            def predict(self, x):
                return np.zeros((len(x), 2)), np.ones((len(x), 2))
        names = b.variable_names(self.config)
        history = pd.DataFrame([{**dict.fromkeys(names, 0.), 'run_id': 'case_000000', 'status': 'success',
                                 'Efficiency': .76, 'MassFlow': 4.2}])
        ratios = np.array([.5, 1., 2., 3., 4.5, 6., 7.])
        diag = pd.DataFrame({'run_id': [f'r{i}' for i in range(7)], 'status': 'success',
                             'slice_id': refinement.slice_id(self.config), 'surrogate_model': 'Model:full12',
                             'true_Efficiency': ratios, 'pred_Efficiency': 0., 'raw_std_Efficiency': 1.,
                             'true_MassFlow': ratios/10, 'pred_MassFlow': 0., 'raw_std_MassFlow': 1.})
        surrogate = refinement.ConditionalSurrogate(self.config, history, lambda *a: Model(), 0, diag)
        self.assertEqual(surrogate.scale.tolist(), [max(1., float(np.quantile(ratios, .95))/2.), 1.])


class ConvergenceLimitTests(unittest.TestCase):
    def test_limits_are_the_named_constants(self):
        self.assertEqual((convergence.RMS_TARGET_MAX, convergence.RESTART_ITERATIONS_RANGE), (1e-5, (1500, 2000)))
        policy = convergence.ConvergencePolicy()
        self.assertEqual((policy.rms_target, policy.restart_iterations), (1e-5, 2000))
        convergence.ConvergencePolicy(rms_target=convergence.RMS_TARGET_MAX, restart_iterations=1500)
        with patch.object(convergence, 'RESTART_ITERATIONS_RANGE', (1000, 2000)):
            convergence.ConvergencePolicy(restart_iterations=1000)

    def test_help_text_derives_from_the_constants(self):
        from blade_gui.field_help import HELP
        self.assertIn('1500–2000', HELP['cfx_convergence.restart_iterations'].hint)
        self.assertIn('RESTART_ITERATIONS_RANGE', HELP['cfx_convergence.restart_iterations'].text)
        self.assertIn('(0, 1e-05]', HELP['cfx_convergence.rms_target'].text)


class GeometryScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = synthetic_config(self.root/'out')
        current = Path('Run-BladeShapeGeometryMeshing.ps1').read_text(encoding='utf-8-sig')
        self.old = self.root/'old.ps1'
        self.old.write_text(current.replace('    [ValidateRange(1, 200)][int]$BladeCount = 10,\n', ''), encoding='utf-8')
        self.assertNotEqual(self.old.read_text(encoding='utf-8'), current)
        self.config['paths'].update(powershell_exe='pwsh', cfturbo_exe='c', turbogrid_exe='t',
                                    geometry_script_path=str(self.old))

    def test_repository_script_declares_blade_count(self):
        text = Path('Run-BladeShapeGeometryMeshing.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('bladecount', b.script_parameters(text))
        self.assertNotIn('bladecount', b.script_parameters(self.old.read_text(encoding='utf-8')))
        # a function's parameter is not the script's
        self.assertEqual(b.script_parameters('function f { param([int]$BladeCount) }'), set())
        self.assertEqual(b.script_parameters('<# (#> [CmdletBinding()]\nparam(\n # (\n [int]$BladeCount\n)'),
                         {'bladecount'})

    def test_old_copy_stops_before_any_external_call(self):
        candidate = self.root/'case'/'candidate.json'
        candidate.parent.mkdir()
        candidate.write_text('{}')
        with patch.object(b.subprocess, 'run') as run:
            with self.assertRaisesRegex(b.GeometryScriptOutdated, 'older than the repository'):
                b.run_geometry(self.config, candidate)
            run.assert_not_called()
        self.config['paths']['geometry_script_path'] = str(Path('Run-BladeShapeGeometryMeshing.ps1').resolve())
        with patch.object(b.subprocess, 'run', return_value=__import__('subprocess').CompletedProcess([], 0)) as run:
            b.run_geometry(self.config, candidate)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('-BladeCount')+1], str(self.config['runtime']['n_blades']))

    def test_old_copy_leaves_no_case_state(self):
        baseline = b.BaselineShape(np.ones(5), np.ones(5), np.linspace(0, 1, 5), np.linspace(0, 1, 5), 0., 0.)
        with self.assertRaises(b.GeometryScriptOutdated):
            b.evaluate_true_cfd(self.config, baseline, np.zeros(12), 0, sample_phase='doe')
        self.assertFalse((self.root/'out'/'cases'/'case_000000'/'geometry_state.json').exists())
        self.assertFalse((self.root/'out'/'training_data.csv').exists())
        path = self.root/'config.json'
        path.write_text(json.dumps(self.config))
        args = b.build_parser().parse_args(['run', '--config', str(path), '--max-new-cfd', '1'])
        with self.assertRaises(b.GeometryScriptOutdated):
            b.run_loop(args)
        self.assertFalse((self.root/'out'/'pending_evaluations.json').exists())


@unittest.skipUnless(PYSIDE_AVAILABLE, 'PySide6 is not installed')
class ConfigPageConstantTests(ProjectFixture):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        from blade_gui.context import AppContext
        from blade_gui.pages.config_page import ConfigPage
        self.ctx = AppContext(self.config_path, self.out)
        self.page = ConfigPage(self.ctx)
        self.page.reload_from_context()

    def tearDown(self):
        self.page.deleteLater()
        self.app.processEvents()
        super().tearDown()

    def test_field_ranges_follow_the_convergence_constants(self):
        rms = self.page._bindings['cfx_convergence.rms_target'][1]
        restart = self.page._bindings['cfx_convergence.restart_iterations'][1]
        self.assertEqual(rms.maximum(), convergence.RMS_TARGET_MAX)
        self.assertEqual((restart.minimum(), restart.maximum()), convergence.RESTART_ITERATIONS_RANGE)

    def test_coverage_default_round_trips_exactly(self):
        self.assertNotIn('diagnostic_gate', self.config.get('refinement', {}))
        key = 'refinement.diagnostic_gate.min_coverage_2sigma'
        widget = self.page._bindings[key][1]
        self.assertAlmostEqual(widget.value(), 5/6, places=4)
        self.assertEqual(self.page._collect()['refinement']['diagnostic_gate']['min_coverage_2sigma'], 5/6)
        self.assertEqual(self.page._rows[key].hint.text(), '6 点时允许 1 个落在 2σ 外')
        widget.setValue(.9)
        self.assertEqual(self.page._collect()['refinement']['diagnostic_gate']['min_coverage_2sigma'], .9)

    def test_settings_alias_is_gone(self):
        self.assertFalse(hasattr(self.ctx, '_read_setting') or hasattr(self.ctx, '_write_setting'))
        self.assertEqual(self.ctx.read_setting('missing', 'x'), 'x')


if __name__ == '__main__':
    unittest.main()
