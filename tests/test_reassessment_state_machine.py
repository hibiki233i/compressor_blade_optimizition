"""Exercise recovery boundaries with real receipts and substituted CFX commands."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import blade_shape_active_learning as b
import blade_shape_cfx_runner as cfx
import blade_shape_convergence as convergence
import blade_shape_interrupt as interruption
import blade_shape_pending as pending
import blade_shape_recovery as recovery
import blade_shape_refinement as refinement
import blade_shape_review as review
from blade_shape_runtime import atomic_json, file_identity
from tests.cfx_output_samples import output


class ReassessmentStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def failed_case(self, mode='policy'):
        self.config = json.loads(Path('blade_shape_config.json').read_text())
        root = self.root / mode
        root.mkdir()
        paths = self.config['paths']
        paths.update(output_dir=str(root), cfx_bin_dir=str(root / 'bin'),
                     template_cfx=str(root / 'template.cfx'), template_cse=str(root / 'extract.cse'))
        (root / 'bin').mkdir()
        for name in ('cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe'):
            (root / 'bin' / name).write_text('substitute')
        for key in ('template_cfx', 'template_cse'):
            Path(paths[key]).write_text('synthetic template')
        self.case = root / 'cases' / 'case_000000'
        self.case.mkdir(parents=True)
        (self.case / 'Impeller_Mesh.gtm').write_text('mesh')
        self.x = refinement.enforce_fixed(self.config, np.zeros(len(b.variable_names(self.config))))
        atomic_json(self.case / 'candidate.json', {'run_id': 'case_000000',
                    'variables': b.vector_to_sample(self.config, self.x)})
        kwargs = dict(p_out_pa=float(self.config['runtime']['p_out_pa']),
                      n_blades=int(self.config['runtime']['n_blades']),
                      cfx_bin_dir=paths['cfx_bin_dir'], template_cfx=paths['template_cfx'],
                      template_cse=paths['template_cse'])
        solves = 0

        def external(cmd, cwd, log):
            nonlocal solves
            name = Path(cmd[0]).name
            if name == 'cfx5pre.exe':
                (cwd / 'Impeller.def').write_text('definition')
                return 0
            self.assertEqual(name, 'cfx5solve.exe', 'the old policy must not launch Post')
            solves += 1
            if solves == 2 and mode == 'nonzero':
                return 1
            (cwd / f'Impeller_{solves:03}.res').write_text(f'result {solves}')
            if solves == 2 and mode == 'ambiguous':
                (cwd / 'another.res').write_text('ambiguous result')
            if solves == 2 and mode == 'missing_out':
                return 0
            text = output('2E-6', 2000, True,
                          {key: '3E-5' for key in ('U - Mom', 'V - Mom', 'W - Mom', 'P - Mass')})
            if solves == 2:
                text = text.replace('OUTER LOOP ITERATION = 2000',
                                    'OUTER LOOP ITERATION = 4000 (2000)')
                if mode == 'unparseable_out':
                    text = 'aborted before a residual table was produced'
            (cwd / f'Impeller_{solves:03}.out').write_text(text)
            return 0

        with patch.object(cfx, '_run_logged', side_effect=external):
            result = cfx.run_cfx_pipeline(self.case, 'case_000000',
                                         review=convergence.ReviewPolicy(), **kwargs)
        self.assertFalse(result.success)
        self.assertFalse(result.review)
        self.assertEqual(solves, 2)
        b.append_row(self.config, {**b.vector_to_sample(self.config, self.x),
                     'run_id': 'case_000000', 'status': 'failed', 'sample_phase': 'doe',
                     'failure_stage': result.failure_stage, 'message': result.message})
        self.old_attempt = interruption.begin_case(self.case, 'case_000000',
                                                   b.load_training(self.config).iloc[0].physical_signature,
                                                   self.x.tolist())
        interruption.finish_case(self.case, 'complete')
        return result

    def post(self, cmd, cwd, log):
        self.assertEqual(Path(cmd[0]).name, 'cfx5post.exe', 'reassessment must run only Post')
        (cwd / 'CFX_Results.txt').write_text('.8,2,1,.5,2.1')
        return 0

    def seed_queue(self):
        atomic_json(pending.path_for(self.config), {'version': 1, 'entries': [{
            'run_id': 'case_000000', 'x': self.x.tolist(),
            'signature': b.load_training(self.config).iloc[0].physical_signature,
            'arguments': {'sample_phase': 'doe', 'doe_index': 0},
            'status': 'failed', 'finalized': True}]})

    def interrupt_after_training_promotion(self, plan):
        original = recovery.atomic_json

        def crash_on_queue(path, value):
            if Path(path) == pending.path_for(self.config):
                raise KeyboardInterrupt('stopped after training promotion, before queue commit')
            return original(path, value)

        with patch.object(recovery, 'atomic_json', side_effect=crash_on_queue), \
                patch.object(cfx, '_run_logged', side_effect=self.post):
            with self.assertRaises(KeyboardInterrupt):
                recovery.apply_plan(self.config, plan, 'reconsider numerical policy')
        self.assertEqual(b.load_training(self.config).status.tolist(), ['review'])
        self.assertEqual(pending.load(self.config)['entries'][0]['status'], 'failed')

    def seed_diagnostics(self):
        paths = (b.diagnostics_csv_path(self.config), b.output_dir(self.config) / 'boundary_diagnostics.csv')
        for path in paths:
            pd.DataFrame([
                {'run_id': 'case_000000', 'status': 'failed', 'failure_stage': 'solve',
                 'message': 'original policy failure', 'candidate_role': 'ehvi',
                 'pred_Efficiency': .7, 'pred_MassFlow': 4.8,
                 'std_Efficiency': .1, 'std_MassFlow': .3,
                 'true_Efficiency': np.nan, 'true_MassFlow': np.nan,
                 'prediction_error_Efficiency': np.nan, 'prediction_error_MassFlow': np.nan,
                 'hv_gain': 0., 'delta_best_Efficiency': 0., 'custom_metadata': 'keep prediction'},
                {'run_id': 'case_000000', 'status': 'failed', 'failure_stage': 'solve',
                 'message': 'original policy failure', 'candidate_role': 'ehvi',
                 'pred_Efficiency': .75, 'pred_MassFlow': np.nan,
                 'std_Efficiency': .1, 'std_MassFlow': np.nan,
                 'custom_metadata': 'keep older prediction'},
                {'run_id': 'case_000001', 'status': 'success', 'true_Efficiency': .9,
                 'custom_metadata': 'unrelated result'},
            ]).to_csv(path, index=False)
        return paths

    def assert_reassessed_diagnostics(self, paths, status='review'):
        for path in paths:
            frame = pd.read_csv(path)
            rows = frame.loc[frame.run_id.eq('case_000000')]
            self.assertEqual(rows.status.tolist(), [status, status])
            np.testing.assert_allclose(rows.true_Efficiency, [.8, .8])
            np.testing.assert_allclose(rows.true_MassFlow, [4.5, 4.5])  # .5 per passage, nine blades.
            np.testing.assert_allclose(rows.prediction_error_Efficiency, [.1, .05])
            self.assertAlmostEqual(rows.iloc[0].prediction_error_MassFlow, -.3)
            self.assertTrue(pd.isna(rows.iloc[1].prediction_error_MassFlow))
            np.testing.assert_allclose(rows.pred_Efficiency, [.7, .75])
            np.testing.assert_allclose(rows.std_Efficiency, [.1, .1])
            self.assertEqual(rows.custom_metadata.tolist(), ['keep prediction', 'keep older prediction'])
            self.assertEqual(rows.iloc[0].hv_gain, 0.)
            self.assertEqual(rows.iloc[0].delta_best_Efficiency, 0.)
            self.assertTrue(rows.failure_stage.isna().all())
            self.assertFalse(rows.message.eq('original policy failure').any())
            other = frame.loc[frame.run_id.eq('case_000001')].iloc[0]
            self.assertEqual((other.status, other.true_Efficiency, other.custom_metadata),
                             ('success', .9, 'unrelated result'))

    def test_reassessment_synchronizes_diagnostics_before_the_human_decision(self):
        for decision, status in (('accepted', 'success'), ('rejected', 'failed')):
            with self.subTest(decision=decision):
                self.failed_case('diagnostics_' + decision)
                paths = self.seed_diagnostics()
                plan = recovery.make_plan(self.config, 'case_000000')
                with patch.object(cfx, '_run_logged', side_effect=self.post):
                    recovery.apply_plan(self.config, plan, 'reconsider numerical policy')
                self.assert_reassessed_diagnostics(paths)
                self.assertTrue(b.write_pareto(self.config, b.load_training(self.config)).empty)
                review.decide(self.config, 'case_000000', decision, 'engineering decision')
                self.assert_reassessed_diagnostics(paths, status)
                roles = refinement.role_diagnostics(pd.read_csv(paths[0]))
                self.assertEqual(roles.empty, decision == 'rejected')
                front = b.write_pareto(self.config, b.load_training(self.config))
                self.assertEqual('case_000000' in front.run_id.values, decision == 'accepted')

    def test_reassessment_replays_a_partial_diagnostic_commit_without_another_post(self):
        for stopped_index in (0, 1):
            with self.subTest(stopped_index=stopped_index):
                self.failed_case(f'diagnostic_replay_{stopped_index}')
                paths = self.seed_diagnostics()
                plan = recovery.make_plan(self.config, 'case_000000')
                original = review._set

                def stop_before_diagnostics(path, *args, **kwargs):
                    if path == paths[stopped_index]:
                        raise KeyboardInterrupt('stopped before diagnostics commit')
                    return original(path, *args, **kwargs)

                with patch.object(review, '_set', side_effect=stop_before_diagnostics), \
                        patch.object(cfx, '_run_logged', side_effect=self.post):
                    with self.assertRaises(KeyboardInterrupt):
                        recovery.apply_plan(self.config, plan, 'reconsider numerical policy')
                self.assert_reassessed_diagnostics(paths[:stopped_index])
                self.assertEqual(b.load_training(self.config).status.tolist(), ['failed'])
                with self.assertRaisesRegex(ValueError, 'not awaiting review'):
                    review.decide(self.config, 'case_000000', 'accepted', 'decision before diagnostics complete')
                with patch.object(cfx, '_run_logged', side_effect=AssertionError('completed Post must be reused')):
                    recovery.apply_plan(self.config, plan, 'finish partial diagnostics commit')
                    recovery.apply_plan(self.config, plan, 'idempotent repetition')
                self.assert_reassessed_diagnostics(paths)
                review.decide(self.config, 'case_000000', 'accepted', 'engineering decision')
                self.assert_reassessed_diagnostics(paths, 'success')

    def test_nonzero_restart_cannot_reassess_the_initial_result(self):
        result = self.failed_case('nonzero')
        self.assertIn('exit=1', result.message)
        before = file_identity(b.training_csv_path(self.config))
        with self.assertRaises(ValueError):
            recovery.make_plan(self.config, 'case_000000')
        self.assertEqual(before, file_identity(b.training_csv_path(self.config)))
        self.assertFalse((b.output_dir(self.config) / 'reassessment_requests.json').exists())

    def test_incomplete_restart_outputs_cannot_reassess_the_initial_result(self):
        for mode in ('ambiguous', 'missing_out', 'unparseable_out'):
            with self.subTest(mode=mode):
                self.failed_case(mode)
                before = file_identity(self.case / 'cfx_state.json')
                with self.assertRaises(ValueError):
                    recovery.make_plan(self.config, 'case_000000')
                self.assertEqual(before, file_identity(self.case / 'cfx_state.json'))

    def test_legacy_restart_output_failures_without_reason_code_are_refused(self):
        for mode in ('ambiguous', 'missing_out', 'unparseable_out'):
            with self.subTest(mode=mode):
                self.failed_case(mode)
                path = self.case / 'cfx_state.json'
                state = json.loads(path.read_text())
                # Older receipts retained the initial result and had no reason
                # code distinguishing a restart output error from policy rejection.
                state['stages']['solve'].pop('reason_code', None)
                self.assertEqual(state['stages']['solve_restart']['status'], 'returned')
                self.assertEqual(state['stages']['solve_restart']['exit_code'], 0)
                self.assertEqual(state['solve_attempts'][-1]['attempt'], 0)
                atomic_json(path, state)
                before = file_identity(path)
                with self.assertRaises(ValueError):
                    recovery.make_plan(self.config, 'case_000000')
                self.assertEqual(before, file_identity(path))

    def test_post_ctrl_c_then_replay_uses_a_new_owner_and_preserves_solve(self):
        self.failed_case()
        plan = recovery.make_plan(self.config, 'case_000000')
        before = {path: file_identity(path) for path in
                  (self.case / 'Impeller_002.res', self.case / 'Impeller_002.out',
                   self.case / interruption.ATTEMPT)}

        class Process:
            pid = 2147483647

            def __init__(self, code=None):
                self.returncode = code

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 130
                return self.returncode

            def kill(self):
                self.returncode = 130

        # Exercise the actual run_logged KeyboardInterrupt handler, including
        # its stop receipt, while substituting the external process itself.
        with patch.object(interruption.subprocess, 'Popen', return_value=Process()), \
                patch.object(interruption.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(interruption.time, 'sleep', side_effect=KeyboardInterrupt('operator Ctrl+C')):
            with self.assertRaises(KeyboardInterrupt):
                recovery.apply_plan(self.config, plan, 'reconsider numerical policy')

        def completed_post(command, **kwargs):
            self.assertEqual(Path(command[0]).name, 'cfx5post.exe')
            (self.case / 'CFX_Results.txt').write_text('.8,2,1,.5,2.1')
            return Process(0)

        with patch.object(interruption.subprocess, 'Popen', side_effect=completed_post) as external:
            recovery.apply_plan(self.config, plan, 'retry interrupted Post')
            recovery.apply_plan(self.config, plan, 'idempotent repetition')
        external.assert_called_once()
        self.assertEqual(b.load_training(self.config).status.tolist(), ['review'])
        self.assertFalse(interruption.stopped(self.old_attempt['owner']))
        self.assertFalse((self.case / 'attempts').exists(), 'Post recovery must not archive completed Solve')
        self.assertEqual(before, {path: file_identity(path) for path in before})

    def test_prepared_record_survives_stop_before_solver_receipt_is_rewritten(self):
        self.failed_case()
        plan = recovery.make_plan(self.config, 'case_000000')
        before = file_identity(self.case / 'cfx_state.json')
        original = recovery.atomic_json

        def stop_after_prepare(path, value):
            result = original(path, value)
            if Path(path).name == 'reassessment_requests.json':
                raise KeyboardInterrupt('stopped immediately after durable prepare')
            return result

        with patch.object(recovery, 'atomic_json', side_effect=stop_after_prepare):
            with self.assertRaises(KeyboardInterrupt):
                recovery.apply_plan(self.config, plan, 'reconsider numerical policy')
        self.assertEqual(before, file_identity(self.case / 'cfx_state.json'))
        self.assertEqual(b.load_training(self.config).status.tolist(), ['failed'])
        with patch.object(cfx, '_run_logged', side_effect=self.post) as post:
            recovery.apply_plan(self.config, plan, 'replay prepared request')
        post.assert_called_once()
        self.assertEqual(b.load_training(self.config).status.tolist(), ['review'])
        self.assertFalse((self.case / 'attempts').exists())

    def test_partial_training_promotion_replays_queue_without_another_post(self):
        self.failed_case()
        self.seed_queue()
        plan = recovery.make_plan(self.config, 'case_000000')
        self.interrupt_after_training_promotion(plan)
        with patch.object(cfx, '_run_logged', side_effect=AssertionError('completed Post must be reused')):
            recovery.apply_plan(self.config, plan, 'finish partial commit')
        entry = pending.load(self.config)['entries'][0]
        self.assertEqual((entry['status'], entry['finalized']), ('review', True))
        self.assertEqual(b.load_training(self.config).run_id.tolist(), ['case_000000'])
        registry = json.loads((b.output_dir(self.config) / 'reassessment_requests.json').read_text())
        self.assertEqual(registry['requests'][plan['plan_id']]['status'], 'applied')

    def test_human_rejection_after_partial_promotion_cannot_be_overwritten(self):
        self.failed_case()
        self.seed_queue()
        plan = recovery.make_plan(self.config, 'case_000000')
        self.interrupt_after_training_promotion(plan)
        review.decide(self.config, 'case_000000', 'rejected', 'unstable monitors')
        with patch.object(cfx, '_run_logged', side_effect=AssertionError('a human decision must be final')):
            with self.assertRaises(ValueError):
                recovery.apply_plan(self.config, plan, 'replay after rejection')
        row = b.load_training(self.config).iloc[0]
        self.assertEqual((row.status, row.failure_stage, row.review_decision),
                         ('failed', 'review_rejected', 'rejected'))
        self.assertEqual(pending.load(self.config)['entries'][0]['status'], 'failed')
        self.assertTrue(b.write_pareto(self.config, b.load_training(self.config)).empty)


if __name__ == '__main__':
    unittest.main()
