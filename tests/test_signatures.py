"""Physical signatures: what invalidates a label, explicit acceptance and old-format records."""
from __future__ import annotations

import copy
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
import blade_shape_cfx_runner as cfx
import blade_shape_refinement as r
import blade_shape_signature as s
from blade_gui import commands
from tests.cfx_output_samples import output as cfx_output


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.c = json.loads(Path('blade_shape_config.json').read_text())
        self.c['paths']['output_dir'] = str(self.out)
        self.c['paths']['cft_batch_template'] = str(Path('tests/fixtures/synthetic_meanline.cft-batch').resolve())
        self.c['surrogate'].update(model='rbf', ehvi_y_samples=16, ehvi_validation_samples=32)
        self.c['runtime'].update(initial_samples=0, nsga2_pop_size=6, nsga2_generations=1, candidate_pool_size=8)
        self.c['refinement'].pop('boundary_levels', None)
        self.c['refinement'].pop('local_search', None)
        self.x = r.enforce_fixed(self.c, np.zeros(12))
        self.x[4] = 2.
        self.cp = self.out / 'config.json'

    def rows(self, n=4, config=None, **extra):
        config = config or self.c
        for i in range(n):
            x = self.x.copy()
            x[0] += .1 * i
            b.append_row(config, {**b.vector_to_sample(config, x), 'run_id': f'case_{i:06d}', 'status': 'success',
                                  'sample_phase': 'active_learning', 'al_iteration': 0,
                                  'Efficiency': .760 + .0001 * i, 'MassFlow': 4.2 + .01 * i, **extra})

    def run_args(self, *extra):
        self.cp.write_text(json.dumps(self.c))
        return b.build_parser().parse_args(['run', '--config', str(self.cp), '--resume', '--iterations', '1',
                                            '--batch-size', '1', '--max-new-cfd', '1', *extra])

    @staticmethod
    def complete(config, baseline, x, index, **meta):
        row = {**b.vector_to_sample(config, x), **meta, 'run_id': f'case_{index:06d}', 'status': 'success',
               'Efficiency': .762, 'MassFlow': 4.25}
        b.append_row(config, row)
        return b.CaseResult(row, True)

    # ------------------------------------------------------------- scope
    def test_only_result_changing_inputs_enter_the_signature(self):
        base = s.physical_signature(self.c)
        same = copy.deepcopy(self.c)
        same['search']['fixed_variables'] = {k: v + .1 for k, v in same['search']['fixed_variables'].items()}
        same['constraints']['duplicate_distance_norm'] *= 2
        same['variables'][0]['upper'] += 1
        same['runtime']['cfx_cores'] += 4
        same['paths']['cfx_bin_dir'] = 'E:/other/install'  # neither has a version folder or solver here
        moved = self.out / 'moved.cft-batch'
        moved.write_bytes(Path(self.c['paths']['cft_batch_template']).read_bytes())
        same['paths']['cft_batch_template'] = str(moved)
        self.assertEqual(s.physical_signature(same), base)
        for change in (lambda c: c['runtime'].update(p_out_pa=c['runtime']['p_out_pa'] + 1),
                       lambda c: c['cfx_convergence'].update(restart_iterations=1500),
                       lambda c: c['variables'].reverse()):
            changed = copy.deepcopy(self.c)
            change(changed)
            self.assertNotEqual(s.physical_signature(changed), base)
        moved.write_text('edited template')
        self.assertNotEqual(s.physical_signature(same), base)

    # ------------------------------------------------- evidence and check
    def test_rows_from_other_inputs_are_not_evidence_and_stop_the_run(self):
        self.rows(4)
        old = copy.deepcopy(self.c)
        before = self.c['runtime']['p_out_pa']
        self.c['runtime']['p_out_pa'] += 2
        history, _ = r.training_partition(self.c, b.load_training(self.c))
        self.assertTrue(history.empty)
        self.assertTrue(b.write_pareto(self.c, b.load_training(self.c)).empty)
        with patch.object(b, 'evaluate_true_cfd', side_effect=AssertionError('must not start CFD')):
            with self.assertRaises(s.InputsChanged) as caught:
                b.run_loop(self.run_args())
        self.assertIn(f"runtime.p_out_pa: {before!r} -> {before + 2!r}", str(caught.exception))
        self.assertIn('4 record(s)', str(caught.exception))
        self.assertEqual(len(r.training_partition(old, b.load_training(old))[0]), 4)

    def test_accepted_change_is_recorded_and_makes_old_rows_evidence(self):
        self.rows(4)
        self.c['runtime']['alpha0'] += 1.0  # declared metadata only; the CFD is unchanged
        with patch.object(b, 'evaluate_true_cfd', side_effect=self.complete), redirect_stdout(StringIO()):
            b.run_loop(self.run_args('--accept-input-change', 'alpha0 was only declared, never sent to CFX'))
        registry = json.loads((self.out / s.REGISTRY).read_text())
        self.assertEqual(len(registry['accepted']), 1)
        entry = registry['accepted'][0]
        self.assertEqual(entry['reason'], 'alpha0 was only declared, never sent to CFX')
        self.assertEqual(entry['to'], s.physical_signature(self.c))
        self.assertEqual(len(entry['records'][entry['from'][0]]), 4)
        frame = b.load_training(self.c)
        self.assertEqual(len(frame), 5)
        self.assertTrue(s.evidence_mask(self.c, frame).all())
        # the new row carries the new signature; old rows keep theirs
        self.assertEqual(frame[s.COLUMN].nunique(), 2)
        # the next run needs no further acceptance, and a later change is caught again
        with patch.object(b, 'evaluate_true_cfd', side_effect=self.complete), redirect_stdout(StringIO()):
            b.run_loop(self.run_args())
        self.c['runtime']['p_out_pa'] += 1
        with self.assertRaises(s.InputsChanged):
            b.run_loop(self.run_args())

    def test_blank_reason_is_refused_and_nothing_to_accept_is_not_recorded(self):
        with redirect_stdout(StringIO()), patch('sys.stderr', StringIO()), self.assertRaises(SystemExit):
            b.build_parser().parse_args(['run', '--accept-input-change', '  '])
        self.rows(4)
        with patch.object(b, 'evaluate_true_cfd', side_effect=self.complete), redirect_stdout(StringIO()) as out:
            b.run_loop(self.run_args('--accept-input-change', 'nothing changed'))
        self.assertIn('nothing to accept', out.getvalue())
        self.assertEqual(json.loads((self.out / s.REGISTRY).read_text())['accepted'], [])

    # ------------------------------------------------------ old records
    def test_rows_and_entries_from_before_signatures(self):
        self.rows(4)
        frame = pd.read_csv(self.out / 'training_data.csv').drop(columns=[s.COLUMN])
        frame.to_csv(self.out / 'training_data.csv', index=False)
        # case_000000 came from the old queue under unchanged inputs: old formula, inferred
        legacy = s.legacy_physical_signature(self.c)
        (self.out / 'pending_evaluations.json').write_text(json.dumps({'version': 1, 'entries': [
            {'run_id': 'case_000000', 'signature': legacy, 'x': [], 'arguments': {}, 'selected': {},
             'status': 'success', 'finalized': True}]}))
        loaded = b.load_training(self.c)
        found = s.row_signatures(self.c, loaded).tolist()
        self.assertEqual(found[0], legacy)
        self.assertTrue(all(s.is_unsigned(signature) for signature in found[1:]))
        self.assertEqual(len(set(found[1:])), 3)
        self.assertEqual(len(r.training_partition(self.c, loaded)[0]), 1)
        with self.assertRaises(s.InputsChanged) as caught:
            b.run_loop(self.run_args())
        self.assertIn('written before signatures existed', str(caught.exception))
        with patch.object(b, 'evaluate_true_cfd', side_effect=self.complete), redirect_stdout(StringIO()):
            b.run_loop(self.run_args('--accept-input-change', 'June runs used the same templates'))
        self.assertEqual(len(r.training_partition(self.c, b.load_training(self.c))[0]), 5)

    def test_accepting_unsigned_rows_covers_only_the_reviewed_rows(self):
        self.rows(2)
        frame = pd.read_csv(self.out / 'training_data.csv').drop(columns=[s.COLUMN])
        frame.to_csv(self.out / 'training_data.csv', index=False)
        with redirect_stdout(StringIO()):
            s.check(self.c, s.training_records(self.c, b.load_training(self.c)), 'reviewed case 0 and 1')
        self.assertTrue(s.evidence_mask(self.c, b.load_training(self.c)).all())
        # a later restored/imported unsigned row, and an edited copy of a reviewed one, are not covered
        reviewed = pd.read_csv(self.out / 'training_data.csv')
        extra = reviewed.iloc[[0]].assign(run_id='case_000099')
        edited = reviewed.iloc[[1]].assign(Efficiency=.99)
        pd.concat([reviewed.iloc[[0]], edited, extra]).to_csv(self.out / 'training_data.csv', index=False)
        loaded = b.load_training(self.c)
        self.assertEqual(s.evidence_mask(self.c, loaded).tolist(), [True, False, False])
        with self.assertRaises(s.InputsChanged) as caught:
            b.run_loop(self.run_args())
        self.assertIn('2 record(s) [case_000001, case_000099]: written before signatures existed',
                      str(caught.exception))

    def test_unfinished_entry_with_old_formula_resumes_under_unchanged_inputs(self):
        self.rows(4)
        x = self.x.copy()
        x[0] = .9
        queue = {'version': 1, 'entries': [{
            'run_id': 'case_000004', 'signature': s.legacy_physical_signature(self.c), 'x': x.tolist(),
            'arguments': {'sample_phase': 'doe', 'doe_index': 4}, 'selected': {}, 'status': 'pending',
            'finalized': False}]}
        (self.out / 'pending_evaluations.json').write_text(json.dumps(queue))
        args = self.run_args()
        args.iterations = 0
        with patch.object(b, 'evaluate_true_cfd', side_effect=self.complete) as evaluate, \
                redirect_stdout(StringIO()):
            b.run_loop(args)
        self.assertEqual(evaluate.call_count, 1)
        # The old formula also hashed search settings, whose old values are unknown:
        # after a search change such a record needs one acceptance. New records do not.
        self.c['search']['slice_tolerance_norm'] = 1e-6
        self.assertNotIn(queue['entries'][0]['signature'], s.compatible(self.c))
        self.assertTrue(s.evidence_mask(self.c, b.load_training(self.c)).iloc[-1])

    def test_boundary_plan_change_can_be_accepted(self):
        self.rows(1)
        plan_path = self.out / 'plan.json'
        baseline = b.BaselineShape(np.ones(5), np.ones(5), np.linspace(0, 1, 5), np.linspace(0, 1, 5), 0., 0.)
        self.c['refinement'].update(boundary_variables=[b.variable_names(self.c)[i] for i in [4, 5, 0, 10]],
                                    extension={'variable': b.variable_names(self.c)[4], 'value': 5.0})
        with patch.object(b, 'extract_baseline', return_value=baseline):
            plan = r.write_boundary_plan(self.c, 'case_000000', plan_path)
            changed = copy.deepcopy(self.c)
            changed['runtime']['rpm'] += 100
            with self.assertRaises(s.InputsChanged) as caught:
                r.run_boundary(changed, plan_path, 'singles', 1, False)
            self.assertIn(f"plan {plan['plan_id'][:12]}", str(caught.exception))
            with patch.object(b, 'evaluate_true_cfd', side_effect=self.complete), redirect_stdout(StringIO()):
                result = r.run_boundary(changed, plan_path, 'singles', 1, False, 'rpm label corrected to template')
        self.assertEqual(result['new_cfd_attempts'], 1)

    # ------------------------------------------------------ case receipts
    def cfx_kw(self, work):
        (work / 'Impeller_Mesh.gtm').write_text('synthetic mesh')
        for name in ['template.cfx', 'extract.cse']:
            (work / name).write_text('synthetic template')
        bindir = work / 'bin'
        bindir.mkdir(exist_ok=True)
        for name in ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe']:
            (bindir / name).write_text('never executed')
        return dict(p_out_pa=10, cfx_bin_dir=bindir, template_cfx=work / 'template.cfx',
                    template_cse=work / 'extract.cse')

    def test_cfx_cores_and_install_are_recorded_not_signed(self):
        work = self.out / 'case'
        work.mkdir()
        kw = self.cfx_kw(work)
        calls = []

        def command(cmd, cwd, log):
            name = Path(cmd[0]).name
            calls.append(name)
            if name == 'cfx5pre.exe':
                (work / 'Impeller.def').write_text('def')
            elif name == 'cfx5solve.exe':
                (work / 'run.res').write_text('res')
                (work / 'run.out').write_text(cfx_output())
            else:
                (work / 'CFX_Results.txt').write_text('0.76,2,1,.42,2.1')
            return 0
        with patch.object(cfx, '_run_logged', side_effect=command):
            self.assertTrue(cfx.run_cfx_pipeline(work, 'case_0', cores=8, **kw).success)
            moved = work / 'bin2'
            moved.mkdir()
            for name in ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe']:
                (moved / name).write_text('never executed')
            self.assertTrue(cfx.run_cfx_pipeline(work, 'case_0', cores=4, **{**kw, 'cfx_bin_dir': moved}).success)
        self.assertEqual(calls, ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe'])
        state = json.loads((work / 'cfx_state.json').read_text())
        self.assertEqual(state['environments'], [{'cores': 8, 'cfx_bin_dir': str(kw['cfx_bin_dir'])},
                                                 {'cores': 4, 'cfx_bin_dir': str(moved)}])

    def test_cfx_receipt_written_before_the_change_is_still_accepted(self):
        work = self.out / 'case'
        work.mkdir()
        kw = self.cfx_kw(work)

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
            self.assertTrue(cfx.run_cfx_pipeline(work, 'case_0', **kw).success)
        # rewrite the receipt in the pre-2026-10 shape: cores and install path signed, no release
        state = json.loads((work / 'cfx_state.json').read_text())
        state['inputs'].pop('solver')
        state['inputs'].update(cores=8, cfx_bin_dir=str(kw['cfx_bin_dir']))
        state['signature'] = cfx.digest(state['inputs'])
        state.pop('environments')
        (work / 'cfx_state.json').write_text(json.dumps(state))
        with patch.object(cfx, '_run_logged', side_effect=AssertionError('no external work')):
            self.assertTrue(cfx.run_cfx_pipeline(work, 'case_0', cores=12, **kw).success)
            changed = cfx.run_cfx_pipeline(work, 'case_0', **{**kw, 'p_out_pa': 11})
            # such a receipt does not record the release: another install path is not trusted
            other = work / 'other_bin'
            other.mkdir()
            moved = cfx.run_cfx_pipeline(work, 'case_0', **{**kw, 'cfx_bin_dir': other})
        self.assertIn('identity changed', changed.message)
        self.assertIn('identity changed', moved.message)

    def test_another_cfx_release_invalidates_results_and_the_physical_signature(self):
        work = self.out / 'case'
        work.mkdir()
        kw = self.cfx_kw(work)

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

        def install(path, solver):
            path.mkdir(parents=True)
            for name in ['cfx5pre.exe', 'cfx5solve.exe', 'cfx5post.exe']:
                (path / name).write_text(solver if name == 'cfx5solve.exe' else 'never executed')
            return path
        v251 = install(work / 'ANSYS Inc' / 'v251' / 'CFX' / 'bin', 'solver 2025 R1')
        moved = install(work / 'D' / 'ANSYS Inc' / 'v251' / 'CFX' / 'bin', 'solver 2025 R1')
        v252 = install(work / 'ANSYS Inc' / 'v252' / 'CFX' / 'bin', 'solver 2025 R2')
        patched = install(work / 'E' / 'ANSYS Inc' / 'v251' / 'CFX' / 'bin', 'solver 2025 R1 patched')
        with patch.object(cfx, '_run_logged', side_effect=command):
            self.assertTrue(cfx.run_cfx_pipeline(work, 'case_0', **{**kw, 'cfx_bin_dir': v251}).success)
        with patch.object(cfx, '_run_logged', side_effect=AssertionError('no external work')):
            self.assertTrue(cfx.run_cfx_pipeline(work, 'case_0', **{**kw, 'cfx_bin_dir': moved}).success)
            for other in (v252, patched):
                result = cfx.run_cfx_pipeline(work, 'case_0', **{**kw, 'cfx_bin_dir': other})
                self.assertIn('identity changed', result.message, other)
        signature = lambda path: s.physical_signature({**self.c, 'paths': {**self.c['paths'], 'cfx_bin_dir': str(path)}})
        self.assertEqual(signature(v251), signature(moved))
        self.assertNotEqual(signature(v251), signature(v252))
        self.assertNotEqual(signature(v251), signature(patched))
        self.rows(2, config={**self.c, 'paths': {**self.c['paths'], 'cfx_bin_dir': str(v251)}})
        self.c['paths']['cfx_bin_dir'] = str(v252)
        with self.assertRaises(s.InputsChanged) as caught:
            b.run_loop(self.run_args())
        self.assertIn("solver.release: 'v251' -> 'v252'", str(caught.exception))

    def test_geometry_receipt_from_old_formula_is_reused(self):
        baseline = b.BaselineShape(np.ones(5), np.ones(5), np.linspace(0, 1, 5), np.linspace(0, 1, 5), 0., 0.)
        candidate = b.write_candidate_files(self.c, baseline, self.x, 'case_000000')
        case = candidate.parent
        (case / 'Impeller_Mesh.gtm').write_text('mesh')
        old = r.digest({'physical': s.legacy_physical_signature(self.c), 'x': self.x.tolist(),
                        'candidate': b.file_identity(candidate)})
        (case / 'geometry_state.json').write_text(json.dumps(
            {'signature': old, 'status': 'complete', 'mesh': b.file_identity(case / 'Impeller_Mesh.gtm')}))

        class Outcome:
            success = True
            message = 'synthetic'
            failure_stage = ''
            metrics = {'Efficiency': .76, 'MassFlow': 4.2, 'PressureRatio': 2., 'Power': 1., 'totalpressureratio': 2.}
        with patch.object(b, 'run_geometry', side_effect=AssertionError('geometry must be reused')), \
                patch.object(b, 'run_cfx_pipeline', return_value=Outcome()):
            result = b.evaluate_true_cfd(self.c, baseline, self.x, 0, sample_phase='doe')
        self.assertTrue(result.success, result.row['message'])

    # --------------------------------------------------------------- GUI
    def test_gui_passes_a_reason_only_when_given(self):
        self.assertNotIn('--accept-input-change', commands.build_run('c.json', accept_input_change='  ').args)
        self.assertEqual(commands.build_run('c.json', accept_input_change=' same CFD ').args[-2:],
                         ['--accept-input-change', 'same CFD'])
        spec = commands.build_run_boundary('c.json', plan='p.json', stage='singles', max_new_cfd=1,
                                           accept_input_change='same CFD')
        self.assertEqual(spec.args[-2:], ['--accept-input-change', 'same CFD'])
        args = b.build_parser().parse_args(spec.args)
        self.assertEqual(args.accept_input_change, 'same CFD')


    def test_gui_project_counts_rows_that_would_stop_a_run(self):
        from blade_gui.project import Project
        self.rows(3)
        self.cp.write_text(json.dumps(self.c))
        project = Project(self.cp)
        self.assertEqual(project.records_from_other_inputs(), 0)
        self.c['runtime']['p_out_pa'] += 1
        self.cp.write_text(json.dumps(self.c))
        project.reload()
        self.assertEqual(project.records_from_other_inputs(), 3)


    def test_gui_result_views_use_only_rows_under_current_inputs(self):
        from blade_gui.project import Project
        self.rows(3)  # old inputs; the best of these written into the fronts
        b.write_pareto(self.c, b.load_training(self.c))
        written = pd.read_csv(self.out / 'pareto_front.csv')
        self.c['runtime']['p_out_pa'] += 1
        for i, (efficiency, flow) in enumerate([(.750, 4.0), (.751, 3.9)], start=3):
            x = self.x.copy()
            x[1] += .1 * i
            b.append_row(self.c, {**b.vector_to_sample(self.c, x), 'run_id': f'case_{i:06d}', 'status': 'success',
                                  'sample_phase': 'active_learning', 'Efficiency': efficiency, 'MassFlow': flow})
        self.cp.write_text(json.dumps(self.c))
        project = Project(self.cp)
        self.assertEqual(len(project.training()), 5)  # case history keeps every row
        self.assertEqual(project.results().run_id.tolist(), ['case_000003', 'case_000004'])
        summary = project.summary()
        self.assertEqual((summary.attempts, summary.other_inputs), (5, 3))
        self.assertEqual((summary.best_efficiency, summary.best_run_id), (.751, 'case_000004'))
        self.assertEqual(len(project.best_so_far()[0]), 2)
        # the stale written front lists old-input rows; the view recomputes it
        self.assertTrue(set(written.run_id) <= {'case_000000', 'case_000001', 'case_000002'})
        self.assertEqual(sorted(project.pareto().run_id), ['case_000003', 'case_000004'])
        self.assertEqual(sorted(project.pareto_strict().run_id), ['case_000003', 'case_000004'])
        # with nothing excluded, the written front is shown as is
        b.write_pareto(self.c, b.load_training(self.c))
        self.c['runtime']['p_out_pa'] -= 1
        self.cp.write_text(json.dumps(self.c))
        project.reload()
        self.assertEqual(project.summary().other_inputs, 2)
        with redirect_stdout(StringIO()):
            s.check(self.c, s.training_records(self.c, b.load_training(self.c)), 'same CFD')
        project.reload()
        self.assertEqual(project.records_from_other_inputs(), 0)
        pd.testing.assert_frame_equal(project.pareto(), pd.read_csv(self.out / 'pareto_front.csv'))


if __name__ == '__main__':
    unittest.main()
