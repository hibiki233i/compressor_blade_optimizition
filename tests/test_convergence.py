from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blade_shape_convergence import ConvergencePolicy, ReviewPolicy, assess_out, convergence_ccl, review_evidence
import blade_shape_cfx_runner as cfx
from tests.cfx_output_samples import (NEAR_CONVERGED_ITERATION, output, outer_iteration_limit_output,
                                      p_mass_output, real_output)


class ResidualTests(unittest.TestCase):
    def test_final_rms_not_max_or_best_history(self):
        p=ConvergencePolicy()
        self.assertTrue(assess_out(output(),p)['converged'])
        result=assess_out(output()+output('2E-4',200,True),p)
        self.assertFalse(result['converged']);self.assertTrue(result['iteration_limit_reached'])
        self.assertEqual(result['iteration'],200)
        self.assertTrue(assess_out(output('1E-5'),p)['converged'])

    def test_missing_truncated_nonfinite_rejected(self):
        for text in [output().replace('CFD Solver finished','broken'),output('NaN'),
                     output().replace('H-Energy','Other'),output()+'OUTER LOOP ITERATION = 101\n']:
            with self.subTest(text=text),self.assertRaises(ValueError):assess_out(text,ConvergencePolicy())

    def test_real_cfx_layout_with_notice_boxes_inside_residual_table(self):
        result=assess_out(real_output(),ConvergencePolicy())
        self.assertEqual(result['iteration'],138)
        self.assertEqual(result['rms'],{'u-mom':4.9e-6,'v-mom':6.2e-6,'w-mom':9.9e-6,'p-mass':3.4e-6,
                                        'h-energy':3.7e-6,'k-turbke':7.5e-6,'o-turbfreq':2.2e-5})
        # Accepted like CFX itself: turbulence (O-TurbFreq 2.2E-05) is recorded, not gated.
        self.assertTrue(result['converged']);self.assertFalse(result['iteration_limit_reached'])
        self.assertEqual(result['max_rms'],9.9e-6)
        self.assertEqual(result['checked_equations'],['h-energy','p-mass','u-mom','v-mom','w-mom'])
        self.assertTrue(assess_out(real_output('3.0E-03'),ConvergencePolicy())['converged'])

    def test_main_equation_above_target_is_not_converged(self):
        text=real_output().replace('| 0.98 | 9.9E-06 |','| 0.98 | 1.2E-05 |')
        result=assess_out(text,ConvergencePolicy())
        self.assertFalse(result['converged']);self.assertEqual(result['max_rms'],1.2e-5)

    def test_residual_table_cut_before_required_equations_is_rejected(self):
        text=real_output()
        text=text[:text.index(' +----------------------+------+---------+---------+------------------+\n | H-Energy')]+'\n'+text[text.index(' CFD Solver finished'):]
        with self.assertRaisesRegex(ValueError,'Missing final RMS equations'):
            assess_out(text,ConvergencePolicy())

    def test_policy_and_additional_budget(self):
        for extra in [1499,2001,1500.5]:
            with self.assertRaises(ValueError):ConvergencePolicy(restart_iterations=extra)
        with self.assertRaises(ValueError):ConvergencePolicy(rms_target=1e-4)
        ccl=convergence_ccl(ConvergencePolicy(restart_iterations=1500),restart=True)
        self.assertIn('Maximum Number of Iterations = 1500',ccl)
        self.assertIn('Residual Type = RMS',ccl)

    def test_time_limit_message_without_iteration_budget_exhaustion_is_not_restartable(self):
        text=output('2E-4',100,True).replace('Maximum Number of Iterations = 100','Maximum Number of Iterations = 2000')
        self.assertFalse(assess_out(text,ConvergencePolicy())['iteration_limit_reached'])

    def test_real_outer_iteration_limit_summary_requires_exhausted_budget(self):
        text=outer_iteration_limit_output('1.4E-4')
        result=assess_out(text,ConvergencePolicy())
        self.assertFalse(result['converged'])
        self.assertTrue(result['iteration_limit_reached'])
        self.assertEqual(result['run_iteration'],2000)
        self.assertEqual(result['declared_iteration_limit'],2000)
        for other in [outer_iteration_limit_output(iteration=100),
                      text.replace('Maximum number of outer iterations','Maximum elapsed time'),
                      text.replace('Run duration reached','User interrupt'),
                      text.replace('Maximum Number of Iterations = 2000\n','')]:
            with self.subTest(text=other):
                self.assertFalse(assess_out(other,ConvergencePolicy())['iteration_limit_reached'])


