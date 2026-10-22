"""Recovery tests use synthetic files and substitutes, never engineering programs."""
import json
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import blade_shape_active_learning as b
import blade_shape_cfx_runner as cfx
import blade_shape_convergence as convergence
import blade_shape_interrupt as interruption
import blade_shape_pending as pending
import blade_shape_recovery as recovery
import blade_shape_refinement as refinement
from blade_shape_runtime import atomic_json, file_identity
from tests.cfx_output_samples import output
from tests.synthetic_pipeline import fake_geometry
from tests import test_refinement

METRICS = dict(Efficiency=.8, MassFlow=4.5, PressureRatio=2., Power=1., totalpressureratio=2.1)


class InterruptedCaseTests(unittest.TestCase):
    setUp = test_refinement.RefinementTests.setUp
    save_row = test_refinement.RefinementTests.save_row

    def test_interrupted_queue_recomputes_same_case_without_duplicate_training_rows(self):
        x = refinement.enforce_fixed(self.config, self.center)
        pending.enqueue(self.config, [b.SelectedCandidate(x, 1, 'doe')], [{'sample_phase':'doe', 'doe_index':0}])
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=KeyboardInterrupt('operator')):
            with self.assertRaises(KeyboardInterrupt):
                pending.run(self.config, self.baseline, 1)
        self.assertEqual(b.load_training(self.config).status.tolist(), ['interrupted'])
        self.assertFalse(pending.load(self.config)['entries'][0]['finalized'])
        case = self.root/'cases/case_000000'
        self.assertTrue(interruption.retry_requested(case))
        (case/'partial.res').write_text('not accepted')
        self.assertEqual(pending.run(self.config, self.baseline, 0)['new_attempts'], 0)
        self.assertTrue((case/'partial.res').exists(), 'zero budget must not archive/recompute')
        with patch.object(b, 'run_geometry', side_effect=fake_geometry) as geometry, \
                patch.object(b, 'run_cfx_pipeline', return_value=cfx.CfxResult(True, METRICS, 'ok')):
            result = pending.run(self.config, self.baseline, 1)
        self.assertEqual(result['new_attempts'], 1)
        geometry.assert_called_once()
        self.assertEqual(b.load_training(self.config).run_id.tolist(), ['case_000000'])
        self.assertEqual(b.load_training(self.config).status.tolist(), ['success'])
        self.assertEqual((case/'attempts/interrupted_0001/partial.res').read_text(), 'not accepted')

    def test_gui_killed_cli_receipt_is_retryable_only_with_matching_stop_token(self):
        case = self.root/'cases/case_000000'
        data = interruption.begin_case(case, 'case_000000', 'signature', [1.])
        (case/'partial.out').write_text('unfinished')
        atomic_json(Path(data['owner']['stop_file']), {'token': 'different run'})
        self.assertFalse(interruption.retry_requested(case))
        interruption.request_stop(data['owner'])
        self.assertTrue(interruption.retry_requested(case))
        interruption.begin_case(case, 'case_000000', 'signature', [1.])
        self.assertTrue((case/'attempts/interrupted_0001/partial.out').exists())

    def test_unmarked_crash_live_worker_and_changed_design_are_not_retried(self):
        case = self.root/'cases/case_000000'
        data = interruption.begin_case(case, 'case_000000', 'signature', [1.])
        (case/'partial.res').write_text('partial')
        self.assertFalse(interruption.retry_requested(case))
        with self.assertRaisesRegex(ValueError, 'Unmarked running'):
            interruption.begin_case(case, 'case_000000', 'signature', [1.])
        self.assertTrue((case/'partial.res').exists())
        interruption.request_stop(data['owner'])
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            interruption.begin_case(case, 'case_000000', 'changed', [1.])
        atomic_json(case/'active_process.json', {'status':'running', 'pid':os.getpid()})
        with self.assertRaisesRegex(RuntimeError, 'live or unverifiable'):
            interruption.begin_case(case, 'case_000000', 'signature', [1.])

    def test_legacy_csv_round_trip_does_not_change_the_interrupted_design(self):
        case=self.root/'case'
        data=interruption.begin_case(case,'case_000000','same',[2.741139422875676])
        interruption.request_stop(data['owner'])
        interruption.begin_case(case,'case_000000','same',[2.7411394228756762])
        self.assertTrue((case/'attempts/interrupted_0001/run_attempt.json').exists())

    def test_boundary_stop_retries_reserved_point_and_does_not_fail_stage(self):
        self.save_row()
        plan = self.root/'plan.json'
        with patch.object(b, 'extract_baseline', return_value=self.baseline):
            refinement.write_boundary_plan(self.config, 'case_000000', plan)
        with patch.object(b, 'extract_baseline', return_value=self.baseline), \
                patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', side_effect=KeyboardInterrupt('operator')):
            with self.assertRaises(KeyboardInterrupt):
                refinement.run_boundary(self.config, plan, 'singles', 1, False)
        state = json.loads(plan.with_suffix('.state.json').read_text())
        run_id = next(iter(state['points'].values()))['run_id']
        with patch.object(b, 'extract_baseline', return_value=self.baseline), \
                patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', return_value=cfx.CfxResult(True, METRICS, 'ok')):
            result = refinement.run_boundary(self.config, plan, 'singles', 1, True)
        self.assertEqual(result['new_cfd_attempts'], 1)
        self.assertNotEqual(result['stage_status'], 'failed')
        self.assertEqual(sum(b.load_training(self.config).run_id.eq(run_id)), 1)

    def test_archive_journal_recovers_a_move_interrupted_midway(self):
        case = self.root/'case'
        data = interruption.begin_case(case, 'case_000000', 'same', [1.])
        (case/'first.txt').write_text('first')
        (case/'second.txt').write_text('second')
        interruption.request_stop(data['owner'])
        original = interruption.shutil.move
        count = 0
        def move(source, target):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('simulated crash')
            return original(source, target)
        with patch.object(interruption.shutil, 'move', side_effect=move):
            with self.assertRaises(OSError):
                interruption.begin_case(case, 'case_000000', 'same', [1.])
        interruption.begin_case(case, 'case_000000', 'same', [1.])
        dest = case/'attempts/interrupted_0001'
        self.assertEqual((dest/'first.txt').read_text(), 'first')
        self.assertEqual((dest/'second.txt').read_text(), 'second')


class ReassessmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = json.loads(Path('blade_shape_config.json').read_text())
        paths = self.config['paths']
        paths.update(output_dir=str(self.root), cfx_bin_dir=str(self.root/'bin'),
                     template_cfx=str(self.root/'template.cfx'), template_cse=str(self.root/'extract.cse'))
        (self.root/'bin').mkdir()
        for name in ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe']:
            (self.root/'bin'/name).write_text('substitute')
        for key in ['template_cfx','template_cse']:
            Path(paths[key]).write_text('synthetic template')
        self.case = self.root/'cases/case_000000'
        self.case.mkdir(parents=True)
        (self.case/'Impeller_Mesh.gtm').write_text('mesh')
        self.x = refinement.enforce_fixed(self.config, np.zeros(12))
        (self.case/'candidate.json').write_text(json.dumps({'run_id':'case_000000',
                                                         'variables':b.vector_to_sample(self.config,self.x)}))
        self.kw = dict(p_out_pa=float(self.config['runtime']['p_out_pa']),cfx_bin_dir=paths['cfx_bin_dir'],
                       template_cfx=paths['template_cfx'],template_cse=paths['template_cse'],
                       n_blades=int(self.config['runtime']['n_blades']))
        solves = 0
        def external(cmd, cwd, log):
            nonlocal solves
            if Path(cmd[0]).name == 'cfx5pre.exe':
                (cwd/'Impeller.def').write_text('def')
            elif Path(cmd[0]).name == 'cfx5solve.exe':
                solves += 1
                (cwd/f'Impeller_{solves:03}.res').write_text('result'+str(solves))
                text=output('2E-6',2000,True,{k:'3E-5' for k in ['U - Mom','V - Mom','W - Mom','P - Mass']})
                if solves == 2:
                    text=text.replace('OUTER LOOP ITERATION = 2000','OUTER LOOP ITERATION = 4000 (2000)')
                (cwd/f'Impeller_{solves:03}.out').write_text(text)
            else:
                self.fail('Old policy must reject four equations without Post')
            return 0
        with patch.object(cfx,'_run_logged',side_effect=external):
            result=cfx.run_cfx_pipeline(self.case,'case_000000',review=convergence.ReviewPolicy(),**self.kw)
        self.assertFalse(result.success)
        b.append_row(self.config,{**b.vector_to_sample(self.config,self.x),'run_id':'case_000000',
                     'sample_phase':'doe','status':'failed','failure_stage':'solve','message':result.message,
                     'custom_history_column':'preserve me'})

    def post(self, cmd, cwd, log):
        self.assertEqual(Path(cmd[0]).name,'cfx5post.exe', 'reassessment must never launch a solver')
        (cwd/'CFX_Results.txt').write_text('.8,2,1,.5,2.1')
        return 0

    def test_plan_is_read_only_apply_extracts_review_metrics_and_is_idempotent(self):
        before=file_identity(self.case/'cfx_state.json')
        plan=recovery.make_plan(self.config,'case_000000')
        self.assertEqual(before,file_identity(self.case/'cfx_state.json'))
        with patch.object(cfx,'_run_logged',side_effect=self.post) as post:
            recovery.apply_plan(self.config,plan,'residual-limit policy')
            recovery.apply_plan(self.config,plan,'same operation again')
        post.assert_called_once()
        row=b.load_training(self.config).iloc[0]
        self.assertEqual(row.status,'review')
        self.assertEqual(row.Efficiency,.8)
        self.assertEqual(row.MassFlow,.5*self.kw['n_blades'])
        self.assertEqual(row.custom_history_column,'preserve me')
        self.assertTrue(b.write_pareto(self.config,b.load_training(self.config)).empty)
        state=json.loads((self.case/'cfx_state.json').read_text())
        self.assertEqual(state['reassessment_history'][0]['original_solve']['status'],'failed')

    def test_changed_result_or_policy_and_human_rejection_are_refused(self):
        plan=recovery.make_plan(self.config,'case_000000')
        (self.case/'Impeller_002.res').write_text('changed')
        with self.assertRaisesRegex(ValueError,'evidence changed'):
            recovery.apply_plan(self.config,plan,'inspect')
        self.config['cfx_review']['rms_limit']=5e-5
        with self.assertRaisesRegex(ValueError,'policy changed'):
            recovery.apply_plan(self.config,plan,'inspect')
        review_path=b.training_csv_path(self.config)
        import blade_shape_review as reviews
        reviews._set(review_path,'case_000000',{'failure_stage':'review_rejected'})
        with self.assertRaisesRegex(ValueError,'human rejections'):
            recovery.make_plan(self.config,'case_000000')

    def test_failed_post_can_be_retried_without_resolving(self):
        plan=recovery.make_plan(self.config,'case_000000')
        with patch.object(cfx,'_run_logged',return_value=1):
            with self.assertRaisesRegex(ValueError,'Post did not complete'):
                recovery.apply_plan(self.config,plan,'inspect')
        self.assertEqual(b.load_training(self.config).iloc[0].status,'failed')
        with patch.object(cfx,'_run_logged',side_effect=self.post):
            recovery.apply_plan(self.config,plan,'retry Post')
        self.assertEqual(b.load_training(self.config).iloc[0].status,'review')

    def test_prepared_reassessment_cannot_switch_to_another_result(self):
        plan=recovery.make_plan(self.config,'case_000000')
        with patch.object(cfx,'_run_logged',return_value=1):
            with self.assertRaises(ValueError):
                recovery.apply_plan(self.config,plan,'inspect')
        receipt=self.case/'cfx_state.json'
        state=json.loads(receipt.read_text())
        state['stages']['solve']['result_file']='another.res'
        atomic_json(receipt,state)
        with patch.object(cfx,'_run_logged',side_effect=AssertionError('must refuse before Post')):
            with self.assertRaisesRegex(ValueError,'prepared reassessment receipt changed'):
                recovery.apply_plan(self.config,plan,'retry')

    def test_legacy_running_receipt_needs_explicit_operator_confirmation(self):
        atomic_json(pending.path_for(self.config), {'version':1,'entries':[{
            'run_id':'case_000000','status':'failed','finalized':True,
            'signature':b.load_training(self.config).iloc[0].physical_signature,'x':self.x.tolist(),
            'arguments':{'sample_phase':'doe','doe_index':0}}]})
        # Do not treat a numerical failure as an operator stop.
        with self.assertRaisesRegex(ValueError,'completed/unconverged'):
            recovery.retry_interrupted(self.config,'case_000000','operator confirmed')
        (self.case/'cfx_state.json').write_text(json.dumps({'version':2,'stages':{'solve':{'status':'running'}}}))
        for path in self.case.glob('*.out'):
            path.write_text('OUTER LOOP ITERATION = 12\ntruncated')
        recovery.retry_interrupted(self.config,'case_000000','operator confirmed old stop')
        self.assertEqual(b.load_training(self.config).iloc[0].status,'interrupted')
        self.assertFalse(pending.load(self.config)['entries'][0]['finalized'])


    def test_legacy_stop_during_continuation_keeps_the_initial_result_as_history(self):
        receipt=self.case/'cfx_state.json'
        state=json.loads(receipt.read_text())
        initial=state['solve_attempts'][0]
        state['stages']['solve']={**initial,'status':'needs_restart','exit_code':0}
        state['stages']['solve_restart']={'status':'running'}
        state['solve_attempts']=[initial]
        atomic_json(receipt,state)
        for path in self.case.glob('*.out'):
            if path.name != initial['out_file']:
                path.write_text('OUTER LOOP ITERATION = 5\nunfinished continuation')
        atomic_json(pending.path_for(self.config), {'version':1,'entries':[{
            'run_id':'case_000000','status':'failed','finalized':True,
            'signature':b.load_training(self.config).iloc[0].physical_signature,'x':self.x.tolist(),
            'arguments':{'sample_phase':'doe','doe_index':0}}]})
        before=file_identity(self.case/initial['result_file'])
        recovery.retry_interrupted(self.config,'case_000000','operator stopped the continuation')
        self.assertEqual(b.load_training(self.config).iloc[0].status,'interrupted')
        self.assertEqual(before,file_identity(self.case/initial['result_file']))
        self.assertFalse((self.case/'attempts').exists(), 'registration must not start or archive CFD')


