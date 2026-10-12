"""Manual review of results that missed the strict RMS target by a small margin."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import blade_shape_active_learning as b
import blade_shape_refinement as r
import blade_shape_review as review
from blade_shape_cfx_runner import CfxResult
from blade_shape_runtime import atomic_json, file_identity
from blade_shape_signature import physical_signature
from blade_gui import commands
from tests import test_refinement
from tests.test_refinement import fake_geometry

METRICS = {'Efficiency': .90, 'MassFlow': 5.0, 'PressureRatio': 2., 'Power': 1., 'totalpressureratio': 2.1}
EVIDENCE = {'equations_above_target': {'p-mass': 1.3e-5}, 'rms_target': 1e-5, 'rms_limit': 1e-4,
            'max_equations_above_target': 3}


def receipt(case: Path) -> None:
    """The CFX receipt a solve leaves when it hands its result to a reviewer."""
    case.mkdir(parents=True, exist_ok=True)
    (case / 'Impeller_002.out').write_text('synthetic out')
    (case / 'Impeller_002.res').write_text('synthetic res')
    atomic_json(case / 'cfx_state.json', {'version': 2, 'stages': {'solve': {
        'status': 'review', 'exit_code': 0, 'review': EVIDENCE,
        'out_file': 'Impeller_002.out', 'out_identity': file_identity(case / 'Impeller_002.out'),
        'result_file': 'Impeller_002.res', 'result_identity': file_identity(case / 'Impeller_002.res'),
        'convergence': {'rms': {'p-mass': 1.3e-5}, 'wall_notices': [], 'iteration': 4000, 'run_iteration': 2000}}}})


class ReviewDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.c = json.loads(Path('blade_shape_config.json').read_text())
        self.c['paths']['output_dir'] = str(self.out)
        x = r.enforce_fixed(self.c, np.zeros(12))
        for i in range(4):
            b.append_row(self.c, {**b.vector_to_sample(self.c, x + .1 * i), 'run_id': f'case_{i:06d}',
                                  'status': 'success', 'sample_phase': 'active_learning',
                                  'Efficiency': .76 + .001 * i, 'MassFlow': 4.2 + .01 * i})
        # dominates every success, so it would lead the front if it were a label
        b.append_row(self.c, {**b.vector_to_sample(self.c, x + .5), 'run_id': 'case_000004', 'status': 'review',
                              'sample_phase': 'active_learning', 'message': 'Awaiting manual review', **METRICS})
        receipt(self.out / 'cases' / 'case_000004')

    def front(self):
        return set(b.write_pareto(self.c, b.load_training(self.c)).run_id)

    def test_review_row_is_not_a_label_until_accepted(self):
        self.assertNotIn('case_000004', self.front())
        clean, _ = r.training_partition(self.c, b.load_training(self.c))
        self.assertNotIn('case_000004', set(clean.run_id))
        listed = review.awaiting(self.c)
        self.assertEqual([item['run_id'] for item in listed], ['case_000004'])
        self.assertEqual(listed[0]['equations_above_target'], {'p-mass': 1.3e-5})

        record = review.decide(self.c, 'case_000004', 'accepted', 'P-Mass flat at 1.3e-5; monitors steady')
        row = b.load_training(self.c).set_index('run_id').loc['case_000004']
        self.assertEqual((row.status, row.review_decision), ('success', 'accepted'))
        self.assertEqual(row.review_reason, 'P-Mass flat at 1.3e-5; monitors steady')
        self.assertEqual(self.front(), {'case_000004'})
        self.assertIn('case_000004', set(pd.read_csv(self.out / 'pareto_front.csv').run_id))
        registry = json.loads(review.decisions_path(self.c).read_text())['decisions']
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry[0]['out_identity'], record['out_identity'])
        self.assertEqual(registry[0]['objectives']['Efficiency'], .90)
        self.assertEqual(review.awaiting(self.c), [])
        with self.assertRaisesRegex(ValueError, 'not awaiting review'):
            review.decide(self.c, 'case_000004', 'rejected', 'second thoughts')

    def test_rejected_result_is_recorded_as_failed(self):
        review.decide(self.c, 'case_000004', 'rejected', 'outlet backflow too large')
        row = b.load_training(self.c).set_index('run_id').loc['case_000004']
        self.assertEqual((row.status, row.failure_stage), ('failed', 'review_rejected'))
        self.assertNotIn('case_000004', self.front())

    def test_decision_needs_unchanged_evidence_a_reason_and_a_review_row(self):
        with self.assertRaisesRegex(ValueError, 'reason'):
            review.decide(self.c, 'case_000004', 'accepted', '  ')
        with self.assertRaisesRegex(ValueError, 'not awaiting review'):
            review.decide(self.c, 'case_000001', 'accepted', 'x')
        (self.out / 'cases' / 'case_000004' / 'Impeller_002.out').write_text('edited')
        with self.assertRaisesRegex(ValueError, 'changed evidence'):
            review.decide(self.c, 'case_000004', 'accepted', 'x')
        self.assertEqual(b.load_training(self.c).set_index('run_id').loc['case_000004'].status, 'review')

    def test_a_recorded_decision_is_applied_after_an_interruption_never_contradicted(self):
        solve = json.loads((self.out / 'cases' / 'case_000004' / 'cfx_state.json').read_text())['stages']['solve']
        atomic_json(review.decisions_path(self.c), {'decisions': [{
            'run_id': 'case_000004', 'decision': 'accepted', 'reason': 'first', 'decided_at': 'earlier',
            'out_identity': solve['out_identity']}]})
        with self.assertRaisesRegex(ValueError, 'that decision stands'):
            review.decide(self.c, 'case_000004', 'rejected', 'second')
        review.decide(self.c, 'case_000004', 'accepted', 'again')
        row = b.load_training(self.c).set_index('run_id').loc['case_000004']
        self.assertEqual((row.status, row.review_reason), ('success', 'first'))
        self.assertEqual(len(json.loads(review.decisions_path(self.c).read_text())['decisions']), 1)

    def test_a_decision_interrupted_after_the_row_was_rewritten_can_be_finished(self):
        diagnostics = b.diagnostics_csv_path(self.c)
        pd.DataFrame([{'run_id': 'case_000004', 'status': 'review', 'pred_Efficiency': .8}]).to_csv(diagnostics, index=False)
        with patch.object(b, 'write_pareto', side_effect=KeyboardInterrupt('interrupted')):
            with self.assertRaises(KeyboardInterrupt):
                review.decide(self.c, 'case_000004', 'accepted', 'flat P-Mass')
        self.assertEqual(b.load_training(self.c).set_index('run_id').loc['case_000004'].status, 'success')
        self.assertNotIn('case_000004', set(pd.read_csv(self.out / 'pareto_front.csv').run_id)
                         if (self.out / 'pareto_front.csv').exists() else set())
        with self.assertRaisesRegex(ValueError, 'not awaiting review'):
            review.decide(self.c, 'case_000004', 'rejected', 'changed my mind')
        review.decide(self.c, 'case_000004', 'accepted', 'repeat')
        self.assertIn('case_000004', set(pd.read_csv(self.out / 'pareto_front.csv').run_id))
        self.assertEqual(pd.read_csv(diagnostics).status.tolist(), ['success'])
        row = b.load_training(self.c).set_index('run_id').loc['case_000004']
        self.assertEqual(row.review_reason, 'flat P-Mass')
        self.assertEqual(row.message, 'Manually accepted after review: flat P-Mass (RMS above 1e-05 on p-mass 1.3e-05)')
        self.assertEqual(len(json.loads(review.decisions_path(self.c).read_text())['decisions']), 1)

    def test_cli_lists_and_decides(self):
        config_path = self.out / 'config.json'
        config_path.write_text(json.dumps(self.c))
        parser = b.build_parser()
        with redirect_stdout(StringIO()) as listed:
            b.review_command(parser.parse_args(['review', '--config', str(config_path)]))
        self.assertEqual([item['run_id'] for item in json.loads(listed.getvalue())], ['case_000004'])
        spec = commands.build_review(config_path, run_id='case_000004', decision='accept', reason='ok')
        with redirect_stdout(StringIO()):
            b.review_command(parser.parse_args(spec.args))
        self.assertEqual(b.load_training(self.c).set_index('run_id').loc['case_000004'].status, 'success')
        with self.assertRaises(ValueError):
            commands.build_review(config_path, run_id='case_000004', decision='accept', reason='')
        self.assertEqual(commands.REVIEW_EXIT, b.REVIEW_EXIT)


class ReviewInLoopsTests(unittest.TestCase):
    """A review outcome in the active-learning loop and in a boundary stage."""

    # the refinement fixture, without re-running its tests here
    setUp = test_refinement.RefinementTests.setUp
    save_row = test_refinement.RefinementTests.save_row

    def outcome(self, review_first=True):
        calls = []

        def run(working_dir, run_id, **_):
            calls.append(run_id)
            if review_first and len(calls) == 1:
                receipt(Path(working_dir))
                return CfxResult(False, dict(METRICS), 'Awaiting manual review', 'review', review=True)
            return CfxResult(True, {**METRICS, 'Efficiency': .762}, 'Success')
        return run, calls

    def test_boundary_stage_pauses_until_the_review_is_accepted(self):
        self.save_row()
        path = self.root / 'plan.json'
        run, calls = self.outcome()
        with patch.object(b, 'extract_baseline', return_value=self.baseline), \
                patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=run):
            r.write_boundary_plan(self.config, 'case_000000', path)
            paused = r.run_boundary(self.config, path, 'singles', 5, False)
            self.assertEqual((paused['stage_status'], paused['new_cfd_attempts']), ('awaiting_review', 1))
            waiting = [p['run_id'] for p in paused['points'].values() if p.get('status') == 'review']
            # still paused: no new CFD while the decision is open
            again = r.run_boundary(self.config, path, 'singles', 5, True)
            self.assertEqual((again['stage_status'], again['new_cfd_attempts']), ('awaiting_review', 0))
            with self.assertRaisesRegex(ValueError, 'Awaiting manual review'):
                r.run_boundary(self.config, path, 'pairs', 5, True)
            review.decide(self.config, waiting[0], 'accepted', 'close enough')
            resumed = r.run_boundary(self.config, path, 'singles', 5, True)
        self.assertEqual(resumed['stage_status'], 'completed')
        self.assertEqual(len(calls), 1 + resumed['new_cfd_attempts'])

    def test_boundary_stage_fails_once_the_review_is_rejected(self):
        self.save_row()
        path = self.root / 'plan.json'
        run, _ = self.outcome()
        with patch.object(b, 'extract_baseline', return_value=self.baseline), \
                patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=run):
            r.write_boundary_plan(self.config, 'case_000000', path)
            paused = r.run_boundary(self.config, path, 'singles', 5, False)
            waiting = [p['run_id'] for p in paused['points'].values() if p.get('status') == 'review']
            review.decide(self.config, waiting[0], 'rejected', 'diverging')
            with self.assertRaisesRegex(ValueError, 'no automatic CFD retry'):
                r.run_boundary(self.config, path, 'singles', 5, True)

    def test_active_learning_reports_results_awaiting_review_without_failing(self):
        for i in range(4):
            x = self.center.copy()
            x[0] += .1 * i
            self.save_row(i, x, Efficiency=.76 + .001 * i)
        self.config['runtime'].update(initial_samples=0, nsga2_pop_size=6, nsga2_generations=1, candidate_pool_size=8)
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(self.config))
        args = b.build_parser().parse_args(['run', '--config', str(config_path), '--resume', '--iterations', '1',
                                            '--batch-size', '1', '--max-new-cfd', '1'])
        run, _ = self.outcome()
        with patch.object(b, 'extract_baseline', return_value=self.baseline), \
                patch.object(b, 'check_geometry_script'), \
                patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=run), \
                redirect_stdout(StringIO()) as printed:
            b.run_loop(args)  # a review is not a failure: no SystemExit
        self.assertIn('Awaiting review: 1', printed.getvalue())
        training = b.load_training(self.config)
        self.assertEqual(training.status.tolist().count('review'), 1)
        queue = json.loads((self.root / 'pending_evaluations.json').read_text())
        self.assertEqual([e['status'] for e in queue['entries']], ['review'])
        self.assertEqual(physical_signature(self.config), training.loc[training.status.eq('review')].iloc[0].physical_signature)


if __name__ == '__main__':
    unittest.main()
