from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blade_shape_convergence import ConvergencePolicy, assess_out, convergence_ccl
import blade_shape_cfx_runner as cfx
from tests.cfx_output_samples import output, real_output


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


class PipelineConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for name in ['Impeller_Mesh.gtm','template.cfx','extract.cse','cfx5pre.exe','cfx5solve.exe','cfx5post.exe']:
            (self.root/name).write_text('synthetic')
        self.kw=dict(p_out_pa=10,cfx_bin_dir=self.root,template_cfx=self.root/'template.cfx',template_cse=self.root/'extract.cse')

    def run_case(self,residuals):
        calls=[];solves=0
        def command(cmd,cwd,log):
            nonlocal solves
            name=Path(cmd[0]).name;calls.append(cmd)
            if name=='cfx5pre.exe':(cwd/'Impeller.def').write_text('def')
            elif name=='cfx5solve.exe':
                rms,limit=residuals[solves];solves+=1
                (cwd/f'Impeller_{solves:03}.res').write_text(f'result{solves}')
                (cwd/f'Impeller_{solves:03}.out').write_text(output(rms,2000 if solves>1 else 100,limit))
            else:(cwd/'CFX_Results.txt').write_text('.76,2,1,.42,2.1')
            return 0
        with patch.object(cfx,'_run_logged',side_effect=command):
            result=cfx.run_cfx_pipeline(self.root,'case_0',**self.kw)
        return result,calls

    def test_one_restart_then_accept_and_recover_without_resolve(self):
        result,calls=self.run_case([('2E-4',True),('2E-6',False)])
        self.assertTrue(result.success)
        solve=[c for c in calls if Path(c[0]).name=='cfx5solve.exe']
        self.assertEqual(len(solve),2);self.assertIn('-initial-file',solve[1])
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