class LegacyInterruptionTests(unittest.TestCase):
    setUp = test_refinement.RefinementTests.setUp
    save_row = test_refinement.RefinementTests.save_row

    def failed_queue_point(self):
        x = refinement.enforce_fixed(self.config, self.center)
        pending.enqueue(self.config, [b.SelectedCandidate(x, 1, 'doe')], [{'sample_phase': 'doe', 'doe_index': 0}])
        self.save_row(x=x, status='failed', sample_phase='doe', failure_stage='geometry')
        queue = pending.load(self.config)
        queue['entries'][0].update(status='failed', finalized=True)
        atomic_json(pending.path_for(self.config), queue)
        case = self.root/'cases/case_000000'
        case.mkdir(parents=True)
        return case, x

    def reserved_boundary_point(self):
        self.save_row()
        path = self.root/'plan.json'
        with patch.object(b, 'extract_baseline', return_value=self.baseline):
            plan = refinement.write_boundary_plan(self.config, 'case_000000', path)
            # Simulate an old CLI stopped after reserving the point but before appending any CSV row.
            with patch.object(b, 'evaluate_true_cfd', side_effect=KeyboardInterrupt('old operator stop')):
                with self.assertRaises(KeyboardInterrupt):
                    refinement.run_boundary(self.config, path, 'singles', 1, False)
        reservations = json.loads((self.root/'case_reservations.json').read_text())
        run_id, owner = next(iter(reservations.items()))
        case = self.root/'cases'/run_id
        case.mkdir(parents=True)
        atomic_json(case/'geometry_state.json', {'status': 'running'})
        return path, plan, run_id, owner, case

    def test_explicit_failed_stages_cannot_be_relabelled_as_operator_interruption(self):
        case, _ = self.failed_queue_point()
        before = {path: file_identity(path) for path in (b.training_csv_path(self.config), pending.path_for(self.config))}
        for stage in ('geometry', 'pre', 'solve', 'solve_restart', 'post', 'nonzero_exit'):
            with self.subTest(stage=stage):
                geometry = {'status': 'failed'} if stage == 'geometry' else {'status': 'complete'}
                stages = {} if stage == 'geometry' else {stage: {'status': 'failed'}}
                if stage == 'nonzero_exit':
                    stages = {'pre': {'status': 'returned', 'exit_code': 1}}
                atomic_json(case/'geometry_state.json', geometry)
                atomic_json(case/'cfx_state.json', {'version': 2, 'stages': stages})
                receipts = {path: file_identity(path) for path in (case/'geometry_state.json', case/'cfx_state.json')}
                with self.assertRaisesRegex(ValueError, 'recorded failed stage'):
                    recovery.retry_interrupted(self.config, 'case_000000', 'operator confirmed')
                self.assertFalse((case/interruption.ATTEMPT).exists())
                self.assertFalse((case/'attempts').exists())
                self.assertEqual(before, {path: file_identity(path) for path in before})
                self.assertEqual(receipts, {path: file_identity(path) for path in receipts})

    def test_registered_evidence_changed_before_resume_is_refused_without_archiving(self):
        case, x = self.failed_queue_point()
        atomic_json(case/'geometry_state.json', {'status': 'running'})
        atomic_json(case/'candidate.json', {'run_id': 'case_000000',
                                            'variables': dict(zip(b.variable_names(self.config), x.tolist()))})
        recovery.retry_interrupted(self.config, 'case_000000', 'confirmed old geometry stop')
        changes = {'cfx_state.json': json.dumps({'version': 2, 'stages': {'pre': {'status': 'running'}}}),
                   'geometry_state.json': json.dumps({'status': 'running', 'note': 'edited'}),
                   'candidate.json': json.dumps({**json.loads((case/'candidate.json').read_text()), 'note': 'edited'})}
        for name, text in changes.items():
            with self.subTest(changed=name):
                path = case/name
                original = path.read_bytes() if path.exists() else None
                path.write_text(text)
                with patch.object(b, 'run_geometry', side_effect=AssertionError('changed evidence was recomputed')):
                    with self.assertRaisesRegex(ValueError, f'retry-interrupted changed.*{name}'):
                        pending.run(self.config, self.baseline, 1)
                self.assertFalse((case/'attempts').exists())
                self.assertEqual(json.loads((case/interruption.ATTEMPT).read_text())['status'], 'interrupted')
                path.write_bytes(original) if original is not None else path.unlink()
        with patch.object(b, 'run_geometry', side_effect=fake_geometry), \
                patch.object(b, 'run_cfx_pipeline', return_value=cfx.CfxResult(True, METRICS, 'ok')):
            self.assertEqual(pending.run(self.config, self.baseline, 1)['new_attempts'], 1)
        self.assertEqual(b.load_training(self.config).status.tolist(), ['success'])
        self.assertTrue((case/'attempts/interrupted_0001/candidate.json').exists())

    def test_legacy_geometry_running_receipt_still_recomputes_same_design(self):
        case, x = self.failed_queue_point()
        atomic_json(case/'geometry_state.json', {'status': 'running'})
        recovery.retry_interrupted(self.config, 'case_000000', 'confirmed old geometry stop')
        with patch.object(b, 'run_geometry', side_effect=fake_geometry) as geometry, \
                patch.object(b, 'run_cfx_pipeline', return_value=cfx.CfxResult(True, METRICS, 'ok')):
            result = pending.run(self.config, self.baseline, 1)
        self.assertEqual(result['new_attempts'], 1)
        geometry.assert_called_once()
        frame = b.load_training(self.config)
        self.assertEqual(frame.run_id.tolist(), ['case_000000'])
        self.assertEqual(frame.status.tolist(), ['success'])
        np.testing.assert_allclose(frame[b.variable_names(self.config)].iloc[0].to_numpy(float), x, rtol=0, atol=1e-10)
        self.assertEqual(json.loads((case/'attempts/interrupted_0001/geometry_state.json').read_text())['status'], 'running')

    def test_legacy_pre_running_receipt_can_still_be_registered(self):
        case, _ = self.failed_queue_point()
        atomic_json(case/'geometry_state.json', {'status': 'complete'})
        atomic_json(case/'cfx_state.json', {'version': 2, 'stages': {'pre': {'status': 'running'}}})
        recovery.retry_interrupted(self.config, 'case_000000', 'confirmed old pre stop')
        self.assertTrue(interruption.retry_requested(case))
        self.assertEqual(b.load_training(self.config).status.tolist(), ['interrupted'])
        self.assertFalse(pending.load(self.config)['entries'][0]['finalized'])

    def test_reserved_boundary_without_csv_or_queue_retries_same_case(self):
        path, plan, run_id, owner, case = self.reserved_boundary_point()
        before = file_identity(self.root/'case_reservations.json')
        self.assertFalse(b.load_training(self.config).run_id.eq(run_id).any())
        self.assertEqual(pending.load(self.config)['entries'], [])
        recovery.retry_interrupted(self.config, run_id, 'confirmed old boundary stop')
        row = b.load_training(self.config).loc[lambda frame: frame.run_id.eq(run_id)].iloc[0]
        self.assertEqual(row.status, 'interrupted')
        self.assertEqual(row.sample_phase, 'boundary')
        self.assertEqual(row.experiment_id, plan['plan_id'])
        self.assertEqual(row.design_role, owner['point_id'])
        self.assertEqual(row.selection_source, 'predefined_boundary')
        self.assertTrue(row[b.RESULT_COLUMNS].isna().all())
        with patch.object(b, 'extract_baseline', return_value=self.baseline), \
                patch.object(b, 'run_geometry', side_effect=fake_geometry) as geometry, \
                patch.object(b, 'run_cfx_pipeline', return_value=cfx.CfxResult(True, METRICS, 'ok')):
            result = refinement.run_boundary(self.config, path, 'singles', 1, True)
        self.assertEqual(result['new_cfd_attempts'], 1)
        geometry.assert_called_once()
        rows = b.load_training(self.config).loc[lambda frame: frame.run_id.eq(run_id)]
        self.assertEqual(rows.status.tolist(), ['success'])
        np.testing.assert_allclose(rows[b.variable_names(self.config)].iloc[0].to_numpy(float), owner['x'], rtol=0, atol=1e-10)
        self.assertEqual(before, file_identity(self.root/'case_reservations.json'))
        self.assertTrue((case/'attempts/interrupted_0001/geometry_state.json').exists())

    def test_invalid_boundary_reservations_are_rejected_without_mutation(self):
        _, _, run_id, owner, case = self.reserved_boundary_point()
        before = file_identity(b.training_csv_path(self.config))
        invalid = [dict(signature='incompatible'), dict(x=owner['x'][:-1]), dict(x=[owner['x']]),
                   dict(x=['NaN'] + owner['x'][1:]), dict(plan_id=''), dict(point_id=None)]
        for changes in invalid:
            with self.subTest(changes=changes):
                atomic_json(self.root/'case_reservations.json', {run_id: {**owner, **changes}})
                with self.assertRaises(ValueError):
                    recovery.retry_interrupted(self.config, run_id, 'operator confirmed')
                self.assertEqual(before, file_identity(b.training_csv_path(self.config)))
                self.assertFalse((case/interruption.ATTEMPT).exists())
                self.assertFalse(pending.path_for(self.config).exists())

    def test_conflicting_queue_or_candidate_cannot_change_a_reserved_case(self):
        _, _, run_id, owner, case = self.reserved_boundary_point()
        before = file_identity(b.training_csv_path(self.config))
        atomic_json(pending.path_for(self.config), {'version': 1, 'entries': [{
            'run_id': run_id, 'signature': owner['signature'], 'x': owner['x'], 'arguments': {'sample_phase': 'doe'}}]})
        with self.assertRaisesRegex(ValueError, 'uniquely tracked'):
            recovery.retry_interrupted(self.config, run_id, 'operator confirmed')
        pending.path_for(self.config).unlink()
        for wrong_id in (False, True):
            with self.subTest(wrong_id=wrong_id):
                x = np.array(owner['x'])
                if not wrong_id:
                    x[0] += 1
                atomic_json(case/'candidate.json', {'run_id': 'case_999999' if wrong_id else run_id,
                                                   'variables': b.vector_to_sample(self.config, x)})
                with self.assertRaisesRegex(ValueError, 'Existing candidate'):
                    recovery.retry_interrupted(self.config, run_id, 'operator confirmed')
                self.assertEqual(before, file_identity(b.training_csv_path(self.config)))
                self.assertFalse((case/interruption.ATTEMPT).exists())
                self.assertFalse((case/'attempts').exists())

    def test_recorded_design_must_match_its_queue_before_registration(self):
        case, _ = self.failed_queue_point()
        queue = pending.load(self.config)
        queue['entries'][0]['x'][0] += 1
        atomic_json(pending.path_for(self.config), queue)
        before = file_identity(b.training_csv_path(self.config))
        with self.assertRaisesRegex(ValueError, 'Recorded row'):
            recovery.retry_interrupted(self.config, 'case_000000', 'operator confirmed')
        self.assertEqual(before, file_identity(b.training_csv_path(self.config)))
        self.assertFalse((case/interruption.ATTEMPT).exists())


