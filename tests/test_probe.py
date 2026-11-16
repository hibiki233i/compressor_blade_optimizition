"""Offline behaviour of the fixed eight-point probe; external tools are stand-ins."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import blade_shape_active_learning as b
import blade_shape_probe as probe
import blade_shape_signature as signatures
from blade_shape_cfx_runner import CfxResult
from tests import legacy_box
from tests.synthetic_pipeline import fake_geometry

CENTER = {'hub_beta_0_deg_offset': 1.0, 'hub_beta_3_deg_offset': .5, 'hub_beta_4_deg_offset': 1.5,
          'shroud_beta_0_deg_offset': 2.0, 'shroud_beta_2_deg_offset': -.5, 'shroud_beta_3_deg_offset': .4,
          'hub_theta_deg_offset': .5}
GOOD = {'Efficiency': .80, 'MassFlow': 4.6, 'PressureRatio': 2.5, 'Power': 1.8e11, 'totalpressureratio': 2.6}


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # the probe was built for the earlier frozen seven-variable slice
        config = legacy_box.pin(json.loads(Path('blade_shape_config.json').read_text(encoding='utf-8')), frozen=True)
        paths = config['paths']
        paths['output_dir'] = str(self.root / 'optimizer')
        paths['cft_batch_template'] = str(Path('tests/fixtures/synthetic_meanline.cft-batch').resolve())
        for key in ('base_cft', 'turbogrid_template', 'template_cfx', 'template_cse'):
            path = self.root / key
            path.write_text('synthetic fixture; never execute')
            paths[key] = str(path)
        script = self.root / 'geometry_script'
        script.write_text('param([int]$BladeCount = 10)\n# synthetic fixture; never execute')
        paths['geometry_script_path'] = str(script)
        solver = self.root / 'cfx_bin'
        solver.mkdir()
        for tool in ('cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe'):
            (solver / tool).write_text('synthetic fixture; never execute')
        paths['cfx_bin_dir'] = str(solver)
        paths['template_dir'] = str(self.root)
        for key in ('powershell_exe', 'cfturbo_exe', 'turbogrid_exe'):
            path = self.root / key
            path.write_text('synthetic fixture; never execute')
            paths[key] = str(path)
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps(config))
        self.config = b.load_config(self.config_path)
        self.names = b.variable_names(self.config)
        self.center = [CENTER.get(name, 0.0) for name in self.names]
        self.write_center()
        self.out = self.root / 'probe'
        self.outcomes = {}
        self.calls = []

    def write_center(self, **changes):
        row = {**dict(zip(self.names, self.center)), **GOOD, 'run_id': 'case_000040', 'status': 'success',
               'sample_phase': 'al', 'failure_stage': '', 'message': '',
               signatures.COLUMN: signatures.physical_signature(self.config), **changes}
        b.output_dir(self.config).mkdir(exist_ok=True)
        pd.DataFrame([row]).to_csv(b.training_csv_path(self.config), index=False)

    def cfx(self, case_dir, run_id, **kwargs):
        """Recorded center values by default; a test overrides one run_id with a result."""
        self.calls.append(run_id)
        return self.outcomes.get(run_id, CfxResult(True, dict(GOOD), 'synthetic'))

    def run_probe(self, budget, *extra, output=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ['--config', str(self.config_path), '--center-run-id', 'case_000040',
                '--output-dir', str(output or self.out), '--max-new-cfd', str(budget), *extra]
        with mock.patch.object(b, 'run_geometry', fake_geometry), mock.patch.object(b, 'run_cfx_pipeline', self.cfx), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = probe.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def results(self):
        return pd.read_csv(self.out / probe.RESULTS_NAME).set_index('point_id')

    def study_rows(self):
        return pd.read_csv(self.out / 'training_data.csv').set_index('design_role')

    def test_points_change_one_coordinate_from_the_center_and_lift_only_the_extension_bound(self):
        study = probe.experiment_config(self.config, self.out)
        plan = probe.build_plan(study, probe.load_center(self.config, 'case_000040'))
        ids = [point['point_id'] for point in plan['points']]
        self.assertEqual(ids, ['center_replicate', 'shroud_beta_0_3.5', 'shroud_beta_0_4.0', 'hub_beta_1_-1.0',
                               'hub_beta_2_-1.0', 'shroud_beta_1_-1.0', 'shroud_beta_4_+1.0', 'shroud_theta_-0.5'])
        self.assertEqual(plan['points'][0]['x'], self.center)
        for point in plan['points'][1:]:
            changed = [name for name, a, c in zip(self.names, point['x'], self.center) if a != c]
            self.assertEqual(len(changed), 1, point)
        self.assertEqual(plan['points'][2]['x'][self.names.index('shroud_beta_0_deg_offset')], 4.0)
        self.assertEqual(signatures.physical_signature(study), signatures.physical_signature(self.config))
        uppers = {v['name']: v['upper'] for v in study['variables']}
        for variable in self.config['variables']:
            if variable['name'] != 'shroud_beta_0_deg_offset':
                self.assertEqual(uppers[variable['name']], variable['upper'])
        shared = json.loads(self.config_path.read_text())
        self.assertEqual(next(v['upper'] for v in shared['variables'] if v['name'] == 'shroud_beta_0_deg_offset'), 3.0)

    def test_zero_budget_checks_rules_and_creates_nothing(self):
        before = b.training_csv_path(self.config).read_bytes()
        code, out, err = self.run_probe(0)
        self.assertEqual((code, err), (0, ''))
        self.assertIn('passed for all points', out)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.calls, [])
        self.assertEqual(b.training_csv_path(self.config).read_bytes(), before)

    def test_budget_and_resume_finish_eight_points_without_touching_main_training(self):
        before = b.training_csv_path(self.config).read_bytes()
        self.assertEqual(self.run_probe(1)[0], 0)
        self.assertEqual(self.calls, ['case_000000'])
        self.assertEqual(self.results().status.eq('not_run').sum(), 7)
        code, _, err = self.run_probe(1)
        self.assertEqual(code, 2)
        self.assertIn('--resume', err)
        self.assertEqual(self.run_probe(100, '--resume')[0], 0)
        self.assertEqual(self.calls, [f'case_{i:06d}' for i in range(8)])
        table = self.results()
        self.assertTrue(table.status.eq('success').all())
        self.assertAlmostEqual(table.loc['center_replicate', 'delta_eta_pp'], 0.0)
        self.run_probe(100, '--resume')
        self.assertEqual(len(self.calls), 8, 'recorded points are never recomputed')
        self.assertEqual(b.training_csv_path(self.config).read_bytes(), before)
        self.assertFalse((b.output_dir(self.config) / 'cases').exists())

    def test_reference_is_the_recomputed_center(self):
        self.outcomes['case_000000'] = CfxResult(True, {**GOOD, 'Efficiency': GOOD['Efficiency'] + 1e-4}, 'synthetic')
        self.outcomes['case_000001'] = CfxResult(True, {**GOOD, 'Efficiency': GOOD['Efficiency'] + 2e-3}, 'synthetic')
        self.run_probe(2)
        self.assertAlmostEqual(self.results().loc['shroud_beta_0_3.5', 'delta_eta_pp'], .19, places=6)

    def test_failed_3p5_skips_4p0_and_stays_skipped_on_resume(self):
        self.outcomes['case_000001'] = CfxResult(False, {}, 'residuals high', 'solve')
        self.assertEqual(self.run_probe(100)[0], 0)
        self.assertNotIn('case_000002', self.calls)
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(self.results().loc['shroud_beta_0_4.0', 'status'], 'skipped')
        self.run_probe(100, '--resume')
        self.assertEqual(len(self.calls), 7, 'a recorded failure is never recomputed')
        self.assertEqual(self.results().loc['shroud_beta_0_4.0', 'status'], 'skipped')

    def test_failed_frozen_single_does_not_stop_the_others(self):
        self.outcomes['case_000003'] = CfxResult(False, {}, 'residuals high', 'solve')
        self.assertEqual(self.run_probe(100)[0], 0)
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(self.results().loc['hub_beta_1_-1.0', 'status'], 'failed')

    def test_review_keeps_values_and_continues(self):
        self.outcomes['case_000001'] = CfxResult(False, {**GOOD, 'Efficiency': .81}, 'p-mass 1.5e-5', review=True)
        self.assertEqual(self.run_probe(100)[0], 0)
        self.assertEqual(len(self.calls), 8)
        row = self.results().loc['shroud_beta_0_3.5']
        self.assertEqual(row.status, 'review')
        self.assertAlmostEqual(row.delta_eta_pp, 1.0, places=6)

    def test_replicate_failure_or_mismatch_stops_all_new_cfd(self):
        self.outcomes['case_000000'] = CfxResult(False, {}, 'solver crashed', 'solve')
        code, _, err = self.run_probe(100)
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, ['case_000000'])
        self.assertIn('center_replicate', err)
        self.assertEqual(self.run_probe(100, '--resume')[0], 1)
        self.assertEqual(self.calls, ['case_000000'])

        self.calls.clear()
        self.outcomes['case_000000'] = CfxResult(True, {**GOOD, 'Efficiency': GOOD['Efficiency'] + 5e-4}, 'synthetic')
        other = self.root / 'second'
        self.assertEqual(self.run_probe(100, output=other)[0], 1)
        self.assertEqual(self.calls, ['case_000000'])

    def test_replicate_within_tolerance_in_review_continues(self):
        self.outcomes['case_000000'] = CfxResult(False, {**GOOD, 'Efficiency': GOOD['Efficiency'] + 1e-4},
                                                 'p-mass 1.1e-5', review=True)
        self.assertEqual(self.run_probe(2)[0], 0)
        self.assertEqual(self.calls, ['case_000000', 'case_000001'])

    def test_environment_failure_stops(self):
        def missing(config, candidate_path, dry_run=False):
            raise OSError('pwsh not found')
        self.assertEqual(self.run_probe(1)[0], 0)
        with mock.patch.object(probe.b, 'check_geometry_script'):
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(b, 'run_geometry', missing), mock.patch.object(b, 'run_cfx_pipeline', self.cfx), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = probe.main(['--config', str(self.config_path), '--center-run-id', 'case_000040',
                                   '--output-dir', str(self.out), '--max-new-cfd', '100', '--resume'])
        self.assertEqual(code, 1)
        self.assertIn('environment', stderr.getvalue())
        self.assertEqual(self.calls, ['case_000000'])
        self.assertEqual(self.results().loc['shroud_beta_0_3.5', 'status'], 'failed')
        self.assertEqual(self.results().loc['shroud_beta_0_4.0', 'status'], 'not_run')

    def test_interrupted_point_is_recomputed_on_resume_with_same_run_id(self):
        self.outcomes['case_000001'] = CfxResult(True, dict(GOOD), 'unused')
        state = {'interrupt': True}

        def cfx(case_dir, run_id, **kwargs):
            if run_id == 'case_000001' and state.pop('interrupt', False):
                raise KeyboardInterrupt
            return self.cfx(case_dir, run_id, **kwargs)
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ['--config', str(self.config_path), '--center-run-id', 'case_000040', '--output-dir', str(self.out),
                '--max-new-cfd', '100']
        with mock.patch.object(b, 'run_geometry', fake_geometry), mock.patch.object(b, 'run_cfx_pipeline', cfx), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(probe.main(argv), 130)
        self.assertEqual(self.study_rows().loc['shroud_beta_0_3.5', 'status'], 'interrupted')
        self.assertEqual(self.calls, ['case_000000'])
        self.assertEqual(self.run_probe(100, '--resume')[0], 0)
        self.assertEqual(self.calls.count('case_000000'), 1)
        self.assertEqual(self.calls.count('case_000001'), 1)
        self.assertEqual(len(self.calls), 8)
        self.assertTrue(self.results().status.eq('success').all())

    def test_output_must_be_separate_and_empty(self):
        main = b.output_dir(self.config)
        for bad in (main, main / 'probe', self.root):
            code, _, err = self.run_probe(1, output=bad)
            self.assertEqual(code, 2, bad)
            self.assertIn('separate', err)
        self.out.mkdir()
        (self.out / 'leftover.txt').write_text('x')
        code, _, err = self.run_probe(1)
        self.assertEqual(code, 2)
        self.assertIn('leftover.txt', err)
        self.assertEqual(self.calls, [])

    def test_center_must_be_compatible_success(self):
        for changes in ({'status': 'review'}, {signatures.COLUMN: 'other'}, {'run_id': 'case_000041'}):
            self.write_center(**changes)
            code, _, err = self.run_probe(1)
            self.assertEqual(code, 2, changes)
            self.assertIn('Center case_000040', err)
        self.assertEqual(self.calls, [])

    def test_changed_inputs_refuse_an_existing_probe(self):
        self.run_probe(1)
        template = Path(self.config['paths']['template_cfx'])
        before = signatures.physical_signature(self.config)
        template.write_text('changed physics')
        self.config = b.load_config(self.config_path)
        self.assertNotEqual(signatures.physical_signature(self.config), before)
        self.write_center()
        code, _, err = self.run_probe(100, '--resume')
        self.assertEqual(code, 2)
        self.assertIn('different probe', err)
        self.assertEqual(self.calls, ['case_000000'])

    def test_rule_violation_refuses_the_whole_plan_before_any_cfd(self):
        config = json.loads(self.config_path.read_text())
        config['constraints']['max_beta_offset_deg'] = 3.8
        self.config_path.write_text(json.dumps(config))
        self.config = b.load_config(self.config_path)
        self.write_center()
        code, _, err = self.run_probe(100)
        self.assertEqual(code, 2)
        self.assertIn('shroud_beta_0_4.0', err)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.out.exists())

    def test_missing_external_tools_are_found_before_any_cfd(self):
        (Path(self.config['paths']['cfx_bin_dir']) / 'cfx5post.exe').unlink()
        Path(self.config['paths']['cfturbo_exe']).unlink()
        code, _, err = self.run_probe(1)
        self.assertEqual(code, 2)
        self.assertIn('cfx5post.exe', err)
        self.assertIn('cfturbo_exe', err)
        self.assertFalse(self.out.exists())

    def test_plan_identity_ignores_a_rewritten_center_row(self):
        self.run_probe(1)
        names = dict(zip(self.names, self.center))
        names['hub_beta_0_deg_offset'] += 1e-15
        self.write_center(**names, Efficiency=GOOD['Efficiency'] + 1e-15)
        self.assertEqual(self.run_probe(1, '--resume')[0], 0)
        self.assertEqual(self.calls, ['case_000000', 'case_000001'])

    def test_results_report_work_and_lost_work(self):
        self.run_probe(1)
        row = self.results().loc['center_replicate']
        work = GOOD['Power'] / GOOD['MassFlow'] * 1e-6
        self.assertAlmostEqual(row.work_J_kg, work)
        self.assertAlmostEqual(row.lost_work_J_kg, (1 - GOOD['Efficiency']) * work)
        self.assertTrue(np.isnan(self.results().loc['shroud_beta_0_3.5', 'Efficiency']))


if __name__ == '__main__':
    unittest.main()
