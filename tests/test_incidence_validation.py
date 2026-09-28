from __future__ import annotations
import copy
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import blade_shape_incidence_validation as v


def spec():
    return {'schema_version': 1, 'target_id': 'synthetic',
            'res_path': 'synthetic.res', 'geometry_source': 'synthetic.cft',
            'geometry_verified': True, 'hub_beta_deg': 60., 'shroud_beta_deg': 30.,
            'measurement': {'bands': 2, 'le_station': .22, 'turbo_domain': 'R1',
                            'normal_sign': 1, 'theta_reference_sign': -1,
                            'max_reverse_fraction': .01},
            'conditions': {'rpm': 10000., 'inlet_total_pressure_pa': 100.,
                           'inlet_total_temperature_k': 80., 'fluid_id': 'helium',
                           'n_blades': 10}}


def values():
    result = {'surface_mass_flow': 2.}
    for j, angle in enumerate([55., 35.]):
        for key, value in dict(forward=1., reverse=0., ws_flux=math.sin(math.radians(angle)),
                               wt_flux=-math.cos(math.radians(angle)), span_flux=(j+.5)/2).items():
            result[f'b{j:03d}_{key}'] = value
    return result


class IncidenceTests(unittest.TestCase):
    def test_signed_triangle_and_mass_weighted_metal_angle(self):
        rows, summary = v.reduce_profile(values(), spec())
        self.assertAlmostEqual(rows[0]['incidence_deg'], -2.5)
        self.assertAlmostEqual(rows[1]['incidence_deg'], 2.5)
        self.assertAlmostEqual(summary['incidence_rms_mass_deg'], 2.5)
        self.assertTrue(summary['quality_ok'])
        changed = values(); changed['b000_wt_flux'] *= -1
        rows, _ = v.reduce_profile(changed, spec())
        self.assertGreater(rows[0]['flow_angle_deg'], 90.)

    def test_reverse_empty_nonfinite_and_wrong_normal_fail_quality(self):
        for key, number in [('b000_reverse', .5), ('b000_forward', 0.)]:
            altered = values(); altered[key] = number
            _, result = v.reduce_profile(altered, spec())
            self.assertFalse(result['quality_ok'])
            self.assertIsNone(result['incidence_rms_mass_deg'])
        altered = values(); altered['b000_ws_flux'] = float('nan')
        with self.assertRaises(ValueError): v.reduce_profile(altered, spec())

    def test_band_partition_and_no_absolute_velocity_in_triangle(self):
        expressions = v.profile_expressions(spec())
        self.assertIn('Span Normalized < 0.5000000000', expressions['b000_forward'])
        self.assertIn('Span Normalized <= 1.0000000000', expressions['b001_forward'])
        self.assertIn('Velocity Streamwise', expressions['b000_ws_flux'])
        self.assertNotIn('abs(Velocity Circumferential)', expressions['b000_wt_flux'])

    def test_unconfirmed_geometry_or_conventions_rejected(self):
        for key, value in [('geometry_verified', False), ('hub_beta_deg', float('inf'))]:
            s=spec(); s[key]=value
            with self.assertRaises(ValueError): v.validate_spec(s)
        s=spec(); s['measurement']['normal_sign']=0
        with self.assertRaises(ValueError): v.validate_spec(s)

    def test_legacy_reproduction_and_grid_validation(self):
        rows=[{'span': j/19, 'beta_cfx_deg': -30.} for j in range(20)]
        result, summary=v.legacy_profile(rows, 60., 60.)
        self.assertAlmostEqual(summary['mean_abs_incidence_deg'], 0.)
        self.assertEqual(len(result), 20)
        rows[-1]['span']=.9
        with self.assertRaises(ValueError): v.legacy_profile(rows, 60., 60.)

    def test_comparison_rejects_unmatched_flow_and_different_measurement(self):
        _, a=v.reduce_profile(values(), spec())
        a.update(spec=spec(), method='forward_mass_velocity_triangle_v1')
        b=copy.deepcopy(a); b['net_mass_flow_kg_s'] *= 1.2
        self.assertFalse(v.compare_results(a,b,.01)['matched_operating_point'])
        b=copy.deepcopy(a); b['spec']['measurement']['le_station']=.24
        with self.assertRaises(ValueError): v.compare_results(a,b,.01)

    def test_failed_post_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); s=spec(); case=root/'case'; case.mkdir()
            for name in ['synthetic.res','synthetic.cft']: (case/name).touch()
            (root/'post.exe').touch()
            s['res_path']=str(case/'synthetic.res'); s['geometry_source']=str(case/'synthetic.cft')
            with patch.object(v.subprocess,'run', side_effect=OSError('no post')):
                with self.assertRaises(OSError): v.extract(s,root/'post.exe',root/'out')
            state=json.loads((root/'out/state.json').read_text())
            self.assertEqual(state['status'],'failed')
            self.assertTrue((root/'out/extract.cse').is_file())
            self.assertTrue((root/'out/command.json').is_file())


    def test_successful_extraction_keeps_identity_and_quality_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); case=root/'case';case.mkdir();s=spec()
            for name in ['synthetic.res','synthetic.cft']:(case/name).write_text('test only')
            (root/'post.exe').touch()
            s['res_path']=str(case/'synthetic.res');s['geometry_source']=str(case/'synthetic.cft')
            def post(command,**kwargs):
                raw=''.join(f'{key}\t{value}\n' for key,value in values().items())+'__complete__\t1\n'
                (kwargs['cwd']/v.RAW_NAME).write_text(raw)
                return subprocess.CompletedProcess(command,0,stdout='mock post complete')
            with patch.object(v.subprocess,'run',side_effect=post):
                result=v.extract(s,root/'post.exe',root/'out')
            self.assertTrue(result['quality_ok'])
            self.assertFalse(result['numerical_acceptance_verified'])
            self.assertTrue((root/'out/profile.csv').exists())
            self.assertEqual((case/'synthetic.res').read_text(),'test only')
            with self.assertRaises(FileExistsError):v.extract(s,root/'post.exe',root/'out')