class ReviewClassificationTests(unittest.TestCase):
    def evidence(self, text, review=ReviewPolicy()):
        return review_evidence(assess_out(text, ConvergencePolicy()), ConvergencePolicy(), review)

    def test_real_near_converged_table_is_offered_for_review_with_its_backflow(self):
        report = assess_out(NEAR_CONVERGED_ITERATION, ConvergencePolicy())
        self.assertFalse(report['converged'])
        # Wallscale at exactly 1.0E-05 meets the target; only P-Mass is above it.
        self.assertEqual(review_evidence(report, ConvergencePolicy(), ReviewPolicy())['equations_above_target'],
                         {'p-mass': 1.3e-05})
        self.assertEqual(report['wall_notices'], [
            {'side': 'inlet', 'boundary': 'R1 Inlet', 'faces_percent': 3.6, 'area_percent': 0.2},
            {'side': 'outlet', 'boundary': 'R1 Outlet', 'faces_percent': 15.8, 'area_percent': 1.9}])

    def test_review_needs_few_equations_above_target_and_all_within_the_limit(self):
        self.assertIsNotNone(self.evidence(output('2.0E-06', 2000, True, {
            'U - Mom': '2E-5', 'P - Mass': '9E-5', 'TurbFreq': '3E-5'})))
        self.assertIsNone(self.evidence(output('2.0E-06', 2000, True, {
            'U - Mom': '2E-5', 'V - Mom': '2E-5', 'P - Mass': '9E-5', 'TurbFreq': '3E-5'})), 'four above target')
        self.assertIsNone(self.evidence(p_mass_output('1.1E-04')), 'above the review limit')
        self.assertIsNone(self.evidence(output()), 'converged results need no review')
        self.assertIsNone(self.evidence(p_mass_output(), ReviewPolicy(max_equations_above_target=0)), 'disabled')
        self.assertIsNone(self.evidence(p_mass_output('5E-5'), ReviewPolicy(rms_limit=4e-5)))

    def test_review_policy_may_only_tighten(self):
        for bad in [dict(rms_limit=2e-4), dict(rms_limit=0), dict(max_equations_above_target=-1),
                    dict(max_equations_above_target=1.5)]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ReviewPolicy(**bad)
        self.assertEqual(ReviewPolicy.from_config({}), ReviewPolicy())


class PipelineConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for name in ['Impeller_Mesh.gtm','template.cfx','extract.cse','cfx5pre.exe','cfx5solve.exe','cfx5post.exe']:
            (self.root/name).write_text('synthetic')
        self.kw=dict(p_out_pa=10,cfx_bin_dir=self.root,template_cfx=self.root/'template.cfx',template_cse=self.root/'extract.cse')

    def run_case(self,residuals,*,exhausted_output=output,initial_iteration=100,restart_iteration=2000,review=None):
        calls=[];solves=0
        def command(cmd,cwd,log):
            nonlocal solves
            name=Path(cmd[0]).name;calls.append(cmd)
            if name=='cfx5pre.exe':(cwd/'Impeller.def').write_text('def')
            elif name=='cfx5solve.exe':
                rms,limit=residuals[solves];solves+=1
                (cwd/f'Impeller_{solves:03}.res').write_text(f'result{solves}')
                render=exhausted_output if limit else output
                iteration=restart_iteration if solves>1 else initial_iteration
                text=render(rms,iteration,limit)
                if solves>1:  # continued history: accumulated count, this run's in parentheses
                    text=text.replace(f'OUTER LOOP ITERATION = {iteration}\n',
                                      f'OUTER LOOP ITERATION = {initial_iteration+iteration} ({iteration})\n')
                (cwd/f'Impeller_{solves:03}.out').write_text(text)
            else:(cwd/'CFX_Results.txt').write_text('.76,2,1,.42,2.1')
            return 0
        with patch.object(cfx,'_run_logged',side_effect=command):
            result=cfx.run_cfx_pipeline(self.root,'case_0',review=review,**self.kw)
        return result,calls

    def test_one_restart_then_accept_and_recover_without_resolve(self):
        result,calls=self.run_case([('2E-4',True),('2E-6',False)])
        self.assertTrue(result.success)
        solve=[c for c in calls if Path(c[0]).name=='cfx5solve.exe']
        self.assertEqual(len(solve),2);self.assertIn('-continue-from-file',solve[1])
        self.assertNotIn('-initial-file',solve[1])
        self.assertTrue(solve[1][-1].endswith('Impeller_001.res'))
        with patch.object(cfx,'_run_logged',side_effect=AssertionError('unexpected external work')):
            self.assertTrue(cfx.run_cfx_pipeline(self.root,'case_0',**self.kw).success)
        state=json.loads((self.root/'cfx_state.json').read_text())
        self.assertEqual(len(state['solve_attempts']),2)
        self.assertTrue(state['stages']['solve']['convergence']['converged'])
        (self.root/'Impeller_002.out').write_text('changed')
        self.assertFalse(cfx.run_cfx_pipeline(self.root,'case_0',**self.kw).success)

    def test_second_failure_discarded_without_post_or_retry(self):
        result,calls=self.run_case([('2E-4',True),('2E-4',True)])
        self.assertFalse(result.success);self.assertEqual(result.failure_stage,'solve')
        self.assertFalse(any(Path(c[0]).name=='cfx5post.exe' for c in calls))
        with patch.object(cfx,'_run_logged',side_effect=AssertionError('retry')):
            self.assertFalse(cfx.run_cfx_pipeline(self.root,'case_0',**self.kw).success)

    def test_non_limit_stop_does_not_trigger_restart(self):
        result,calls=self.run_case([('2E-4',False)])
        self.assertFalse(result.success)
        self.assertEqual(sum(Path(c[0]).name=='cfx5solve.exe' for c in calls),1)

    def test_real_outer_iteration_limit_summary_restarts_then_accepts(self):
        result,calls=self.run_case([('1.4E-4',True),('2E-6',False)],
                                  exhausted_output=outer_iteration_limit_output,initial_iteration=2000)
        self.assertTrue(result.success)
        solve=[c for c in calls if Path(c[0]).name=='cfx5solve.exe']
        self.assertEqual(len(solve),2)
        self.assertIn('-continue-from-file',solve[1])
        self.assertTrue(solve[1][-1].endswith('Impeller_001.res'))
        state=json.loads((self.root/'cfx_state.json').read_text())
        self.assertEqual(state['restart']['iterations'],2000)
        self.assertEqual(state['restart']['method'],'continue_from_file')
        self.assertEqual(len(state['solve_attempts']),2)
        final=state['stages']['solve']['convergence']
        self.assertEqual((final['iteration'],final['run_iteration']),(4000,2000))

    def test_restart_close_to_the_target_is_post_processed_for_review(self):
        result,calls=self.run_case([('1.3E-05',True),('1.3E-05',True)],exhausted_output=p_mass_output,
                                  initial_iteration=2000,review=ReviewPolicy())
        self.assertFalse(result.success);self.assertTrue(result.review);self.assertEqual(result.failure_stage,'review')
        self.assertIn('p-mass',result.message)
        self.assertAlmostEqual(result.metrics['Efficiency'],.76)
        self.assertEqual(sum(Path(c[0]).name=='cfx5solve.exe' for c in calls),2)
        state=json.loads((self.root/'cfx_state.json').read_text())
        self.assertEqual(state['stages']['solve']['status'],'review')
        self.assertEqual(state['stages']['solve']['review']['equations_above_target'],{'p-mass':1.3e-5})
        # A resume returns the same verdict without new solver or Post work.
        with patch.object(cfx,'_run_logged',side_effect=AssertionError('unexpected external work')):
            again=cfx.run_cfx_pipeline(self.root,'case_0',review=ReviewPolicy(),**self.kw)
        self.assertTrue(again.review);self.assertEqual(again.metrics,result.metrics)
        (self.root/'Impeller_002.out').write_text('changed')
        self.assertFalse(cfx.run_cfx_pipeline(self.root,'case_0',review=ReviewPolicy(),**self.kw).review)

    def test_without_a_review_policy_a_close_restart_is_still_discarded(self):
        result,calls=self.run_case([('1.3E-05',True),('1.3E-05',True)],exhausted_output=p_mass_output,
                                  initial_iteration=2000)
        self.assertFalse(result.success);self.assertFalse(result.review)
        self.assertFalse(any(Path(c[0]).name=='cfx5post.exe' for c in calls))

    def test_only_a_restart_that_used_its_budget_is_offered_for_review(self):
        # an abnormal first stop (only P-Mass above, no iteration-limit stop) gets no restart and no review
        result,calls=self.run_case([('1.3E-05',True)],exhausted_output=lambda r,i,e:p_mass_output(r,i,False),
                                   review=ReviewPolicy())
        self.assertFalse(result.review);self.assertIn('not a normal iteration-limit stop',result.message)
        self.assertEqual(sum(Path(c[0]).name=='cfx5solve.exe' for c in calls),1)
        # a continued run cut short by an accumulated iteration limit did not use its budget
        def cut_short(rms,iteration,exhausted):
            return outer_iteration_limit_output('2.0E-06',iteration).replace(
                '| P - Mass             | 0.90 | 2.0E-06 |','| P - Mass             | 0.90 | 1.3E-05 |')
        self.tmp.cleanup();self.setUp()
        result,_=self.run_case([('x',True),('x',True)],exhausted_output=cut_short,initial_iteration=2000,
                               restart_iteration=1,review=ReviewPolicy())
        self.assertFalse(result.review);self.assertIn('continued history',result.message)

    def test_converged_restart_is_accepted_even_if_the_limit_was_also_reported(self):
        result,_=self.run_case([('1.4E-4',True),('2E-6',True)],exhausted_output=outer_iteration_limit_output,
                               initial_iteration=2000,restart_iteration=1)
        self.assertTrue(result.success)

    def test_restart_cut_short_by_an_accumulated_iteration_limit_is_reported(self):
        # If CFX counted Maximum Number of Iterations over the continued history,
        # a run that already used 2000 would stop at once: 2001 (1).
        result,calls=self.run_case([('1.4E-4',True),('1.4E-4',True)],exhausted_output=outer_iteration_limit_output,
                                  initial_iteration=2000,restart_iteration=1)
        self.assertFalse(result.success);self.assertEqual(result.failure_stage,'solve')
        self.assertIn('counted Maximum Number of Iterations over the continued history',result.message)
        self.assertFalse(any(Path(c[0]).name=='cfx5post.exe' for c in calls))
        with patch.object(cfx,'_run_logged',side_effect=AssertionError('must not retry failed CFD')):
            self.assertFalse(cfx.run_cfx_pipeline(self.root,'case_0',**self.kw).success)

    def test_real_outer_iteration_limit_summary_still_discards_second_failure(self):
        result,calls=self.run_case([('1.4E-4',True),('1.4E-4',True)],
                                  exhausted_output=outer_iteration_limit_output,initial_iteration=2000)
        self.assertFalse(result.success)
        self.assertEqual(result.failure_stage,'solve')
        self.assertEqual(sum(Path(c[0]).name=='cfx5solve.exe' for c in calls),2)
        self.assertFalse(any(Path(c[0]).name=='cfx5post.exe' for c in calls))
        with patch.object(cfx,'_run_logged',side_effect=AssertionError('must not retry failed CFD')):
            self.assertFalse(cfx.run_cfx_pipeline(self.root,'case_0',**self.kw).success)

    def test_initial_convergence_requires_only_one_solve(self):
        result,calls=self.run_case([('1E-6',False)])
        self.assertTrue(result.success)
        self.assertEqual(sum(Path(c[0]).name=='cfx5solve.exe' for c in calls),1)

    def test_interrupted_restart_is_not_automatically_repeated(self):
        count=0
        def command(cmd,cwd,log):
            nonlocal count
            if Path(cmd[0]).name=='cfx5pre.exe':
                (cwd/'Impeller.def').write_text('def')
            elif Path(cmd[0]).name=='cfx5solve.exe':
                count+=1
                if count==2:raise KeyboardInterrupt('during restart')
                (cwd/'Impeller_001.res').write_text('res')
                (cwd/'Impeller_001.out').write_text(output('2E-4',100,True))
            else:self.fail('Unconverged point reached Post')
            return 0
        with patch.object(cfx,'_run_logged',side_effect=command):
            with self.assertRaises(KeyboardInterrupt):cfx.run_cfx_pipeline(self.root,'case_0',**self.kw)
        with patch.object(cfx,'_run_logged',side_effect=AssertionError('duplicate continuation')):
            self.assertFalse(cfx.run_cfx_pipeline(self.root,'case_0',**self.kw).success)

    def test_missing_out_does_not_reach_post_or_restart(self):
        def command(cmd,cwd,log):
            if Path(cmd[0]).name=='cfx5pre.exe':(cwd/'Impeller.def').write_text('def')
            elif Path(cmd[0]).name=='cfx5solve.exe':(cwd/'Impeller_001.res').write_text('res')
            else:self.fail('Missing RMS evidence reached Post')
            return 0
        with patch.object(cfx,'_run_logged',side_effect=command):
            result=cfx.run_cfx_pipeline(self.root,'case_0',**self.kw)
        self.assertFalse(result.success);self.assertEqual(result.failure_stage,'solve')


if __name__=='__main__':unittest.main()
