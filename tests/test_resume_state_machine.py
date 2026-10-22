"""Resume transactions use completed CFD evidence independently of new-work budgets.

All completion receipts are produced by the real CFX runner. Only geometry and
the external executable boundary are substituted; recovery itself is exercised.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import blade_shape_active_learning as b
import blade_shape_cfx_runner as cfx
import blade_shape_interrupt as interruption
import blade_shape_pending as pending
import blade_shape_refinement as refinement
import blade_shape_review as reviews
from blade_shape_runtime import atomic_json, case_reservations, file_identity, reserve_case
from tests.cfx_output_samples import output
from tests.synthetic_pipeline import fake_geometry
from tests import test_refinement


class ResumeStateMachineTests(unittest.TestCase):
    setUp = test_refinement.RefinementTests.setUp
    save_row = test_refinement.RefinementTests.save_row

    def study(self, name: str) -> None:
        """Give each subtest an independent output root and signed input assets."""
        if not hasattr(self, '_base_config'):
            self._base_config = copy.deepcopy(self.config)
            self._suite_root = self.root
        self.config = copy.deepcopy(self._base_config)
        self.root = self._suite_root / name
        self.root.mkdir()
        binary = self.root / 'bin'
        binary.mkdir()
        for executable in ('pre', 'solve', 'post'):
            (binary / f'cfx5{executable}.exe').write_text('synthetic executable substitute')
        paths = self.config['paths']
        paths.update(output_dir=str(self.root), cfx_bin_dir=str(binary),
                     template_cfx=str(self.root / 'template.cfx'),
                     template_cse=str(self.root / 'extract.cse'))
        Path(paths['template_cfx']).write_text('synthetic CFX project')
        Path(paths['template_cse']).write_text('synthetic extraction session')
        self.tool_calls = []

    def external(self, status='success'):
        def invoke(command, case, log):
            name = Path(command[0]).name
            self.tool_calls.append((case.name, name))
            if name == 'cfx5pre.exe':
                (case / 'Impeller.def').write_text('synthetic solver definition')
            elif name == 'cfx5solve.exe':
                attempt = 1 + len(list(case.glob('Impeller_*.res')))
                (case / f'Impeller_{attempt:03d}.res').write_text(f'synthetic result {attempt}')
                if status == 'review':
                    text = output('2E-6', 2000, True, {'W - Mom': '3E-5'})
                    if attempt == 2:
                        text = text.replace('OUTER LOOP ITERATION = 2000',
                                            'OUTER LOOP ITERATION = 4000 (2000)')
                else:
                    text = output()
                (case / f'Impeller_{attempt:03d}.out').write_text(text)
            elif name == 'cfx5post.exe':
                (case / 'CFX_Results.txt').write_text('.8,2,1,.5,2.1')
            else:
                self.fail(f'Unexpected external command: {command}')
            return 0
        return invoke

    def complete_before_csv(self, index, x, status='success', **metadata):
        """Interrupt at the real append boundary after verified Solve and Post."""
        def stop_before_append(config, row):
            self.assertEqual(row['status'], status)
            raise KeyboardInterrupt('simulated stop after durable Post, before CSV')
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(cfx, '_run_logged', side_effect=self.external(status)), \
                patch.object(b, 'append_row', side_effect=stop_before_append):
            with self.assertRaises(KeyboardInterrupt):
                b.evaluate_true_cfd(self.config, self.baseline, x, index, **metadata)
        case = self.root / 'cases' / f'case_{index:06d}'
        self.assertTrue(interruption.completed_result_available(case))
        self.assertEqual(json.loads((case / interruption.ATTEMPT).read_text())['status'], 'complete')
        return case

    def enqueue(self, xs, phase='doe'):
        candidates = [b.SelectedCandidate(x, i + 1, phase) for i, x in enumerate(xs)]
        arguments = [({'sample_phase': phase, 'doe_index': i} if phase == 'doe' else
                      {'sample_phase': phase, 'al_iteration': 0, 'batch_index': i,
                       'selection_rank': i + 1, 'selection_source': 'ehvi'})
                     for i in range(len(xs))]
        pending.enqueue(self.config, candidates, arguments)
        return pending.load(self.config)['entries']

    def boundary_plan(self):
        self.save_row()
        path = self.root / 'plan.json'
        with patch.object(b, 'extract_baseline', return_value=self.baseline):
            plan = refinement.write_boundary_plan(self.config, 'case_000000', path)
        return path, plan

    def reserve_boundary(self, path, plan, point, index, status='success', state=True):
        run_id = f'case_{index:06d}'
        reserve_case(self.root, run_id, {'plan_id': plan['plan_id'], 'point_id': point['point_id'],
                                        'x': point['x'], 'signature': plan['signature']})
        state_path = path.with_suffix('.state.json')
        if state:
            checkpoint = json.loads(state_path.read_text()) if state_path.exists() else {
                'plan_id': plan['plan_id'], 'points': {}}
            checkpoint['points'][point['point_id']] = {'run_id': run_id, 'status': 'running'}
            atomic_json(state_path, checkpoint)
        metadata = {'sample_phase': 'boundary', 'experiment_id': plan['plan_id'],
                    'design_role': point['point_id'], 'selection_source': 'predefined_boundary'}
        cfg = refinement.point_config(self.config, point)
        original = self.config
        self.config = cfg
        try:
            case = self.complete_before_csv(index, np.array(point['x']), status, **metadata)
        finally:
            self.config = original
        return case

    def no_external_work(self):
        """Patches prove recovery cannot silently fall through into new CFD."""
        return patch.object(cfx, '_run_logged', side_effect=AssertionError('recovery launched a tool'))

    def test_pending_zero_budget_restores_completed_success_and_review(self):
        for status in ('success', 'review'):
            with self.subTest(status=status):
                self.study(status)
                entry = self.enqueue([self.center])[0]
                case = self.complete_before_csv(0, self.center, status, **entry['arguments'])
                receipt = file_identity(case / 'cfx_state.json')
                with self.no_external_work(), patch.object(b, 'run_geometry', side_effect=AssertionError('geometry')):
                    result = pending.run(self.config, self.baseline, 0)
                self.assertEqual(result['new_attempts'], 0)
                frame = b.load_training(self.config)
                self.assertEqual(frame.run_id.tolist(), ['case_000000'])
                self.assertEqual(frame.status.tolist(), [status])
                self.assertAlmostEqual(frame.iloc[0].MassFlow, .5 * self.config['runtime']['n_blades'])
                self.assertEqual(pending.load(self.config)['entries'][0]['status'], status)
                self.assertTrue(pending.load(self.config)['entries'][0]['finalized'])
                self.assertEqual(file_identity(case / 'cfx_state.json'), receipt)

    def test_recovery_does_not_consume_budget_for_the_next_new_case(self):
        self.study('budget')
        other = self.center.copy(); other[0] = .2
        entries = self.enqueue([self.center, other])
        self.complete_before_csv(0, self.center, **entries[0]['arguments'])
        self.tool_calls.clear()
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(cfx, '_run_logged', side_effect=self.external()):
            result = pending.run(self.config, self.baseline, 1)
        self.assertEqual(result['new_attempts'], 1)
        self.assertEqual(b.load_training(self.config).run_id.tolist(), ['case_000000', 'case_000001'])
        self.assertEqual({run_id for run_id, _ in self.tool_calls}, {'case_000001'})

    def test_budget_exhausted_pending_entry_does_not_hide_later_recovery(self):
        self.study('scan')
        other = self.center.copy(); other[0] = .2
        entries = self.enqueue([self.center, other])
        self.complete_before_csv(1, other, **entries[1]['arguments'])
        with self.no_external_work():
            result = pending.run(self.config, self.baseline, 0)
        self.assertEqual(result['new_attempts'], 0)
        self.assertEqual(b.load_training(self.config).run_id.tolist(), ['case_000001'])
        saved = pending.load(self.config)['entries']
        self.assertFalse(saved[0]['finalized'])
        self.assertTrue(saved[1]['finalized'])
        self.assertFalse((self.root / 'cases/case_000000').exists())

    def test_repeated_recovery_does_not_duplicate_training_or_diagnostics(self):
        self.study('idempotent')
        self.config['refinement']['local_search'] = {'enabled': True}
        entry = self.enqueue([self.center], 'active_learning')[0]
        self.complete_before_csv(0, self.center, **entry['arguments'])
        with self.no_external_work():
            first = pending.run(self.config, self.baseline, 0)
            second = pending.run(self.config, self.baseline, 0)
        self.assertEqual(first['new_attempts'] + second['new_attempts'], 0)
        self.assertEqual(len(b.load_training(self.config)), 1)
        self.assertEqual(pd.read_csv(b.diagnostics_csv_path(self.config)).run_id.tolist(), ['case_000000'])
        local = json.loads((self.root / 'local_search_state.json').read_text())
        self.assertEqual(local['processed_run_ids'], ['case_000000'])

    def test_boundary_zero_budget_recovers_reserved_success_and_review(self):
        for status in ('success', 'review'):
            with self.subTest(status=status):
                self.study('boundary_' + status)
                path, plan = self.boundary_plan()
                self.reserve_boundary(path, plan, plan['points'][0], 1, status)
                with self.no_external_work(), patch.object(b, 'extract_baseline', return_value=self.baseline):
                    result = refinement.run_boundary(self.config, path, 'singles', 0, True)
                    repeated = refinement.run_boundary(self.config, path, 'singles', 0, True)
                self.assertEqual(result['new_cfd_attempts'] + repeated['new_cfd_attempts'], 0)
                self.assertEqual(result['points']['center']['status'], status)
                frame = b.load_training(self.config)
                self.assertEqual(sum(frame.run_id.eq('case_000001')), 1)
                self.assertEqual(frame.loc[frame.run_id.eq('case_000001'), 'status'].tolist(), [status])
                self.assertEqual(result['stage_status'], 'awaiting_review' if status == 'review' else 'budget_exhausted')

    def test_boundary_completed_point_leaves_budget_for_new_work(self):
        self.study('boundary_budget')
        path, plan = self.boundary_plan()
        self.reserve_boundary(path, plan, plan['points'][0], 1)
        self.tool_calls.clear()
        with patch.object(b, 'extract_baseline', return_value=self.baseline), \
                patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(cfx, '_run_logged', side_effect=self.external()):
            result = refinement.run_boundary(self.config, path, 'singles', 1, True)
        self.assertEqual(result['new_cfd_attempts'], 1)
        self.assertEqual(b.load_training(self.config).run_id.tolist(), ['case_000000', 'case_000001', 'case_000002'])
        self.assertEqual({run_id for run_id, _ in self.tool_calls}, {'case_000002'})

    def test_boundary_restores_all_reservations_before_stage_gate(self):
        for status in ('success', 'review'):
            with self.subTest(status=status):
                self.study('gate_' + status)
                path, plan = self.boundary_plan()
                singles = [p for p in plan['points'] if p['stage'] == 'singles']
                points = {}
                for index, point in enumerate(singles[:-1], 1):
                    run_id = f'case_{index:06d}'
                    reserve_case(self.root, run_id, {'plan_id': plan['plan_id'], 'point_id': point['point_id'],
                                                    'x': point['x'], 'signature': plan['signature']})
                    self.save_row(index, np.array(point['x']), sample_phase='boundary',
                                  experiment_id=plan['plan_id'], design_role=point['point_id'])
                    points[point['point_id']] = {'run_id': run_id, 'status': 'success'}
                atomic_json(path.with_suffix('.state.json'), {'plan_id': plan['plan_id'], 'points': points})
                self.reserve_boundary(path, plan, singles[-1], 5, status, state=False)
                with self.no_external_work(), patch.object(b, 'extract_baseline', return_value=self.baseline):
                    if status == 'review':
                        with self.assertRaisesRegex(ValueError, 'review|successfully'):
                            refinement.run_boundary(self.config, path, 'pairs', 0, True)
                    else:
                        result = refinement.run_boundary(self.config, path, 'pairs', 0, True)
                        self.assertEqual(result['new_cfd_attempts'], 0)
                        self.assertEqual(result['stage_status'], 'budget_exhausted')
                restored = b.load_training(self.config)
                self.assertEqual(restored.loc[restored.run_id.eq('case_000005'), 'status'].tolist(), [status])
                checkpoint = json.loads(path.with_suffix('.state.json').read_text())
                self.assertEqual(checkpoint['points'][singles[-1]['point_id']]['status'], status)

    def test_boundary_later_reservation_recovers_before_an_unstarted_point(self):
        self.study('boundary_scan')
        path, plan = self.boundary_plan()
        later = plan['points'][1]
        self.reserve_boundary(path, plan, later, 1, state=False)
        with self.no_external_work(), patch.object(b, 'extract_baseline', return_value=self.baseline):
            result = refinement.run_boundary(self.config, path, 'singles', 0, True)
        self.assertEqual(result['new_cfd_attempts'], 0)
        self.assertEqual(result['points'][later['point_id']]['status'], 'success')
        self.assertNotIn('center', result['points'])
        self.assertEqual(set(case_reservations(self.root)), {'case_000001'})

    def test_boundary_late_stop_reconciles_interrupted_row_diagnostic_state_and_pareto(self):
        for status in ('success', 'review'):
            with self.subTest(status=status):
                self.study('boundary_late_stop_' + status)
                path, plan = self.boundary_plan()
                with patch.object(b, 'extract_baseline', return_value=self.baseline), \
                        patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                        patch.object(b, 'run_cfx_pipeline', side_effect=KeyboardInterrupt('first operator stop')):
                    with self.assertRaises(KeyboardInterrupt):
                        refinement.run_boundary(self.config, path, 'singles', 1, False)
                checkpoint = json.loads(path.with_suffix('.state.json').read_text())
                run_id = checkpoint['points']['center']['run_id']
                case = self.root / 'cases' / run_id
                with self.no_external_work(), patch.object(b, 'extract_baseline', return_value=self.baseline):
                    refinement.run_boundary(self.config, path, 'singles', 0, True)
                diagnostics_path = self.root / 'boundary_diagnostics.csv'
                diagnostics = pd.read_csv(diagnostics_path)
                self.assertEqual(diagnostics.loc[diagnostics.run_id.eq(run_id), 'status'].tolist(), ['interrupted'])

                def stop_before_overwrite(config, row):
                    self.assertEqual(row['run_id'], run_id)
                    self.assertEqual(row['status'], status)
                    raise KeyboardInterrupt('second stop after Post, before replacing interrupted CSV')

                with patch.object(b, 'extract_baseline', return_value=self.baseline), \
                        patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                        patch.object(cfx, '_run_logged', side_effect=self.external(status)), \
                        patch.object(b, 'append_row', side_effect=stop_before_overwrite):
                    with self.assertRaises(KeyboardInterrupt):
                        refinement.run_boundary(self.config, path, 'singles', 1, True)
                self.assertTrue(interruption.completed_result_available(case))
                self.assertEqual(b.load_training(self.config).loc[lambda f: f.run_id.eq(run_id), 'status'].tolist(),
                                 ['interrupted'])
                receipt = file_identity(case / 'cfx_state.json')
                archives = list((case / 'attempts').iterdir())
                self.assertEqual(len(archives), 1)
                with self.no_external_work(), patch.object(b, 'run_geometry', side_effect=AssertionError('geometry')), \
                        patch.object(b, 'extract_baseline', return_value=self.baseline):
                    result = refinement.run_boundary(self.config, path, 'singles', 0, True)
                    repeated = refinement.run_boundary(self.config, path, 'singles', 0, True)
                self.assertEqual(result['new_cfd_attempts'] + repeated['new_cfd_attempts'], 0)
                frame = b.load_training(self.config)
                self.assertEqual(frame.loc[frame.run_id.eq(run_id), 'status'].tolist(), [status])
                checkpoint = json.loads(path.with_suffix('.state.json').read_text())
                self.assertEqual(checkpoint['points']['center']['status'], status)
                diagnostics = pd.read_csv(diagnostics_path)
                recorded = diagnostics.loc[diagnostics.run_id.eq(run_id)]
                self.assertEqual(recorded.status.tolist(), [status])
                self.assertAlmostEqual(recorded.iloc[0].true_Efficiency, .8)
                self.assertAlmostEqual(recorded.iloc[0].true_MassFlow, .5 * self.config['runtime']['n_blades'])
                front = pd.read_csv(self.root / 'pareto_front.csv')
                self.assertEqual(run_id in front.run_id.values, status == 'success')
                self.assertEqual(file_identity(case / 'cfx_state.json'), receipt)
                self.assertEqual(list((case / 'attempts').iterdir()), archives)

    def test_recovery_preserves_a_completed_manual_accept_or_reject(self):
        for decision, status in (('accepted', 'success'), ('rejected', 'failed')):
            with self.subTest(decision=decision):
                self.study('manual_' + decision)
                entry = self.enqueue([self.center], 'active_learning')[0]
                case = self.complete_before_csv(0, self.center, 'review', **entry['arguments'])
                with self.no_external_work():
                    pending.run(self.config, self.baseline, 0)
                reviews.decide(self.config, 'case_000000', decision, 'engineering decision must survive recovery')
                csv_identity = file_identity(b.training_csv_path(self.config))
                receipt_identity = file_identity(case / 'cfx_state.json')
                with self.no_external_work(), patch.object(b, 'run_geometry', side_effect=AssertionError('geometry')):
                    recovered = b.recover_recorded_case(self.config, self.baseline, self.center, 0, **entry['arguments'])
                    result = pending.run(self.config, self.baseline, 0)
                self.assertEqual(recovered.row['status'], status)
                self.assertEqual(recovered.success, decision == 'accepted')
                self.assertEqual(result['new_attempts'], 0)
                self.assertEqual(file_identity(b.training_csv_path(self.config)), csv_identity)
                self.assertEqual(file_identity(case / 'cfx_state.json'), receipt_identity)
                self.assertEqual(pending.load(self.config)['entries'][0]['status'], status)
                diagnostics = pd.read_csv(b.diagnostics_csv_path(self.config))
                self.assertEqual(diagnostics.status.tolist(), [status])
                self.assertEqual(b.load_training(self.config).iloc[0].review_decision, decision)
                self.assertFalse((case / 'attempts').exists())

    def test_changed_completion_evidence_is_rejected_without_execution_or_csv(self):
        for name in ('Impeller_Mesh.gtm', 'candidate.json', 'Impeller.def', 'update_bc.ccl',
                     'convergence.ccl', 'Impeller_001.out', 'Impeller_001.res', 'CFX_Results.txt',
                     'physical_signature'):
            with self.subTest(evidence=name):
                self.study('tamper_' + name.replace('.', '_'))
                entry = self.enqueue([self.center])[0]
                case = self.complete_before_csv(0, self.center, **entry['arguments'])
                if name == 'physical_signature':
                    self.config['runtime']['p_out_pa'] += 1
                elif name == 'candidate.json':
                    changed = case / name
                    candidate = json.loads(changed.read_text())
                    candidate['variables'][b.variable_names(self.config)[0]] += .1
                    atomic_json(changed, candidate)
                else:
                    changed = case / name
                    changed.write_text(changed.read_text() + '\nchanged after completion\n')
                receipt = file_identity(case / 'cfx_state.json')
                with self.no_external_work(), patch.object(b, 'run_geometry', side_effect=AssertionError('geometry')):
                    with self.assertRaises(ValueError):
                        b.recover_recorded_case(self.config, self.baseline, self.center, 0, **entry['arguments'])
                self.assertTrue(b.load_training(self.config).empty)
                self.assertEqual(file_identity(case / 'cfx_state.json'), receipt)
                self.assertFalse((case / 'attempts').exists())

    def test_incomplete_post_cannot_be_recovered_as_a_complete_record(self):
        self.study('unfinished_post')
        entry = self.enqueue([self.center])[0]
        case = self.complete_before_csv(0, self.center, **entry['arguments'])
        receipt_path = case / 'cfx_state.json'
        state = json.loads(receipt_path.read_text())
        state['stages']['post'] = {'status': 'running'}
        atomic_json(receipt_path, state)
        (case / 'CFX_Results.txt').unlink()
        receipt = file_identity(receipt_path)
        with self.no_external_work():
            result = b.recover_recorded_case(self.config, self.baseline, self.center, 0, **entry['arguments'])
        self.assertIsNone(result)
        self.assertEqual(file_identity(receipt_path), receipt)
        self.assertTrue(b.load_training(self.config).empty)
        self.assertFalse((case / 'attempts').exists())

    def test_unfinished_operator_stop_requires_budget_and_retries_same_run_id(self):
        self.study('interrupted')
        self.enqueue([self.center])
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=KeyboardInterrupt('operator stop')):
            with self.assertRaises(KeyboardInterrupt):
                pending.run(self.config, self.baseline, 1)
        case = self.root / 'cases/case_000000'
        before = file_identity(case / interruption.ATTEMPT)
        with self.no_external_work():
            self.assertEqual(pending.run(self.config, self.baseline, 0)['new_attempts'], 0)
        self.assertEqual(file_identity(case / interruption.ATTEMPT), before)
        self.assertFalse((case / 'attempts').exists())
        self.assertEqual(b.load_training(self.config).status.tolist(), ['interrupted'])
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(cfx, '_run_logged', side_effect=self.external()):
            result = pending.run(self.config, self.baseline, 1)
        self.assertEqual(result['new_attempts'], 1)
        self.assertEqual(b.load_training(self.config).run_id.tolist(), ['case_000000'])
        self.assertEqual(b.load_training(self.config).status.tolist(), ['success'])
        self.assertTrue((case / 'attempts/interrupted_0001/run_attempt.json').exists())


if __name__ == '__main__':
    unittest.main()
