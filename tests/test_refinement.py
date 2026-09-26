from __future__ import annotations
import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import blade_shape_active_learning as b

def fake_geometry(config, candidate_path, dry_run=False):
    (candidate_path.parent/'Impeller_Mesh.gtm').write_text('synthetic test mesh')
    return True,'ok'

class RefinementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = json.loads(Path('blade_shape_config.json').read_text())
        self.config['paths']['output_dir'] = str(self.root)
        self.config['surrogate']['model'] = 'rbf'
        names = b.variable_names(self.config)
        fixed_names = [names[i] for i in [1,2,6,9,11]]
        self.config['search'] = {'active_variables':[n for n in names if n not in fixed_names],
                                 'fixed_variables':{n:0.0 for n in fixed_names}, 'slice_tolerance_norm':1e-8}
        self.config['refinement'] = {'challenger_min_samples':4, 'diagnostic_min_points':6,
          'diagnostic_window':12, 'boundary_variables':[names[i] for i in [4,5,0,10]],
          'extension':{'variable':names[4], 'value':5.0}}
        self.center = np.zeros(12)
        self.center[4] = 2.0
        self.baseline = b.BaselineShape(np.ones(5),np.ones(5),np.linspace(0,1,5),np.linspace(0,1,5),0.,0.)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('blade_shape_refinement'), 'refinement feature missing')
        return importlib.import_module('blade_shape_refinement')

    def save_row(self, number=0, x=None, **extra):
        x=self.center if x is None else x
        row={**b.vector_to_sample(self.config,x), 'run_id':f'case_{number:06d}',
             'status':'success','sample_phase':'active_learning','Efficiency':.76,'MassFlow':4.2,**extra}
        b.append_row(self.config,row)
        return row

    def test_fixed_sampling_and_duplicate_slice(self):
        # Removing fixed enforcement or using projected historical distance must fail this test.
        samples=b.lhs_samples(self.config,8,42)
        self.assertTrue(np.all(samples[:,[1,2,6,9,11]]==0), 'fixed coordinates changed')
        old=self.center.copy();old[1]=1
        self.assertTrue(b.is_far_enough(self.config,self.center,old[None,:]))
        self.assertFalse(b.is_far_enough(self.config,self.center,self.center[None,:]))

    def test_partition_preserves_old_coordinates(self):
        r=self.module();old=self.center.copy();old[1]=1
        df=pd.DataFrame([self.save_row(0,old),self.save_row(1)])
        history,local=r.training_partition(self.config,df)
        self.assertEqual(len(history),2);self.assertEqual(len(local),1)
        self.assertEqual(history.iloc[0][b.variable_names(self.config)[1]],1)
        bad=copy.deepcopy(self.config);bad['search']['fixed_variables'][b.variable_names(bad)[0]]=0
        with self.assertRaises(ValueError):r.active_indices(bad)

    def test_csv_metadata_and_boundary_phase_survive_append(self):
        self.save_row(0,sample_phase='boundary',experiment_id='p',design_role='center',external_note='keep')
        self.save_row(1)
        raw=pd.read_csv(self.root/'training_data.csv');loaded=b.load_training(self.config)
        self.assertIn('external_note',raw.columns, 'append discarded an existing CSV field')
        self.assertEqual(raw.iloc[0]['external_note'],'keep')
        self.assertEqual(loaded.iloc[0]['sample_phase'],'boundary')
        self.assertEqual(loaded.iloc[0]['experiment_id'],'p')

    def test_exact_hypervolume(self):
        r=self.module()
        self.assertAlmostEqual(r.hypervolume_2d(np.array([[2,1],[1,2],[.5,.5]]),np.zeros(2)),3.)
        self.assertEqual(r.hypervolume_2d(np.array([[np.nan,3]]),np.zeros(2)),0.)

    def test_boundary_plan_stage_controls_and_immutability(self):
        r=self.module();self.save_row()
        with patch.object(b,'extract_baseline',return_value=self.baseline):
            plan=r.write_boundary_plan(self.config,'case_000000',self.root/'plan.json')
        self.assertEqual([sum(p['stage']==s for p in plan['points']) for s in ['singles','pairs','extension']],[5,6,1])
        ext=plan['points'][-1];control=next(p for p in plan['points'] if p['point_id']==ext['control_id'])
        a=np.array(ext['x']);c=np.array(control['x']);self.assertEqual(np.count_nonzero(a!=c),1)
        self.assertEqual(a[4],5.);self.assertEqual(c[4],4.5)
        self.assertTrue(np.all(np.array([p['x'] for p in plan['points']])[:,[1,2,6,9,11]]==0))
        with self.assertRaises(FileExistsError):r.write_boundary_plan(self.config,'case_000000',self.root/'plan.json')

    def test_role_diagnostics_exclude_failure_and_report_width(self):
        r=self.module();rows=[]
        for role,value,status in [('ehvi',.761,'success'),('diversity',.770,'success'),('ehvi',99,'failed')]:
            rows.append({'run_id':str(len(rows)), 'candidate_role':role,'status':status,
              'pred_Efficiency':.760,'true_Efficiency':value,'std_Efficiency':.002,
              'pred_MassFlow':4.,'true_MassFlow':4.01,'std_MassFlow':.02})
        table=r.role_diagnostics(pd.DataFrame(rows))
        q=table[(table.candidate_role=='ehvi')&(table.objective=='Efficiency')].iloc[0]
        self.assertEqual(q['n'],1);self.assertAlmostEqual(q.mae,.001)
        self.assertAlmostEqual(q.mean_interval_width_2sigma,.008)

    def test_boundary_resume_and_stage_requirements(self):
        r=self.module();self.save_row()
        with patch.object(b,'extract_baseline',return_value=self.baseline):
            r.write_boundary_plan(self.config,'case_000000',self.root/'plan.json')
            with self.assertRaises(ValueError):r.run_boundary(self.config,self.root/'plan.json','pairs',1,False)
            calls=[]
            def evaluate(config,baseline,x,index,**metadata):
                calls.append(index)
                row={**b.vector_to_sample(config,x),**metadata,'run_id':f'case_{index:06d}',
                     'status':'success','Efficiency':.761,'MassFlow':4.21}
                b.append_row(config,row)
                return b.CaseResult(row,True)
            # External CFD is the sole replacement; actual plan/state/CSV machinery runs.
            with patch.object(b,'evaluate_true_cfd',side_effect=evaluate):
                r.run_boundary(self.config,self.root/'plan.json','singles',2,False)
                self.assertEqual(calls,[1,2])
                r.run_boundary(self.config,self.root/'plan.json','singles',5,True)
                self.assertEqual(calls,[1,2,3,4,5])
                r.run_boundary(self.config,self.root/'plan.json','singles',5,True)
                self.assertEqual(len(calls),5)
                state=json.loads((self.root/'plan.state.json').read_text())
                self.assertEqual(sum(v['status']=='success' for v in state['points'].values()),5)

    def test_interrupted_boundary_reconciles_csv_without_repeating(self):
        r=self.module();self.save_row()
        path=self.root/'plan.json'
        with patch.object(b,'extract_baseline',return_value=self.baseline):
            r.write_boundary_plan(self.config,'case_000000',path)
            def crash_after_write(config,baseline,x,index,**metadata):
                b.append_row(config,{**b.vector_to_sample(config,x),**metadata,'run_id':f'case_{index:06d}',
                                     'status':'success','Efficiency':.761,'MassFlow':4.21})
                raise KeyboardInterrupt('simulated interruption after durable CSV append')
            with patch.object(b,'evaluate_true_cfd',side_effect=crash_after_write):
                with self.assertRaises(KeyboardInterrupt):r.run_boundary(self.config,path,'singles',1,False)
            calls=[]
            def finish(config,baseline,x,index,**metadata):
                calls.append(index)
                row={**b.vector_to_sample(config,x),**metadata,'run_id':f'case_{index:06d}',
                     'status':'success','Efficiency':.762,'MassFlow':4.22}
                b.append_row(config,row);return b.CaseResult(row,True)
            with patch.object(b,'evaluate_true_cfd',side_effect=finish):
                r.run_boundary(self.config,path,'singles',1,True)
            self.assertEqual(calls,[2])
            state=json.loads(path.with_suffix('.state.json').read_text())
            self.assertEqual(state['points']['center']['status'],'success')
            self.assertIn('hv_gain',state['points']['center'])

    def test_signed_plan_rejects_coordinate_and_config_edits(self):
        r=self.module();self.save_row();path=self.root/'plan.json'
        with patch.object(b,'extract_baseline',return_value=self.baseline):
            plan=r.write_boundary_plan(self.config,'case_000000',path)
            wrong=copy.deepcopy(self.config);wrong['runtime']['p_out_pa']+=1
            with self.assertRaises(ValueError):r.run_boundary(wrong,path,'singles',1,False)
            plan['points'][0]['x'][0]=2;path.write_text(json.dumps(plan))
            with self.assertRaises(ValueError):r.run_boundary(self.config,path,'singles',1,False)

    def test_model_uses_full_history_but_local_challenger_only_same_slice(self):
        r=self.module()
        for i in range(5):
            x=self.center.copy();x[0]=i*.1
            if i==0:x[1]=1
            self.save_row(i,x)
        class ShapeCheckedModel:
            def __init__(self,x):self.width=x.shape[1];self.n=len(x)
            def predict(self,x):
                if x.shape[1]!=self.width:raise ValueError('model input lost columns')
                return np.full((len(x),2),self.n),np.ones((len(x),2))
        model=r.ConditionalSurrogate(self.config,b.load_training(self.config),lambda c,x,y,s:ShapeCheckedModel(x),42)
        meta=model.metadata(self.center)
        self.assertEqual(meta['history_train_count'],5)
        self.assertEqual(meta['slice_train_count'],4)
        self.assertEqual(meta['challenger_pred_Efficiency'],4)
        self.assertEqual(model.predict(self.center[None,:])[0][0,0],5)

    def test_case_number_accounts_for_orphaned_directory(self):
        self.save_row(2)
        (self.root/'cases'/'case_000010').mkdir(parents=True)
        self.assertEqual(b.next_case_index(self.config,b.load_training(self.config)),11)

    def test_invalid_cfd_metrics_do_not_become_success(self):
        class Outcome:
            success=True;message='Success';metrics={'Efficiency':np.nan,'MassFlow':4.2}
        with patch.object(b,'run_geometry',side_effect=fake_geometry),patch.object(b,'run_cfx_pipeline',return_value=Outcome()):
            result=b.evaluate_true_cfd(self.config,self.baseline,self.center,0,sample_phase='active_learning')
        self.assertFalse(result.success)
        self.assertEqual(result.row['failure_stage'],'post')

    def test_invalid_vector_is_a_geometry_failure(self):
        self.assertIn('invalid_vector',b.constraint_violations(self.config,self.baseline,np.zeros(7)))

    def test_gate_requires_same_model_and_enough_utilization_points(self):
        r=self.module();out=self.root/'active_learning_diagnostics.csv';rows=[]
        for i in range(6):
            rows.append(dict(run_id=f'case_{i:06d}',slice_id=r.slice_id(self.config),surrogate_model='old' if i<5 else 'new',
                             status='success',candidate_role='ehvi',pred_Efficiency=.76,true_Efficiency=.76,
                             std_Efficiency=.001,pred_MassFlow=4.2,true_MassFlow=4.2,std_MassFlow=.01))
        pd.DataFrame(rows).to_csv(out,index=False)
        gate=r.write_diagnostics(self.config)
        self.assertFalse(gate['passed'])
        self.assertEqual(gate['objectives']['Efficiency']['n'],1)

    def test_environment_failure_is_recorded(self):
        with patch.object(b,'run_geometry',side_effect=FileNotFoundError('PowerShell missing')):
            result=b.evaluate_true_cfd(self.config,self.baseline,self.center,0,sample_phase='boundary')
        self.assertFalse(result.success)
        self.assertEqual(result.row['failure_stage'],'environment')
        self.assertEqual(len(pd.read_csv(self.root/'training_data.csv')),1)

    def test_active_loop_keeps_roles_predictions_and_iteration_on_resume(self):
        self.config['runtime'].update(initial_samples=0,nsga2_pop_size=6,nsga2_generations=2,candidate_pool_size=12)
        self.config['surrogate'].update(ehvi_y_samples=3,ehvi_hv_points=32)
        for i in range(4):
            x=self.center.copy();x[0]=i*.2
            self.save_row(i,x,al_iteration=13,Efficiency=.760+i*.0001,MassFlow=4.20+i*.01)
        path=self.root/'config.json';path.write_text(json.dumps(self.config))
        args=b.build_parser().parse_args(['run','--config',str(path),'--resume','--iterations','1','--max-new-cfd','3'])
        class Outcome:
            success=True;message='synthetic test only'
            metrics={'Efficiency':.762,'MassFlow':4.25,'PressureRatio':2.,'Power':1.,'totalpressureratio':2.1}
        with patch.object(b,'extract_baseline',return_value=self.baseline),patch.object(b,'run_geometry',side_effect=fake_geometry),patch.object(b,'run_cfx_pipeline',return_value=Outcome()):
            b.run_loop(args)
        training=b.load_training(self.config);diagnostics=pd.read_csv(self.root/'active_learning_diagnostics.csv')
        self.assertEqual(len(training),7)
        self.assertEqual(set(training.tail(3).al_iteration),{14})
        self.assertEqual(diagnostics.candidate_role.tolist(),['ehvi','uncertainty','diversity'])
        self.assertTrue(diagnostics.challenger_pred_Efficiency.notna().all())
        self.assertTrue((diagnostics.hv_gain>=0).all())
        self.assertEqual(len(list((self.root/'predictions').glob('*.json'))),3)
        self.assertTrue(np.all(training.tail(3)[[b.variable_names(self.config)[i] for i in [1,2,6,9,11]]].to_numpy()==0))

    def test_failure_stops_stage_without_automatic_retry(self):
        r=self.module();self.save_row();path=self.root/'plan.json'
        with patch.object(b,'extract_baseline',return_value=self.baseline):
            r.write_boundary_plan(self.config,'case_000000',path)
            def fail(config,baseline,x,index,**metadata):
                row={**b.vector_to_sample(config,x),**metadata,'run_id':f'case_{index:06d}',
                     'status':'failed','failure_stage':'mesh'}
                b.append_row(config,row);return b.CaseResult(row,False)
            with patch.object(b,'evaluate_true_cfd',side_effect=fail):
                result=r.run_boundary(self.config,path,'singles',5,False)
                self.assertEqual(result['new_cfd_attempts'],1)
                with self.assertRaises(ValueError):r.run_boundary(self.config,path,'singles',5,True)
            self.assertEqual(len(b.load_training(self.config)),2)

    def test_all_boundary_stages_keep_extension_local_and_write_predictions(self):
        r=self.module();self.save_row();path=self.root/'plan.json'
        with patch.object(b,'extract_baseline',return_value=self.baseline):
            r.write_boundary_plan(self.config,'case_000000',path)
            class Outcome:
                success=True;message='synthetic test only'
                metrics={'Efficiency':.762,'MassFlow':4.25,'PressureRatio':2.,'Power':1.,'totalpressureratio':2.1}
            with patch.object(b,'run_geometry',side_effect=fake_geometry),patch.object(b,'run_cfx_pipeline',return_value=Outcome()):
                r.run_boundary(self.config,path,'singles',5,False)
                r.run_boundary(self.config,path,'pairs',6,True)
                r.run_boundary(self.config,path,'extension',1,True)
        training=b.load_training(self.config)
        self.assertEqual(len(training),13)
        ext=training.loc[training.design_role.eq('extension')].iloc[0]
        ctrl=training.loc[training.design_role.eq(b.variable_names(self.config)[4])].iloc[0]
        self.assertEqual(ext[b.variable_names(self.config)[4]],5.)
        self.assertEqual(ctrl[b.variable_names(self.config)[4]],4.5)
        self.assertEqual(self.config['variables'][4]['upper'],4.5)
        diag=pd.read_csv(self.root/'boundary_diagnostics.csv')
        self.assertEqual(len(diag),12)
        self.assertTrue(diag.tail(1).pred_Efficiency.notna().all())
        self.assertEqual(len(training.loc[training.sample_phase.eq('boundary')]),12)

if __name__=='__main__':unittest.main()
