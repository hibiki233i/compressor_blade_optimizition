"""Legacy recovery is checked at real durable stage boundaries, through resume.

The CFX runner writes every receipt. Only external programs and the simulated
old process exit are substituted; expected transitions come from the recovery
contract, rather than from the registration predicate.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import numpy as np

import blade_shape_active_learning as b
import blade_shape_cfx_runner as cfx
import blade_shape_interrupt as interruption
import blade_shape_pending as pending
import blade_shape_recovery as recovery
import blade_shape_refinement as refinement
import blade_shape_review as reviews
import blade_shape_signature as signatures
from blade_shape_runtime import atomic_json, digest, file_identity, reserve_case
from tests.synthetic_pipeline import fake_geometry
from tests import test_resume_state_machine as resume_tests


class LegacyExit(Exception):
    """An old process exited without writing a modern stop/attempt receipt."""


class LegacyRecoveryStateMachineTests(unittest.TestCase):
    setUp = resume_tests.ResumeStateMachineTests.setUp
    study = resume_tests.ResumeStateMachineTests.study
    external = resume_tests.ResumeStateMachineTests.external
    enqueue = resume_tests.ResumeStateMachineTests.enqueue
    save_row = resume_tests.ResumeStateMachineTests.save_row
    boundary_plan = resume_tests.ResumeStateMachineTests.boundary_plan

    def legacy_case(self, stage='post', status='running', outcome='success', *, row=True, boundary=False):
        if boundary:
            path, plan = self.boundary_plan()
            point = plan['points'][0]
            run_id, x = 'case_000001', np.array(point['x'])
            metadata = dict(sample_phase='boundary', experiment_id=plan['plan_id'],
                            design_role=point['point_id'], selection_source='predefined_boundary')
            reserve_case(self.root, run_id, dict(plan_id=plan['plan_id'], point_id=point['point_id'],
                                                x=x.tolist(), signature=plan['signature']))
            atomic_json(path.with_suffix('.state.json'), dict(plan_id=plan['plan_id'], points={
                point['point_id']: dict(run_id=run_id, status='running')}))
            self.boundary = path
        else:
            entry = self.enqueue([self.center])[0]
            run_id, x, metadata = entry['run_id'], self.center, entry['arguments']
        case = self.root / 'cases' / run_id

        def save_then_exit(path, value):
            atomic_json(path, value)
            current = (value if Path(path).name == 'geometry_state.json' and stage == 'geometry'
                       else value.get('stages', {}).get(stage, {}) if Path(path).name == 'cfx_state.json'
                       else {})
            if current.get('status') == status:
                raise LegacyExit(f'old process exited after {stage} {status}')

        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(cfx, '_run_logged', side_effect=self.external(outcome)), \
                patch.object(b, 'atomic_json', side_effect=save_then_exit), \
                patch.object(cfx, 'atomic_json', side_effect=save_then_exit):
            with self.assertRaises(LegacyExit):
                b._evaluate_true_cfd(self.config, self.baseline, x, b.parse_case_number(run_id), **metadata)
        self.assertFalse((case / interruption.ATTEMPT).exists())
        if row:
            b.append_row(self.config, {**b.vector_to_sample(self.config, x), **metadata, 'run_id': run_id,
                                      'status': 'failed', 'failure_stage': 'solve' if stage == 'solve_restart' else stage,
                                      'case_dir': str(case), 'message': 'legacy incomplete stage',
                                      'historical_note': 'preserve this column'})
            if not boundary:
                queue = pending.load(self.config)
                queue['entries'][0].update(status='failed', finalized=True)
                atomic_json(pending.path_for(self.config), queue)
        self.tool_calls.clear()
        return case

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*')
                if path.is_file() and path.name != '.optimizer.lock'}

    def refuse_unchanged(self, case):
        before = self.snapshot()
        with patch.object(cfx, '_run_logged', side_effect=AssertionError('registration launched a tool')):
            with self.assertRaises((ValueError, RuntimeError)):
                recovery.retry_interrupted(self.config, case.name, 'confirmed old operator stop')
        self.assertEqual(self.snapshot(), before)

    def register(self, case):
        try:
            return recovery.retry_interrupted(self.config, case.name, 'confirmed old operator stop')
        except ValueError as exc:
            self.fail(f'A genuine legacy interruption was refused: {exc}')

    def resume(self, budget, outcome='success'):
        if hasattr(self, 'boundary'):
            with patch.object(b, 'extract_baseline', return_value=self.baseline):
                return refinement.run_boundary(self.config, self.boundary, 'singles', budget, True)['new_cfd_attempts']
        return pending.run(self.config, self.baseline, budget)['new_attempts']

    def register_and_resume(self, case, scope, outcome='success'):
        kept = {path.name: path.read_bytes() for path in case.iterdir() if path.is_file()
                and (path.suffix in {'.res', '.out', '.def', '.gtm', '.ccl'}
                     or path.name in {'candidate.json', 'geometry_state.json'})}
        old_post = (case / 'CFX_Results.txt').read_bytes() if (case / 'CFX_Results.txt').exists() else None
        with patch.object(cfx, '_run_logged', side_effect=AssertionError('registration launched a tool')):
            self.register(case)
        self.assertFalse((case / 'attempts').exists())
        with patch.object(b, 'run_geometry', side_effect=AssertionError('zero budget launched geometry')), \
                patch.object(cfx, '_run_logged', side_effect=AssertionError('zero budget launched CFX')):
            self.assertEqual(self.resume(0), 0)
        self.assertFalse((case / 'attempts').exists())
        with patch.object(b, 'run_geometry', side_effect=(
                AssertionError('Post recovery recomputed geometry') if scope == 'post' else fake_geometry)), \
                patch.object(cfx, '_run_logged', side_effect=self.external(outcome)):
            self.assertEqual(self.resume(1, outcome), 1)
        calls = [name for run, name in self.tool_calls if run == case.name]
        expected = ['cfx5post.exe'] if scope == 'post' else ['cfx5pre.exe', 'cfx5solve.exe'] + (
            ['cfx5solve.exe'] if outcome == 'review' else []) + ['cfx5post.exe']
        self.assertEqual(calls, expected)
        frame = b.load_training(self.config).loc[lambda rows: rows.run_id.eq(case.name)]
        self.assertEqual(frame.status.tolist(), [outcome])
        if 'historical_note' in frame:
            self.assertEqual(frame.iloc[0].historical_note, 'preserve this column')
        journal = json.loads((case / interruption.RETRY).read_text())
        self.assertEqual([attempt['scope'] for attempt in journal['attempts']], [scope])
        if scope == 'post':
            self.assertEqual(kept, {name: (case / name).read_bytes() for name in kept})
            if old_post is not None:
                self.assertEqual((case / 'attempts/interrupted_0001/CFX_Results.txt').read_bytes(), old_post)
        if outcome == 'review':
            self.assertFalse(b.write_pareto(self.config, b.load_training(self.config)).run_id.eq(case.name).any())
        with patch.object(b, 'run_geometry', side_effect=AssertionError('completed point was retried')), \
                patch.object(cfx, '_run_logged', side_effect=AssertionError('completed point was retried')):
            self.assertEqual(self.resume(0), 0)

    def test_real_stage_boundaries_register_and_resume_with_the_expected_scope(self):
        cases = [('geometry', 'running', 'success', 'all'),
                 ('pre', 'running', 'success', 'all'), ('pre', 'returned', 'success', 'all'),
                 ('solve', 'running', 'success', 'all'),
                 ('solve', 'needs_restart', 'review', 'all'), ('solve_restart', 'running', 'review', 'all'),
                 ('post', 'running', 'success', 'post'), ('post', 'returned', 'success', 'post'),
                 ('post', 'running', 'review', 'post'), ('post', 'returned', 'review', 'post')]
        for stage, status, outcome, scope in cases:
            with self.subTest(stage=stage, status=status, outcome=outcome):
                self.study(f'{stage}_{status}_{outcome}')
                case = self.legacy_case(stage, status, outcome)
                self.register_and_resume(case, scope, outcome)

    def test_post_without_csv_recovers_ordinary_and_boundary_owners(self):
        for boundary, row in ((False, False), (True, False), (True, True)):
            with self.subTest(boundary=boundary, row=row):
                self.study(f'post_owner_{boundary}_{row}')
                case = self.legacy_case(row=row, boundary=boundary)
                self.register_and_resume(case, 'post')

    def test_finished_solver_without_acceptance_and_complete_post_are_not_interruptions(self):
        for stage, outcome in [('solve', 'success'), ('solve_restart', 'review'), ('post', 'success')]:
            with self.subTest(stage=stage):
                self.study(f'finished_{stage}')
                case = self.legacy_case(stage, 'complete' if stage == 'post' else 'returned', outcome)
                self.refuse_unchanged(case)

    def test_post_cannot_override_a_different_recorded_failure_or_human_decision(self):
        for stage in ('geometry', 'pre', 'solve', 'cfx', 'review_rejected', 'geometry_rules', 'environment'):
            with self.subTest(stage=stage):
                self.study(f'wrong_failure_{stage}')
                case = self.legacy_case()
                reviews._set(b.training_csv_path(self.config), case.name, {'failure_stage': stage})
                self.refuse_unchanged(case)

    def test_post_needs_accepted_intact_final_solver_evidence(self):
        for damaged in ('result', 'out', 'definition', 'mesh', 'controls', 'restart_controls',
                        'failed_post', 'nonzero_post', 'failed_solve', 'last_attempt', 'live_process'):
            with self.subTest(damaged=damaged):
                self.study(f'bad_evidence_{damaged}')
                case = self.legacy_case(outcome='review')
                state_path = case / 'cfx_state.json'
                state = json.loads(state_path.read_text())
                solve = state['stages']['solve']
                files = dict(result=solve['result_file'], out=solve['out_file'], definition='Impeller.def',
                             mesh='Impeller_Mesh.gtm', controls='convergence.ccl', restart_controls='restart_convergence.ccl')
                if damaged in files:
                    (case / files[damaged]).write_text('changed after the legacy stop')
                elif damaged == 'failed_post':
                    state['stages']['post'].update(status='failed', exit_code=1)
                elif damaged == 'nonzero_post':
                    state['stages']['post'].update(status='returned', exit_code=1)
                elif damaged == 'failed_solve':
                    solve.update(status='failed')
                elif damaged == 'last_attempt':
                    state['stages']['solve_restart'].update(status='running', exit_code=None)
                else:
                    atomic_json(case / 'active_process.json', dict(status='running', pid=os.getpid()))
                atomic_json(state_path, state)
                self.refuse_unchanged(case)

    def test_post_registration_pins_the_completed_solver_files_until_resume(self):
        for name in ('Impeller_001.res', 'Impeller_001.out', 'Impeller.def', 'convergence.ccl'):
            with self.subTest(name=name):
                self.study(f'changed_after_registration_{name}')
                case = self.legacy_case()
                self.register(case)
                (case / name).write_text('changed after registration')
                before = self.snapshot()
                with patch.object(b, 'run_geometry', side_effect=AssertionError('conflict recomputed geometry')), \
                        patch.object(cfx, '_run_logged', side_effect=AssertionError('conflict launched CFX')):
                    with self.assertRaisesRegex(ValueError, 'Evidence confirmed'):
                        self.resume(1)
                self.assertEqual(self.snapshot(), before)

    def test_post_registration_checks_completed_geometry_and_cfx_input_receipts(self):
        for damaged in ('geometry_signature', 'cfx_inputs'):
            with self.subTest(damaged=damaged):
                self.study(damaged)
                case = self.legacy_case()
                path = case / ('geometry_state.json' if damaged == 'geometry_signature' else 'cfx_state.json')
                state = json.loads(path.read_text())
                if damaged == 'geometry_signature':
                    state['signature'] = 'conflicting completed geometry identity'
                else:
                    state['inputs']['p_out_pa'] += 1
                atomic_json(path, state)
                self.refuse_unchanged(case)

    def test_post_archive_replays_after_a_move_is_interrupted(self):
        self.study('post_partial_archive')
        case = self.legacy_case(status='returned')
        self.register(case)
        move = shutil.move
        def move_then_stop(source, target):
            move(source, target)
            raise KeyboardInterrupt('exit after one archive move')
        with patch.object(interruption.shutil, 'move', side_effect=move_then_stop), \
                patch.object(cfx, '_run_logged', side_effect=AssertionError('archive started CFD')):
            with self.assertRaises(KeyboardInterrupt):
                self.resume(1)
        with patch.object(b, 'run_geometry', side_effect=AssertionError('archive replay recomputed geometry')), \
                patch.object(cfx, '_run_logged', side_effect=self.external()):
            self.assertEqual(self.resume(1), 1)
        self.assertEqual([name for _, name in self.tool_calls], ['cfx5post.exe'])
        journal = json.loads((case / interruption.RETRY).read_text())
        self.assertEqual([(a['scope'], a['status']) for a in journal['attempts']], [('post', 'archived')])
        self.assertEqual(b.load_training(self.config).status.tolist(), ['success'])

    def test_accepted_physical_change_preserves_the_old_signature_and_recomputes(self):
        self.study('accepted_change')
        case = self.legacy_case()
        old_signature = b.load_training(self.config).iloc[0].physical_signature
        self.config['runtime']['rpm'] += 100
        signatures.check(self.config, {old_signature: [case.name]}, 'accepted operating-point change')
        self.assertNotEqual(old_signature, signatures.physical_signature(self.config))
        self.register_and_resume(case, 'all')
        original = json.loads((case / 'attempts/interrupted_0001' / interruption.ATTEMPT).read_text())
        self.assertEqual(original['signature'], old_signature)
        self.assertEqual(b.load_training(self.config).iloc[0].physical_signature,
                         signatures.physical_signature(self.config))

    def use_legacy_signature(self, case):
        legacy = signatures.legacy_physical_signature(self.config)
        queue = pending.load(self.config)
        queue['entries'][0]['signature'] = legacy
        atomic_json(pending.path_for(self.config), queue)
        reviews._set(b.training_csv_path(self.config), case.name, {signatures.COLUMN: legacy})
        path = case / 'geometry_state.json'
        geometry = json.loads(path.read_text())
        geometry['signature'] = digest({'physical': legacy, 'x': self.center.tolist(),
                                        'candidate': file_identity(case / 'candidate.json')})
        atomic_json(path, geometry)
        self.assertIn(legacy, signatures.compatible(self.config))
        self.assertEqual(signatures.load_registry(self.config)['accepted'], [])
        return legacy

    def test_unchanged_legacy_signature_keeps_solve_and_only_retries_post(self):
        self.study('unchanged_legacy_signature')
        case = self.legacy_case()
        self.use_legacy_signature(case)
        self.register_and_resume(case, 'post')

    def test_unchanged_legacy_signature_still_checks_completed_inputs(self):
        for damaged in ('geometry_signature', 'cfx_inputs'):
            with self.subTest(damaged=damaged):
                self.study(f'legacy_signature_{damaged}')
                case = self.legacy_case()
                self.use_legacy_signature(case)
                path = case / ('geometry_state.json' if damaged == 'geometry_signature' else 'cfx_state.json')
                state = json.loads(path.read_text())
                if damaged == 'geometry_signature':
                    state['signature'] = 'conflicting completed geometry identity'
                else:
                    state['inputs']['p_out_pa'] += 1
                atomic_json(path, state)
                self.refuse_unchanged(case)

    def test_accepted_change_during_post_archive_upgrades_and_replays_the_full_archive(self):
        self.study('accepted_change_during_archive')
        case = self.legacy_case(status='returned')
        old_signature = b.load_training(self.config).iloc[0].physical_signature
        self.register(case)
        move = shutil.move
        def move_then_stop(source, target):
            move(source, target)
            raise KeyboardInterrupt('exit after one archive move')
        with patch.object(interruption.shutil, 'move', side_effect=move_then_stop):
            with self.assertRaises(KeyboardInterrupt):
                self.resume(1)
        self.config['runtime']['rpm'] += 100
        signatures.check(self.config, {old_signature: [case.name]}, 'accepted change during archive')
        # Scope upgrade itself must also survive interruption before all moves finish.
        with patch.object(interruption.shutil, 'move', side_effect=move_then_stop):
            with self.assertRaises(KeyboardInterrupt):
                self.resume(1)
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(cfx, '_run_logged', side_effect=self.external()):
            self.assertEqual(self.resume(1), 1)
        self.assertEqual([name for _, name in self.tool_calls], ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe'])
        journal = json.loads((case / interruption.RETRY).read_text())
        self.assertEqual([(a['scope'], a['status']) for a in journal['attempts']], [('all', 'archived')])
        archive = case / 'attempts/interrupted_0001'
        self.assertTrue((archive / 'Impeller_001.res').exists())
        self.assertEqual(json.loads((archive / interruption.ATTEMPT).read_text())['signature'], old_signature)
        frame = b.load_training(self.config)
        self.assertEqual(frame.status.tolist(), ['success'])
        self.assertEqual(frame.physical_signature.tolist(), [signatures.physical_signature(self.config)])

    def stop_after_archive_commit(self):
        def save_then_stop(path, value):
            atomic_json(path, value)
            if Path(path).name == interruption.RETRY and value['attempts'][-1]['status'] == 'archived':
                raise KeyboardInterrupt('archive committed before replacement attempt')
        with patch.object(interruption, 'atomic_json', side_effect=save_then_stop):
            with self.assertRaises(KeyboardInterrupt):
                self.resume(1)

    def test_completed_archive_handoff_keeps_original_inputs_until_the_new_attempt_is_durable(self):
        for mode in ('post_unchanged', 'post_changed', 'all_changed'):
            with self.subTest(mode=mode):
                self.study(f'completed_archive_{mode}')
                case = self.legacy_case()
                old_signature = b.load_training(self.config).iloc[0].physical_signature
                self.register(case)
                if mode == 'all_changed':
                    self.config['runtime']['rpm'] += 100
                    signatures.check(self.config, {old_signature: [case.name]}, 'change before archiving')
                self.stop_after_archive_commit()
                self.assertFalse((case / interruption.ATTEMPT).exists())
                if mode != 'post_unchanged':
                    self.config['runtime']['rpm'] += 100
                    signatures.check(self.config, {old_signature: [case.name]}, 'change after archive commit')
                with patch.object(b, 'run_geometry', side_effect=(
                        AssertionError('unchanged handoff recomputed geometry') if mode == 'post_unchanged' else fake_geometry)), \
                        patch.object(cfx, '_run_logged', side_effect=self.external()):
                    self.assertEqual(self.resume(1), 1)
                expected = ['cfx5post.exe'] if mode == 'post_unchanged' else ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe']
                self.assertEqual([name for _, name in self.tool_calls], expected)
                journal = json.loads((case / interruption.RETRY).read_text())
                self.assertEqual([(a['scope'], a['status']) for a in journal['attempts']],
                                 [('post' if mode == 'post_unchanged' else 'all', 'archived')])
                self.assertEqual(b.load_training(self.config).status.tolist(), ['success'])

    def test_completed_archive_handoff_still_checks_registered_solver_files(self):
        self.study('completed_archive_changed_evidence')
        case = self.legacy_case()
        self.register(case)
        self.stop_after_archive_commit()
        (case / 'Impeller_001.res').write_text('changed after completed Post archive')
        before = self.snapshot()
        with patch.object(b, 'run_geometry', side_effect=AssertionError('handoff ignored conflicting evidence')), \
                patch.object(cfx, '_run_logged', side_effect=AssertionError('handoff launched CFX')):
            with self.assertRaisesRegex(ValueError, 'Evidence confirmed'):
                self.resume(1)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
