"""Config seams: name-based variable mapping, surrogate fallback, command-file
version header and the declared operating-point check. Offline only."""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import numpy as np

import blade_shape_active_learning as b
import blade_shape_cfx_runner as cfx
import blade_shape_flow_diagnostics as fd
import blade_shape_refinement as r
from blade_gui import commands
from blade_gui.project import validate_config
from tests.cfx_output_samples import output as cfx_output

FIXTURE = Path('tests/fixtures/synthetic_meanline.cft-batch').resolve()


def positional_geometry(baseline, x):
    """The pre-refactor mapping, kept verbatim as the bit-identity reference."""
    offsets = np.array(x, dtype=float)
    hub_offsets = np.deg2rad(offsets[0:5])
    shroud_offsets = np.deg2rad(offsets[5:10])
    hub_theta = baseline.hub_theta_rad + math.radians(float(offsets[10]))
    shroud_theta = baseline.shroud_theta_rad + math.radians(float(offsets[11]))
    return {
        "hub_beta_rad": (baseline.hub_beta_rad + hub_offsets).tolist(),
        "shroud_beta_rad": (baseline.shroud_beta_rad + shroud_offsets).tolist(),
        "hub_theta_rad": float(hub_theta),
        "shroud_theta_rad": float(shroud_theta),
        "baseline_hub_beta_rad": baseline.hub_beta_rad.tolist(),
        "baseline_shroud_beta_rad": baseline.shroud_beta_rad.tolist(),
        "baseline_hub_theta_rad": float(baseline.hub_theta_rad),
        "baseline_shroud_theta_rad": float(baseline.shroud_theta_rad),
        "hub_x": baseline.hub_x.tolist(),
        "shroud_x": baseline.shroud_x.tolist(),
    }


class VariableMappingTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path('blade_shape_config.json').read_text())
        self.config['paths']['cft_batch_template'] = str(FIXTURE)
        self.baseline = b.extract_baseline(self.config)
        rng = np.random.default_rng(7)
        lb, ub = b.lower_bounds(self.config), b.upper_bounds(self.config)
        self.samples = [r.enforce_fixed(self.config, lb + rng.random(12) * (ub - lb)) for _ in range(50)]
        self.samples.append(np.full(12, 1.6))  # violates the theta limit
        self.samples.append(np.full(12, 5.9))  # violates several rules

    def test_current_order_is_bit_identical_to_positional_mapping(self):
        self.assertEqual(b.variable_names(self.config), list(b.GEOMETRY_VARIABLES))
        for x in self.samples:
            self.assertEqual(b.candidate_geometry(self.config, self.baseline, x),
                             positional_geometry(self.baseline, x))

    def test_reordered_variables_map_by_name(self):
        reordered = copy.deepcopy(self.config)
        order = list(range(12))[::-1]
        order[0], order[5] = order[5], order[0]
        reordered['variables'] = [self.config['variables'][i] for i in order]
        for x in self.samples:
            y = x[order]
            self.assertEqual(b.candidate_geometry(reordered, self.baseline, y),
                             b.candidate_geometry(self.config, self.baseline, x))
            self.assertEqual(b.constraint_violations(reordered, self.baseline, y),
                             b.constraint_violations(self.config, self.baseline, x))
        theta = np.zeros(12)
        theta[b.variable_names(reordered).index('shroud_theta_deg_offset')] = 1.6
        self.assertIn('theta_offset', b.constraint_violations(reordered, self.baseline, theta))

    def test_missing_or_unmapped_names_are_rejected(self):
        missing = copy.deepcopy(self.config)
        missing['variables'] = missing['variables'][:-1]
        with self.assertRaisesRegex(ValueError, 'shroud_theta_deg_offset'):
            b.geometry_indices(missing)
        renamed = copy.deepcopy(self.config)
        renamed['variables'][3]['name'] = 'splitter_beta_0_deg_offset'
        with self.assertRaisesRegex(ValueError, 'splitter_beta_0_deg_offset'):
            b.candidate_geometry(renamed, self.baseline, np.zeros(12))

    def test_load_config_rejects_unmapped_variable(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'BLADE_SHAPE_LOCAL_INI': str(Path(tmp) / 'absent.ini')}):
            bad = copy.deepcopy(self.config)
            bad['variables'][0]['name'] = 'hub_beta_9_deg_offset'
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(bad))
            with redirect_stderr(StringIO()), self.assertRaisesRegex(ValueError, 'hub_beta_0_deg_offset'):
                b.load_config(path)


class SurrogateFallbackTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path('blade_shape_config.json').read_text())
        rng = np.random.default_rng(3)
        self.x = rng.random((6, 12))
        self.y = rng.random((6, 2))

    def fit(self, model, fallback, x=None):
        self.config['surrogate'].update(model=model, fallback_model=fallback)
        with redirect_stdout(StringIO()):
            return b.fit_surrogate(self.config, self.x if x is None else x, self.y[:len(self.x if x is None else x)], 1)

    def test_rbf_is_used_directly(self):
        self.assertIsInstance(self.fit('rbf', 'none'), b.RbfRidgeEnsemble)

    def test_unknown_model_names_are_errors(self):
        with self.assertRaisesRegex(ValueError, 'surrogate.model'):
            self.fit('random_forest', 'rbf_ridge_ensemble')
        with self.assertRaisesRegex(ValueError, 'fallback_model'):
            self.fit('gp', 'linear')

    def test_unavailable_gp_uses_configured_fallback_or_stops(self):
        with patch.object(b, 'GaussianProcessRegressor', None):
            self.assertIsInstance(self.fit('gp', 'rbf_ridge_ensemble'), b.RbfRidgeEnsemble)
            with self.assertRaisesRegex(RuntimeError, 'scikit-learn'):
                self.fit('gp', 'none')
            with self.assertRaisesRegex(RuntimeError, 'cannot replace'):
                self.fit('gp', 'gp')
        # scikit-learn is optional: make it look installed so only the sample count is at stake
        with patch.object(b, 'GaussianProcessRegressor', object), \
                self.assertRaisesRegex(RuntimeError, 'at least 3 samples'):
            self.fit('gp', 'none', x=self.x[:2])

    def test_gui_flags_invalid_surrogate(self):
        self.config['surrogate']['fallback_model'] = 'linear'
        self.assertTrue(any('代理模型' in issue.message for issue in validate_config(self.config)
                            if issue.level == 'error'))


class CommandFileVersionTests(unittest.TestCase):
    def test_header_stays_on_the_verified_syntax_version(self):
        for version in (None, '', '251', 'v251', '25.1', '252', '261'):
            self.assertEqual(cfx.command_file_version(version), '25.1')
        for version in ('242', 'v232', 'abc', '2025R1'):
            with self.assertRaises(ValueError):
                cfx.command_file_version(version)
        self.assertEqual(cfx.ansys_release('v261'), (26, 1))

    def test_generated_pre_script_and_post_session_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            paths = cfx.write_cfx_pre_inputs(work, p_out_pa=10.0, template_cfx='C:/t/x.cfx', ansys_version='261')
            expected = ('COMMAND FILE:\n  CFX Pre Version = 25.1\nEND\n>load filename=C:/t/x.cfx\n>update\n'
                        f'>gtmImport filename={cfx._cfx_path(work / "Impeller_Mesh.gtm")}, type=GTM, '
                        'units=m, nameStrategy=Assembly\n>update\n'
                        f'>writeCaseFile filename={cfx._cfx_path(work / "Impeller.def")}, '
                        'operation=write def file\n>update\n>quit\n')
            self.assertEqual(paths['pre_script'].read_text(), expected)
        session = fd.render_session({'a': '1'}, ansys_version='252')
        self.assertTrue(session.startswith('COMMAND FILE:\n  CFX Post Version = 25.1\nEND\n'))

    def test_cfx_input_signature_has_no_version_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / 'Impeller_Mesh.gtm').write_text('synthetic mesh')
            for name in ['template.cfx', 'extract.cse']:
                (work / name).write_text('synthetic template')
            bindir = work / 'bin'
            bindir.mkdir()
            for name in ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe']:
                (bindir / name).write_text('never executed')

            def command(cmd, cwd, log):
                name = Path(cmd[0]).name
                if name == 'cfx5pre.exe':
                    (work / 'Impeller.def').write_text('def')
                elif name == 'cfx5solve.exe':
                    (work / 'run.res').write_text('res')
                    (work / 'run.out').write_text(cfx_output())
                else:
                    (work / 'CFX_Results.txt').write_text('0.76,2,1,.42,2.1')
                return 0
            with patch.object(cfx, '_run_logged', side_effect=command):
                self.assertTrue(cfx.run_cfx_pipeline(work, 'case_0', p_out_pa=10, cfx_bin_dir=bindir,
                                                     template_cfx=work / 'template.cfx',
                                                     template_cse=work / 'extract.cse').success)
            state = json.loads((work / 'cfx_state.json').read_text())
        self.assertEqual(set(state['inputs']), {'run_id', 'mesh', 'candidate', 'template_cfx', 'template_cse',
                                                'p_out_pa', 'cores', 'n_blades', 'cfx_bin_dir', 'convergence'})
        self.assertEqual(state['signature'], cfx.digest(state['inputs']))


