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
import blade_shape_signature as signatures
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

    def test_pending_zero_budget_recovers_an_accepted_producing_signature(self):
        for status in ('success', 'review'):
            for legacy in (False, True):
                with self.subTest(status=status, legacy=legacy):
                    self.study(f'accepted_completed_{status}_{legacy}')
                    entry = self.enqueue([self.center])[0]
                    case = self.complete_before_csv(0, self.center, status, **entry['arguments'])
                    producing_signature = entry['signature']
                    if legacy:
                        (case / interruption.ATTEMPT).unlink()
                    receipt = file_identity(case / 'cfx_state.json')
                    self.config['runtime']['rpm'] += 1
                    with self.assertRaises(signatures.InputsChanged):
                        signatures.check(self.config, pending.records(self.config), None)
                    signatures.check(self.config, pending.records(self.config),
                                     'corrected the declared RPM; the completed evidence remains applicable')
                    signatures.pin(self.config)
                    with self.no_external_work(), patch.object(b, 'run_geometry', side_effect=AssertionError('geometry')):
                        result = pending.run(self.config, self.baseline, 0)
                        repeated = pending.run(self.config, self.baseline, 0)
                    self.assertEqual(result['new_attempts'] + repeated['new_attempts'], 0)
                    frame = b.load_training(self.config)
                    self.assertEqual(frame.run_id.tolist(), ['case_000000'])
                    self.assertEqual(frame.status.tolist(), [status])
                    self.assertEqual(frame[signatures.COLUMN].tolist(), [producing_signature])
                    self.assertEqual(pending.load(self.config)['entries'][0]['signature'], producing_signature)
                    if not legacy:
                        self.assertEqual(json.loads((case / interruption.ATTEMPT).read_text())['signature'],
                                         producing_signature)
                    self.assertEqual(file_identity(case / 'cfx_state.json'), receipt)
                    self.assertFalse((case / 'attempts').exists())

    def test_boundary_zero_budget_preserves_the_accepted_producing_signature(self):
        for status in ('success', 'review'):
            with self.subTest(status=status):
                self.study('boundary_accepted_completed_' + status)
                path, plan = self.boundary_plan()
                case = self.reserve_boundary(path, plan, plan['points'][0], 1, status)
                (case / interruption.ATTEMPT).unlink()  # Historical completion predates attempt tokens.
                self.config['runtime']['rpm'] += 1
                with self.no_external_work(), patch.object(b, 'run_geometry', side_effect=AssertionError('geometry')), \
                        patch.object(b, 'extract_baseline', return_value=self.baseline):
                    result = refinement.run_boundary(self.config, path, 'singles', 0, True,
                                                     'corrected the declared RPM; completed evidence remains applicable')
                self.assertEqual(result['new_cfd_attempts'], 0)
                row = b.load_training(self.config).loc[lambda f: f.run_id.eq('case_000001')].iloc[0]
                self.assertEqual(row.status, status)
                self.assertEqual(row[signatures.COLUMN], plan['signature'])
                self.assertEqual(result['points']['center']['status'], status)

    def test_accepted_signature_does_not_bypass_completed_cfx_input_checks(self):
        self.study('accepted_changed_solver_input')
        entry = self.enqueue([self.center])[0]
        case = self.complete_before_csv(0, self.center, **entry['arguments'])
        receipt = file_identity(case / 'cfx_state.json')
        self.config['runtime']['p_out_pa'] += 1
        signatures.check(self.config, pending.records(self.config), 'confirmed historical records')
        signatures.pin(self.config)
        with self.no_external_work(), patch.object(b, 'run_geometry', side_effect=AssertionError('geometry')):
            with self.assertRaisesRegex(ValueError, 'inputs|evidence'):
                pending.run(self.config, self.baseline, 0)
        self.assertTrue(b.load_training(self.config).empty)
        self.assertEqual(file_identity(case / 'cfx_state.json'), receipt)

    def test_retried_completion_uses_its_new_attempt_signature(self):
        self.study('retried_producing_signature')
        entry = self.enqueue([self.center])[0]
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=KeyboardInterrupt('operator stop')):
            with self.assertRaises(KeyboardInterrupt):
                pending.run(self.config, self.baseline, 1)
        self.config['runtime']['rpm'] += 1
        signatures.check(self.config, pending.records(self.config), 'corrected declared RPM')
        signatures.pin(self.config)
        case = self.complete_before_csv(0, self.center, **entry['arguments'])
        producing_signature = signatures.physical_signature(self.config)
        self.assertNotEqual(producing_signature, entry['signature'])
        with self.no_external_work():
            result = pending.run(self.config, self.baseline, 0)
        self.assertEqual(result['new_attempts'], 0)
        self.assertEqual(b.load_training(self.config)[signatures.COLUMN].tolist(), [producing_signature])
        self.assertEqual(json.loads((case / interruption.ATTEMPT).read_text())['signature'], producing_signature)

    def test_accepted_producer_still_checks_input_changes_during_recovery(self):
        self.study('recovery_input_race')
        entry = self.enqueue([self.center])[0]
        self.complete_before_csv(0, self.center, **entry['arguments'])
        self.config['runtime']['rpm'] += 1
        signatures.check(self.config, pending.records(self.config), 'corrected declared RPM')
        signatures.pin(self.config)
        original = cfx.recover_cfx_result

        def change_after_verification(*args, **kwargs):
            result = original(*args, **kwargs)
            self.config['runtime']['rpm'] += 1
            return result

        with self.no_external_work(), patch.object(cfx, 'recover_cfx_result', side_effect=change_after_verification):
            result = pending.run(self.config, self.baseline, 0)
        self.assertEqual(result['new_attempts'], 0)
        row = b.load_training(self.config).iloc[0]
        self.assertEqual((row.status, row.failure_stage), ('failed', 'environment'))
        self.assertEqual(row[signatures.COLUMN], entry['signature'])
        self.assertTrue(b.write_pareto(self.config, b.load_training(self.config)).empty)

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


    def stop_at_post(self, status='success'):
        """The operator stop is observed when CFX-Post starts, after a verified Solve."""
        tools = self.external(status)
        def invoke(command, case, log):
            if Path(command[0]).name == 'cfx5post.exe':
                interruption.request_stop(interruption.current_owner(case))
                interruption.check_stop(case)
            return tools(command, case, log)
        return invoke

    def stopped_in_post(self, status='success'):
        self.enqueue([self.center])
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(cfx, '_run_logged', side_effect=self.stop_at_post(status)):
            with self.assertRaises(KeyboardInterrupt):
                pending.run(self.config, self.baseline, 1)
        case = self.root / 'cases/case_000000'
        self.assertEqual(b.load_training(self.config).status.tolist(), ['interrupted'])
        self.assertTrue(interruption.retry_requested(case))
        self.tool_calls.clear()
        return case, json.loads((case / 'cfx_state.json').read_text())['stages']['solve']

    def test_stop_during_post_reruns_only_post_on_the_verified_solve(self):
        for status in ('success', 'review'):
            with self.subTest(status=status):
                self.study(f'post_stop_{status}')
                case, solve = self.stopped_in_post(status)
                kept = {name: file_identity(case / name) for name in
                        (solve['result_file'], solve['out_file'], 'Impeller.def', 'Impeller_Mesh.gtm', 'candidate.json')}
                with patch.object(b, 'run_geometry', side_effect=AssertionError('verified geometry was recomputed')), \
                        patch.object(cfx, '_run_logged', side_effect=self.external(status)):
                    result = pending.run(self.config, self.baseline, 1)
                self.assertEqual(result['new_attempts'], 1)
                self.assertEqual([name for _, name in self.tool_calls], ['cfx5post.exe'])
                self.assertEqual(kept, {name: file_identity(case / name) for name in kept})
                frame = b.load_training(self.config)
                self.assertEqual(frame.run_id.tolist(), ['case_000000'])
                self.assertEqual(frame.status.tolist(), [status])
                journal = json.loads((case / interruption.RETRY).read_text())
                self.assertEqual([attempt['scope'] for attempt in journal['attempts']], ['post'])
                self.assertTrue((case / 'attempts/interrupted_0001' / interruption.ATTEMPT).exists())
                self.assertFalse(list((case / 'attempts').rglob('*.res')), 'the verified Solve must stay in place')

    def test_stop_during_post_with_changed_solve_evidence_recomputes_from_geometry(self):
        self.study('post_stop_changed')
        case, solve = self.stopped_in_post()
        (case / solve['out_file']).write_text('edited after the stop')
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(cfx, '_run_logged', side_effect=self.external()):
            pending.run(self.config, self.baseline, 1)
        self.assertEqual([name for _, name in self.tool_calls], ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe'])
        self.assertEqual((case / 'attempts/interrupted_0001' / solve['out_file']).read_text(), 'edited after the stop')
        self.assertEqual(json.loads((case / interruption.RETRY).read_text())['attempts'][0]['scope'], 'all')
        self.assertEqual(b.load_training(self.config).status.tolist(), ['success'])

    def test_accepted_input_change_recomputes_a_stopped_point(self):
        self.study('accepted_change')
        self.enqueue([self.center])
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=KeyboardInterrupt('operator stop')):
            with self.assertRaises(KeyboardInterrupt):
                pending.run(self.config, self.baseline, 1)
        case = self.root / 'cases/case_000000'
        old = json.loads((case / interruption.ATTEMPT).read_text())['signature']
        Path(self.config['paths']['template_cfx']).write_text('revised CFX project')
        records = signatures.merge(pending.records(self.config),
                                   signatures.training_records(self.config, b.load_training(self.config)))
        with self.assertRaises(signatures.InputsChanged):
            signatures.check(self.config, records, None)
        signatures.check(self.config, records, 'template revision does not affect the stopped point')
        self.assertNotEqual(old, signatures.physical_signature(self.config))
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(cfx, '_run_logged', side_effect=self.external()):
            result = pending.run(self.config, self.baseline, 1)
        self.assertEqual(result['new_attempts'], 1)
        frame = b.load_training(self.config)
        self.assertEqual(frame.status.tolist(), ['success'])
        self.assertEqual(frame[signatures.COLUMN].tolist(), [signatures.physical_signature(self.config)])
        self.assertEqual(json.loads((case / 'attempts/interrupted_0001' / interruption.ATTEMPT).read_text())['signature'], old)


if __name__ == '__main__':
    unittest.main()
