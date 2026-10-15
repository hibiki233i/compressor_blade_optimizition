"""Offline benchmark: repeatability, isolation and useful regression failures."""
from __future__ import annotations

import importlib.util
import json
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


@unittest.skipUnless(importlib.util.find_spec('scipy'), 'offline reference uses SciPy LHS/Sobol')
class OfflineBaselineTests(unittest.TestCase):
    def test_changed_sampling_seed_fails_numerical_reference(self):
        # Exercise a real upstream parameter change, not a corrupted CSV.
        # Selection, objective values and diagnostics must stay tied to seed 42.
        runner = self.runner()
        compare = runpy.run_path('benchmarks/compare_offline_baseline.py')['compare_outputs']
        config = json.loads(Path('benchmarks/offline_baseline/config.json').read_text())
        config['runtime']['seed'] = 43
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            changed_config = root / 'config.json'
            changed_config.write_text(json.dumps(config), encoding='utf-8')
            runner['run_baseline'](root / 'output', changed_config)
            with self.assertRaisesRegex(ValueError, r'training_data\.csv:'):
                compare(root / 'output')

    def runner(self):
        script = Path('benchmarks/run_offline_baseline.py')
        self.assertTrue(script.is_file(), 'one-command offline benchmark is missing')
        return runpy.run_path(str(script))

    def test_run_is_repeatable_and_never_calls_engineering_tools_or_local_ini(self):
        # A leaked INI path, real tool call, fixed-coordinate drift, or changed seed fails here.
        runner = self.runner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ini = root / 'machine.ini'
            ini.write_text('[paths]\noutput_dir = DO_NOT_USE\ncft_batch_template = MISSING\n')
            with patch.dict('os.environ', {'BLADE_SHAPE_LOCAL_INI': str(ini)}), \
                    patch('subprocess.Popen', side_effect=AssertionError('external process invoked')):
                for name in ('first', 'second'):
                    runner['run_baseline'](root / name)
            for name in ('training_data.csv', 'pareto_front.csv', 'active_learning_diagnostics.csv'):
                first = pd.read_csv(root / 'first' / name).drop(columns=['case_dir'], errors='ignore')
                second = pd.read_csv(root / 'second' / name).drop(columns=['case_dir'], errors='ignore')
                pd.testing.assert_frame_equal(first, second)
            training = pd.read_csv(root / 'first' / 'training_data.csv')
            self.assertEqual(len(training), 18)
            self.assertEqual(set(training.status), {'success'})
            self.assertTrue(training.message.str.contains('SYNTHETIC').all())
            fixed = ['hub_beta_1_deg_offset', 'hub_beta_2_deg_offset', 'shroud_beta_1_deg_offset',
                     'shroud_beta_4_deg_offset', 'shroud_theta_deg_offset']
            np.testing.assert_array_equal(training[fixed], np.zeros((18, 5)))
            diagnostics = pd.read_csv(root / 'first' / 'active_learning_diagnostics.csv')
            self.assertEqual(diagnostics.candidate_role.tolist(), ['ehvi', 'uncertainty', 'diversity'] * 2)
            self.assertTrue(np.isfinite(diagnostics[['pred_Efficiency', 'pred_MassFlow']]).all().all())
            saved = (root / 'first' / 'training_data.csv').read_bytes()
            with self.assertRaises(FileExistsError):
                runner['run_baseline'](root / 'first')
            self.assertEqual((root / 'first' / 'training_data.csv').read_bytes(), saved)

    def test_comparison_detects_changed_selection_objective_front_and_diagnostics(self):
        # Tolerances must allow numerical noise but reject actual optimizer/output regressions.
        runner = self.runner()
        checker = Path('benchmarks/compare_offline_baseline.py')
        self.assertTrue(checker.is_file(), 'reference comparison script is missing')
        compare = runpy.run_path(str(checker))['compare_outputs']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'original'
            runner['run_baseline'](original)
            compare(original)
            for filename, column, value in [
                ('training_data.csv', 'Efficiency', .1),
                ('training_data.csv', 'hub_beta_0_deg_offset', 100.),
                ('training_data.csv', 'Efficiency', np.nan),
                ('pareto_front.csv', 'run_id', 'case_999999'),
                ('active_learning_diagnostics.csv', 'candidate_role', 'wrong'),
                ('active_learning_diagnostics.csv', 'pred_Efficiency', np.nan),
            ]:
                with self.subTest(file=filename, column=column, value=value):
                    changed = root / 'changed'
                    changed.mkdir()
                    for source in original.glob('*.csv'):
                        shutil.copy2(source, changed / source.name)
                    frame = pd.read_csv(changed / filename)
                    frame.loc[0, column] = value
                    frame.to_csv(changed / filename, index=False)
                    with self.assertRaises(ValueError):
                        compare(changed)
                    shutil.rmtree(changed)
            reference = json.loads(Path('benchmarks/offline_baseline/reference.json').read_text())
            training = pd.read_csv(original / 'training_data.csv')
            training.loc[0, 'Efficiency'] += reference['tolerances']['absolute'] / 2
            training.to_csv(original / 'training_data.csv', index=False)
            compare(original)


if __name__ == '__main__':
    unittest.main()