class SensitivityPlanTests(unittest.TestCase):
    def setUp(self):
        import blade_shape_active_learning as al
        self.al=al
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config=json.loads(Path('blade_shape_config.json').read_text())
        self.config['paths']['output_dir']=str(self.root/'optimizer')
        self.config['paths']['cft_batch_template']=str(Path('tests/fixtures/synthetic_meanline.cft-batch').resolve())
        for key in ('base_cft','turbogrid_template','template_cfx','template_cse','geometry_script_path'):
            p=self.root/key;p.write_text('synthetic fixture; never execute')
            self.config['paths'][key]=str(p)
        self.path=self.root/'config.json';self.path.write_text(json.dumps(self.config))
        self.study=self.root/'study'

    def plan(self):
        return v.make_plan(self.path,None,self.study,.25,[10.,12.])

    def test_freezes_target_geometry_and_isolates_other_coordinates(self):
        plan=self.plan()
        self.assertEqual(len(plan['points']),10)
        self.assertEqual(plan['points'][0]['x'],[0.]*12)
        self.assertEqual(plan['points'][1]['x'][0],-.25)
        self.assertEqual(plan['config']['search']['fixed_variables']['hub_beta_1_deg_offset'],0.)
        self.assertEqual(json.loads(self.path.read_text()),self.config)
        with self.assertRaises(FileExistsError):
            v.make_plan(self.path,None,self.study,.25,[10.,12.])

    def test_budget_resume_and_failure_never_retried(self):
        self.plan();calls=[]
        def evaluate(config,baseline,x,index,**kwargs):
            calls.append(index)
            return self.al.CaseResult({'status':'success','MassFlow':4.2},True)
        with patch.object(self.al,'evaluate_true_cfd',side_effect=evaluate):
            result=v.run_plan(self.study/'plan.json',1,False)
            self.assertEqual(result['status'],'budget_exhausted')
            self.assertEqual(calls,[0])
            with self.assertRaisesRegex(ValueError,'resume'):
                v.run_plan(self.study/'plan.json',1,False)
            result=v.run_plan(self.study/'plan.json',1,True)
            self.assertEqual(calls,[0,1])
        with patch.object(self.al,'evaluate_true_cfd',return_value=self.al.CaseResult({'status':'failed'},False)) as evaluate:
            result=v.run_plan(self.study/'plan.json',2,True)
            self.assertEqual(result['status'],'failed');self.assertEqual(evaluate.call_count,1)
            with self.assertRaisesRegex(ValueError,'Automatic retry'):
                v.run_plan(self.study/'plan.json',2,True)
            self.assertEqual(evaluate.call_count,1)

    def test_interruption_records_running_and_prevents_duplicate_cfd(self):
        self.plan()
        with patch.object(self.al,'evaluate_true_cfd',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):v.run_plan(self.study/'plan.json',1,False)
        state=json.loads((self.study/'progress.json').read_text())
        self.assertEqual(state['points']['0']['status'],'running')
        with self.assertRaisesRegex(ValueError,'Automatic retry'):
            v.run_plan(self.study/'plan.json',1,True)

    def test_changed_inputs_and_out_of_bounds_plan_rejected(self):
        self.plan()
        self.path.write_text(self.path.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'Frozen input changed'):
            v.run_plan(self.study/'plan.json',1,False)
        with self.assertRaisesRegex(ValueError,'out_of_bounds'):
            v.make_plan(self.path,None,self.root/'bad',100.,None)
        self.assertFalse((self.root/'bad').exists())


class EntryTests(unittest.TestCase):
    def test_windows_variable_paths_can_be_prepared_on_mac(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'spec.json'
            v.main(['init','--res',r'D:\target\test.res','--geometry-source',r'D:\target\test.cft','--output',str(output)])
            s=v.load_spec(output)
            self.assertEqual(s['res_path'],r'D:\target\test.res')
            self.assertFalse(s['geometry_verified'])

    def test_sweep_freezes_input_and_collects_six_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);case=root/'case';case.mkdir();s=spec()
            for key,name in [('res_path','test.res'),('geometry_source','test.cft')]:
                p=case/name;p.touch();s[key]=str(p)
            def extract(current,*args):
                return dict(quality_ok=True,incidence_rms_mass_deg=2.,mean_abs_incidence_mass_deg=1.5,net_mass_flow_kg_s=.004)
            with patch.object(v,'extract',side_effect=extract) as run:
                rows=v.measurement_sweep(s,root/'post.exe',root/'sweep',[.20,.22,.24],[20,40])
            self.assertEqual(run.call_count,6);self.assertEqual(len(rows),6)
            self.assertTrue((root/'sweep/sensitivity.csv').is_file())


if __name__ == '__main__': unittest.main()