CCL = '''LIBRARY:
  CEL:
    EXPRESSIONS:
      MyBackPressure = 10 [Pa]
      Speed = 10000 [rev min^-1]
    END
  END
END
FLOW: Flow Analysis 1
  DOMAIN: R1
    DOMAIN MODELS:
      DOMAIN MOTION:
        Angular Velocity = Speed
        Option = Rotating
      END
    END
  END
END
'''


class OperatingPointTests(unittest.TestCase):
    def test_rotor_speed_report(self):
        self.assertEqual(cfx.rotor_speed_report(CCL, 10000)['status'], 'match')
        rad = CCL.replace('= Speed', f'= -{10000 * math.pi / 30:.10f} [radian s^-1]')
        self.assertEqual(cfx.rotor_speed_report(rad, 10000)['status'], 'match')
        self.assertEqual(cfx.rotor_speed_report(CCL, 10500)['status'], 'mismatch')
        report = cfx.rotor_speed_report(CCL.replace('= Speed', '= Speed * 2'), 10000)
        self.assertEqual((report['status'], report['unresolved']), ('unverified', ['Speed * 2']))
        self.assertEqual(cfx.rotor_speed_report('FLOW: F\nEND\n', 10000)['status'], 'unverified')

    def test_declared_values_stay_in_the_physical_signature(self):
        config = json.loads(Path('blade_shape_config.json').read_text())
        before = r.physical_signature(config)
        for key in ('rpm', 'mass_flow', 'alpha0'):
            changed = copy.deepcopy(config)
            changed['runtime'][key] += 1.0
            self.assertNotEqual(r.physical_signature(changed), before, key)

    def check_pre(self, rpm, definition=True):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'BLADE_SHAPE_LOCAL_INI': str(Path(tmp) / 'absent.ini')}):
            root = Path(tmp)
            bindir = root / 'bin'
            bindir.mkdir()
            for name in ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe', 'cfx5cmds.exe']:
                (bindir / name).write_text('never executed')
            for name in ['template.cfx', 'extract.cse', 'Impeller.def']:
                (root / name).write_text('synthetic')
            config = json.loads(Path('blade_shape_config.json').read_text())
            config['runtime']['rpm'] = rpm
            config['paths'].update(cfx_bin_dir=str(bindir), template_cfx=str(root / 'template.cfx'),
                                   template_cse=str(root / 'extract.cse'))
            config_path = root / 'config.json'
            config_path.write_text(json.dumps(config))
            calls = []

            def command(cmd, cwd, log):
                calls.append(cmd)
                Path(cmd[cmd.index('-text') + 1]).write_text(CCL)
                return 0
            out = StringIO()
            with patch.object(cfx, '_run_logged', side_effect=command), redirect_stdout(out):
                code = cfx.check_cfx_pre_inputs(config_path, root / 'check',
                                                root / 'Impeller.def' if definition else None)
            self.assertEqual((root / 'Impeller.def').read_text(), 'synthetic')
        return code, calls, out.getvalue()

    def test_check_pre_compares_definition_rpm(self):
        code, calls, text = self.check_pre(10000.0)
        self.assertEqual(code, 0, text)
        self.assertEqual([Path(c[0]).name for c in calls], ['cfx5cmds.exe'])
        self.assertEqual(calls[0][1:3], ['-read', '-def'])
        self.assertIn('match', text)
        code, _, text = self.check_pre(12000.0)
        self.assertEqual(code, 1)
        self.assertIn('mismatch', text)
        code, calls, text = self.check_pre(12000.0, definition=False)
        self.assertEqual((code, calls), (0, []))
        self.assertIn('not checked', text)

    def test_gui_command_passes_definition(self):
        spec = commands.build_check_pre('c.json', working_dir='/tmp/check', definition='/tmp/case/Impeller.def')
        self.assertEqual(spec.args[-2:], ['--def', '/tmp/case/Impeller.def'])
        self.assertNotIn('--def', commands.build_check_pre('c.json', working_dir='/tmp/check').args)


if __name__ == '__main__':
    unittest.main()
