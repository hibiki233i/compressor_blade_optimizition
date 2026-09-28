from __future__ import annotations
import copy,importlib,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import blade_shape_active_learning as b
import blade_shape_refinement as r
import blade_shape_cfx_runner as cfx
from tests.cfx_output_samples import output as cfx_output

class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.out=Path(self.tmp.name)
        self.c=json.loads(Path('blade_shape_config.json').read_text())
        self.c['paths']['output_dir']=str(self.out)
        self.c['paths']['cft_batch_template']=str(Path('tests/fixtures/synthetic_meanline.cft-batch').resolve())
        self.c['paths']['powershell_exe']=str(self.out/'missing_powershell')
        self.c['surrogate'].update(model='rbf',ehvi_y_samples=16,ehvi_validation_samples=32)
        self.c['runtime'].update(initial_samples=0,nsga2_pop_size=6,nsga2_generations=1,candidate_pool_size=8)
        self.x=r.enforce_fixed(self.c,np.zeros(12));self.x[4]=2.
        self.c['refinement'].pop('boundary_levels',None)
        self.c['refinement'].pop('local_search',None)
        self.cp=self.out/'config.json'
    def rows(self,n=1):
        for i in range(n):
            x=self.x.copy();x[0]+=.1*i
            b.append_row(self.c,{**b.vector_to_sample(self.c,x),'run_id':f'case_{i:06d}','status':'success',
                                'sample_phase':'active_learning','al_iteration':0,'Efficiency':.760+.0001*i,'MassFlow':4.2+.01*i})
        self.cp.write_text(json.dumps(self.c))
    def cfx_setup(self):
        (self.out/'Impeller_Mesh.gtm').write_text('synthetic mesh')
        for n in ['template.cfx','extract.cse']:(self.out/n).write_text('synthetic template')
        bindir=self.out/'bin';bindir.mkdir()
        for n in ['cfx5pre.exe','cfx5solve.exe','cfx5post.exe']:(bindir/n).write_text('never executed')
        return dict(p_out_pa=10,cfx_bin_dir=bindir,template_cfx=self.out/'template.cfx',template_cse=self.out/'extract.cse')
    def test_failed_solve_residue_is_not_accepted(self):
        kw=self.cfx_setup();calls=[]
        def command(cmd,cwd,log):
            name=Path(cmd[0]).name;calls.append(name)
            if name=='cfx5pre.exe':(self.out/'Impeller.def').write_text('def');return 0
            if name=='cfx5solve.exe':(self.out/'partial.res').write_text('partial');return 1
            (self.out/'CFX_Results.txt').write_text('0.76,2,1,.42,2.1');return 0
        with patch.object(cfx,'_run_logged',side_effect=command):
            first=cfx.run_cfx_pipeline(self.out,'case_0',**kw);second=cfx.run_cfx_pipeline(self.out,'case_0',**kw)
        self.assertFalse(first.success);self.assertFalse(second.success,'failed .res was accepted as success')
        self.assertNotIn('cfx5post.exe',calls)
    def test_verified_solve_resumes_post_and_rejects_changed_inputs(self):
        kw=self.cfx_setup();calls=[]
        def command(cmd,cwd,log):
            name=Path(cmd[0]).name;calls.append(name)
            if name=='cfx5pre.exe':(self.out/'Impeller.def').write_text('def');return 0
            if name=='cfx5solve.exe':(self.out/'complete.res').write_text('res');(self.out/'complete.out').write_text(cfx_output());return 0
            (self.out/'CFX_Results.txt').write_text('0.76,2,1,.42,2.1');return 0
        with patch.object(cfx,'_run_logged',side_effect=command):
            self.assertTrue(cfx.run_cfx_pipeline(self.out,'case_0',**kw).success)
            self.assertTrue(cfx.run_cfx_pipeline(self.out,'case_0',**kw).success)
            kw['p_out_pa']=11
            self.assertFalse(cfx.run_cfx_pipeline(self.out,'case_0',**kw).success,'changed BC reused old results')
        self.assertEqual(calls,['cfx5pre.exe','cfx5solve.exe','cfx5post.exe'])
    def test_unverified_result_text_is_rejected(self):
        kw=self.cfx_setup();(self.out/'CFX_Results.txt').write_text('0.76,2,1,.42,2.1')
        self.assertFalse(cfx.run_cfx_pipeline(self.out,'case_0',**kw).success)
    def test_failed_boundary_command_is_nonzero(self):
        self.rows();p=self.out/'plan.json';r.write_boundary_plan(self.c,'case_000000',p)
        result=subprocess.run([sys.executable,'blade_shape_active_learning.py','run-boundary','--config',str(self.cp),
              '--plan',str(p),'--stage','singles','--max-new-cfd','1'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0,'failed stage reported successful exit')
        self.assertEqual(pd.read_csv(self.out/'training_data.csv').iloc[-1].status,'failed')
    def test_blocked_extension_keeps_singles_plan(self):
        self.rows();self.c['constraints']['max_beta_offset_deg']=4.6
        try:p=r.write_boundary_plan(self.c,'case_000000',self.out/'plan.json')
        except ValueError as e:self.fail(f'extension blocked valid singles: {e}')
        self.assertTrue(p['stages']['singles']['valid']);self.assertFalse(p['stages']['extension']['valid'])
    def test_predefined_point_survives_predictor_failure(self):
        self.rows(4);p=self.out/'plan.json';r.write_boundary_plan(self.c,'case_000000',p)
        def evaluate(config,baseline,x,index,**meta):
            row={**b.vector_to_sample(config,x),**meta,'run_id':f'case_{index:06d}','status':'success','Efficiency':.761,'MassFlow':4.22}
            b.append_row(config,row);return b.CaseResult(row,True)
        try:
            with patch.object(b,'fit_surrogate',side_effect=np.linalg.LinAlgError('test fitting failure')),patch.object(b,'evaluate_true_cfd',side_effect=evaluate):
                result=r.run_boundary(self.c,p,'singles',1,False)
        except np.linalg.LinAlgError as e:self.fail(f'prediction blocked predefined CFD: {e}')
        self.assertEqual(result['new_cfd_attempts'],1)
        self.assertEqual(len(b.load_training(self.c)),5)
    def test_equal_candidates_have_equal_ehvi_and_order_invariance(self):
        y=np.array([[.76,4.2],[.755,4.3]])
        m=np.tile([.761,4.25],(20,1));s=np.tile([.002,.03],(20,1))
        scores=b.approximate_expected_hvi(self.c,y,m,s)
        np.testing.assert_allclose(scores,scores[0],rtol=1e-12)
        m[:,0]+=np.arange(20)*.00001
        a=b.approximate_expected_hvi(self.c,y,m,s);z=b.approximate_expected_hvi(self.c,y,m[::-1],s[::-1])[::-1]
        np.testing.assert_allclose(a,z,rtol=1e-12)
    def test_pending_al_reuses_reserved_case_after_interruption(self):
        self.rows(4);args=b.build_parser().parse_args(['run','--config',str(self.cp),'--resume','--iterations','1','--batch-size','1','--max-new-cfd','1'])
        ids=[]
        def interrupted(config,baseline,x,index,**meta):
            ids.append(index);b.write_candidate_files(config,baseline,x,f'case_{index:06d}')
            raise KeyboardInterrupt('simulated crash before append')
        with patch.object(b,'evaluate_true_cfd',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):b.run_loop(args)
        def complete(config,baseline,x,index,**meta):
            ids.append(index);row={**b.vector_to_sample(config,x),**meta,'run_id':f'case_{index:06d}','status':'success','Efficiency':.762,'MassFlow':4.25}
            b.append_row(config,row);return b.CaseResult(row,True)
        with patch.object(b,'evaluate_true_cfd',side_effect=complete):b.run_loop(args)
        self.assertEqual(ids,[4,4])
        self.assertEqual(len(pd.read_csv(self.out/'active_learning_diagnostics.csv')),1)
    def test_local_trial_steps_do_not_collapse_at_upper_bound(self):
        self.x[4]=4.5;self.rows()
        names=self.c['refinement']['boundary_variables']
        self.c['refinement']['boundary_levels']={n:{'step_deg':.25,'min_step_deg':.05,'max_step_deg':.5} for n in names}
        try:p=r.write_boundary_plan(self.c,'case_000000',self.out/'plan.json')
        except ValueError as e:self.fail(f'cannot create inward local control: {e}')
        trial=next(t for t in p['points'] if t['point_id']==names[0])
        self.assertEqual(trial['x'][4],4.25)
    def test_process_lock_prevents_second_writer(self):
        self.assertIsNotNone(importlib.util.find_spec('blade_shape_runtime'),'runtime lock missing')
        from blade_shape_runtime import output_lock
        script="from blade_shape_runtime import output_lock; import sys\nwith output_lock(sys.argv[1]): print('entered')"
        with output_lock(self.out):
            q=subprocess.run([sys.executable,'-c',script,str(self.out)],capture_output=True,text=True)
        self.assertNotEqual(q.returncode,0)
        with output_lock(self.out):pass

    def test_exact_hvi_matches_hand_calculation(self):
        from blade_shape_acquisition import improvement_2d
        p=np.array([[2.,1.],[1.,2.]])
        out=improvement_2d(np.array([[2.,2.],[1.,1.],[3.,.5]]),p,np.zeros(2))
        np.testing.assert_allclose(out,[1.,0.,.5],atol=1e-12)
    def test_local_regions_and_pool_keep_fixed_values(self):
        self.rows(4)
        self.c['refinement']['local_search']={'enabled':True,'fraction':.75,'initial_radius_norm':.1,
          'min_radius_norm':.025,'max_radius_norm':.3,'successes_to_expand':2,'failures_to_shrink':2,'min_relative_hv_gain':.0001}
        self.assertTrue(hasattr(r,'local_regions'),'local search regions missing')
        regions=r.local_regions(self.c,b.load_training(self.c))
        pool,sources=r.candidate_pool(self.c,b.extract_baseline(self.c),b.load_training(self.c),42,regions)
        self.assertEqual(len(pool),len(sources));self.assertGreater(len(pool),0)
        self.assertTrue(r.same_slice_mask(self.c,pool).all())
        for x,source in zip(pool,sources):
            if source=='local_pool':
                self.assertTrue(any(np.all(x>=q['lower']) and np.all(x<=q['upper']) for q in regions))
        r.update_local_search(self.c,'one',{'hv_before':1.,'hv_gain':0},True)
        r.update_local_search(self.c,'one',{'hv_before':1.,'hv_gain':0},True)
        self.assertAlmostEqual(r.local_regions(self.c,b.load_training(self.c))[0]['radius_norm'],.1)
        r.update_local_search(self.c,'two',{'hv_before':1.,'hv_gain':0},True)
        self.assertAlmostEqual(r.local_regions(self.c,b.load_training(self.c))[0]['radius_norm'],.05)
    def test_al_repairs_diagnostics_after_csv_was_written(self):
        self.rows(4);args=b.build_parser().parse_args(['run','--config',str(self.cp),'--resume','--iterations','1','--batch-size','1','--max-new-cfd','1'])
        def evaluated(config,baseline,x,index,**meta):
            row={**b.vector_to_sample(config,x),**meta,'run_id':f'case_{index:06d}','status':'success','Efficiency':.762,'MassFlow':4.25}
            b.append_row(config,row);raise KeyboardInterrupt('after CSV before diagnostics')
        with patch.object(b,'evaluate_true_cfd',side_effect=evaluated):
            with self.assertRaises(KeyboardInterrupt):b.run_loop(args)
        args.max_new_cfd=0
        with patch.object(b,'evaluate_true_cfd',side_effect=AssertionError('must not evaluate again')):b.run_loop(args)
        self.assertEqual(len(pd.read_csv(self.out/'active_learning_diagnostics.csv')),1)
        self.assertEqual(len(b.load_training(self.c)),5)
    def test_cfx_rejects_changed_generated_boundary_file(self):
        kw=self.cfx_setup()
        def command(cmd,cwd,log):
            if Path(cmd[0]).name=='cfx5pre.exe':
                (self.out/'Impeller.def').write_text('def');return 0
            raise KeyboardInterrupt('stop before solve completes')
        with patch.object(cfx,'_run_logged',side_effect=command):
            with self.assertRaises(KeyboardInterrupt):cfx.run_cfx_pipeline(self.out,'case_0',**kw)
        state=json.loads((self.out/'cfx_state.json').read_text())
        # Model the preceding checkpoint boundary before any Solve invocation.
        state['stages'].pop('solve');(self.out/'cfx_state.json').write_text(json.dumps(state))
        (self.out/'update_bc.ccl').write_text('changed pressure')
        with patch.object(cfx,'_run_logged',side_effect=AssertionError('must reject changed CCL')):
            result=cfx.run_cfx_pipeline(self.out,'case_0',**kw)
        self.assertFalse(result.success)

    def test_boundary_reservation_survives_crash_before_point_checkpoint(self):
        self.rows();p=self.out/'plan.json';r.write_boundary_plan(self.c,'case_000000',p)
        real=r.atomic_json
        def interrupt_state(path,value):
            if Path(path)==p.with_suffix('.state.json'):
                raise KeyboardInterrupt('before boundary state checkpoint')
            return real(path,value)
        with patch.object(r,'atomic_json',side_effect=interrupt_state):
            with self.assertRaises(KeyboardInterrupt):r.run_boundary(self.c,p,'singles',1,False)
        self.assertEqual(b.next_case_index(self.c,b.load_training(self.c)),2,'reserved ID reused')
        used=[]
        def evaluate(config,baseline,x,index,**meta):
            used.append(index);row={**b.vector_to_sample(config,x),**meta,'run_id':f'case_{index:06d}',
                                  'status':'success','Efficiency':.761,'MassFlow':4.23}
            b.append_row(config,row);return b.CaseResult(row,True)
        with patch.object(b,'evaluate_true_cfd',side_effect=evaluate):r.run_boundary(self.c,p,'singles',1,True)
        self.assertEqual(used,[1])

    def test_changed_candidate_geometry_cannot_get_original_parameter_label(self):
        baseline=b.extract_baseline(self.c)
        path=b.write_candidate_files(self.c,baseline,self.x,'case_000000')
        payload=json.loads(path.read_text());payload['geometry']['hub_beta_rad'][0]+=.1
        path.write_text(json.dumps(payload))
        with patch.object(b,'run_geometry',side_effect=AssertionError('altered geometry must not execute')):
            with self.assertRaises(ValueError):b.evaluate_true_cfd(self.c,baseline,self.x,0,sample_phase='doe')
    def test_empty_optional_diagnostics_does_not_block_boundary(self):
        self.rows(4);path=self.out/'plan.json';r.write_boundary_plan(self.c,'case_000000',path)
        (self.out/'active_learning_diagnostics.csv').write_text('')
        def evaluated(config,baseline,x,index,**meta):
            row={**b.vector_to_sample(config,x),**meta,'run_id':f'case_{index:06d}','status':'success','Efficiency':.761,'MassFlow':4.23}
            b.append_row(config,row);return b.CaseResult(row,True)
        with patch.object(b,'evaluate_true_cfd',side_effect=evaluated):
            try:result=r.run_boundary(self.c,path,'singles',1,False)
            except pd.errors.EmptyDataError as e:self.fail(f'optional diagnostic stopped CFD: {e}')
        self.assertEqual(result['new_cfd_attempts'],1)
    def test_check_pre_obeys_case_lock(self):
        from blade_shape_runtime import output_lock
        self.cp.write_text(json.dumps(self.c))
        with output_lock(self.out):
            q=subprocess.run([sys.executable,'blade_shape_cfx_runner.py','check-pre','--config',str(self.cp),
                              '--working-dir',str(self.out)],capture_output=True,text=True)
        self.assertNotEqual(q.returncode,0)
        self.assertFalse((self.out/'update_bc.ccl').exists(),'check-pre wrote inside locked case')

    def test_completed_pipeline_recovers_before_csv_without_repeating_external_work(self):
        kw=self.cfx_setup()
        self.c['paths'].update(cfx_bin_dir=str(kw['cfx_bin_dir']),template_cfx=str(kw['template_cfx']),template_cse=str(kw['template_cse']))
        self.rows(4)
        args=b.build_parser().parse_args(['run','--config',str(self.cp),'--resume','--iterations','1','--batch-size','1','--max-new-cfd','1'])
        calls=[]
        def geometry(config,candidate_path,dry_run=False):
            calls.append('geometry');(candidate_path.parent/'Impeller_Mesh.gtm').write_text('mesh');return True,'ok'
        def external(cmd,cwd,log):
            name=Path(cmd[0]).name;calls.append(name)
            if name=='cfx5pre.exe':(cwd/'Impeller.def').write_text('def')
            elif name=='cfx5solve.exe':
                (cwd/'complete.res').write_text('complete');(cwd/'complete.out').write_text(cfx_output())
            else:(cwd/'CFX_Results.txt').write_text('.762,2,1,.425,2.1')
            return 0
        original=b.append_row
        def interrupted(config,row):
            if row['run_id']=='case_000004':raise KeyboardInterrupt('after verified Post before CSV')
            original(config,row)
        with patch.object(b,'run_geometry',side_effect=geometry),patch.object(cfx,'_run_logged',side_effect=external):
            with patch.object(b,'append_row',side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):b.run_loop(args)
            b.run_loop(args)
        self.assertEqual(calls,['geometry','cfx5pre.exe','cfx5solve.exe','cfx5post.exe'])
        self.assertEqual(b.load_training(self.c).tail(1).run_id.iloc[0],'case_000004')
        self.assertEqual(len(pd.read_csv(self.out/'active_learning_diagnostics.csv')),1)
    def test_outside_search_extension_is_not_used_as_local_center(self):
        self.rows(4)
        x=self.x.copy();x[4]=5.
        b.append_row(self.c,{**b.vector_to_sample(self.c,x),'run_id':'case_000004','status':'success',
                            'sample_phase':'boundary','Efficiency':.99,'MassFlow':5.})
        self.c['refinement']['local_search']={'enabled':True,'initial_radius_norm':.05,'min_radius_norm':.05,'max_radius_norm':.3}
        regions=r.local_regions(self.c,b.load_training(self.c))
        self.assertGreater(len(regions),0)
        self.assertNotIn('case_000004',[q['center_run_id'] for q in regions])
        self.assertTrue(all(np.all(q['lower']<=q['upper']) for q in regions))

if __name__=='__main__':unittest.main()