class ReviewModeTests(unittest.TestCase):
    def test_four_equations_get_post_metrics_under_new_mode_and_old_mode_stays_compatible(self):
        from tests.test_convergence import PipelineConvergenceTests
        fixture=PipelineConvergenceTests()
        fixture.setUp()
        self.addCleanup(fixture.tmp.cleanup)
        def four(rms, iteration, limit):
            return output('2E-6',iteration,limit,{k:'3E-5' for k in ['U - Mom','V - Mom','W - Mom','P - Mass']})
        result,calls=fixture.run_case([('x',True),('x',True)],exhausted_output=four,initial_iteration=2000,
                                      review=convergence.ReviewPolicy(mode='residual_limit'))
        self.assertTrue(result.review)
        self.assertEqual(result.metrics['Efficiency'],.76)
        self.assertEqual(sum(Path(cmd[0]).name=='cfx5post.exe' for cmd in calls),1)
        self.assertEqual(convergence.ReviewPolicy.from_config({}).mode,'legacy_count')
        report=convergence.assess_out(four('x',2000,True),convergence.ConvergencePolicy())
        self.assertIsNone(convergence.review_evidence(report,convergence.ConvergencePolicy(),convergence.ReviewPolicy()))
        self.assertIsNone(convergence.review_evidence(report,convergence.ConvergencePolicy(),
                          convergence.ReviewPolicy(mode='residual_limit',max_equations_above_target=0)))

    def test_tail_window_and_global_balance_are_evidence_not_automatic_acceptance(self):
        text=output('2E-4',1999,False)+output('3E-5',2000,True)
        text += ('\n Normalised Imbalance Summary\n'
                 '| Equation | Maximum Flow | Imbalance (%) |\n'
                 '| P-Mass | 4.5E-1 | -0.1731 |\n'
                 '| H-Energy | 1.0E10 | 0.0355 |\n\n')
        report=convergence.assess_out(text,convergence.ConvergencePolicy(),history_window=2)
        self.assertEqual(report['history']['above_1e4_count'],1)
        self.assertEqual(report['normalised_imbalance_percent']['p-mass'],-.1731)
        evidence=convergence.review_evidence(report,convergence.ConvergencePolicy(),
                                             convergence.ReviewPolicy(mode='residual_limit'))
        self.assertTrue(any('history window' in warning for warning in evidence['warnings']))
        self.assertFalse(report['history']['performance_monitors_verified'])


@unittest.skipUnless(importlib.util.find_spec('PySide6') and importlib.util.find_spec('psutil'),
                     'GUI cancellation requires optional PySide6 and psutil')
class GuiStopReceiptTests(unittest.TestCase):
    def test_stop_records_token_before_terminating_cli_and_resume_can_detect_it(self):
        from PySide6.QtWidgets import QApplication
        from blade_gui.runner import CommandRunner
        import time
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            case=Path(directory)/'cases/case_000000'
            runner=CommandRunner(Path.cwd())
            lines=[]
            runner.line.connect(lambda channel,line:lines.append(line))
            script=('import time; from pathlib import Path; import blade_shape_interrupt as i; '
                    f'i.begin_case(Path({str(case)!r}), "case_000000", "same", [1.]); '
                    'print("ready",flush=True); time.sleep(30)')
            runner.start([sys.executable,'-c',script],scope=[directory])
            try:
                deadline=time.monotonic()+10
                while 'ready' not in lines and time.monotonic()<deadline:
                    app.processEvents();time.sleep(.02)
                self.assertIn('ready',lines)
                runner.stop()
                deadline=time.monotonic()+10
                while runner.running and time.monotonic()<deadline:
                    app.processEvents();time.sleep(.02)
                self.assertFalse(runner.running)
                receipt=json.loads(Path(runner._stop_owner['stop_file']).read_text())
                self.assertTrue(receipt['processes'], 'the stop scope must name the processes it terminates')
                self.assertTrue(all(record['started_at'] for record in receipt['processes'].values()),
                                'GUI-known start times distinguish these processes from later PID reuse')
                self.assertTrue(interruption.retry_requested(case))
                interruption.begin_case(case,'case_000000','same',[1.])
                self.assertTrue((case/'attempts/interrupted_0001/run_attempt.json').exists())
            finally:
                if runner.running:
                    runner.stop(wait=True)
                runner.deleteLater()
                app.processEvents()
