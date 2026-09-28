from __future__ import annotations
import unittest
from unittest.mock import PropertyMock,patch
from blade_gui.commands import build_validation,VALIDATION_SCRIPT
from tests.test_gui import ProjectFixture,PYSIDE_AVAILABLE


class ValidationCommandsTests(unittest.TestCase):
    def test_all_actions_use_validation_cli_and_preserve_paths(self):
        options={
            'init':dict(res='D:/a b/x.res',geometry_source='D:/a b/x.cft',output='D:/new/spec.json'),
            'extract':dict(spec='spec.json',post_exe='post.exe',output_dir='out'),
            'sweep':dict(spec='spec.json',post_exe='post.exe',output_dir='out',stations='.2 .22 .24',bands='20,40'),
            'legacy':dict(csv='old.csv',hub_beta_deg=0.,shroud_beta_deg=30.,output_dir='out'),
            'compare':dict(baseline='a.json',target='b.json',flow_tolerance=.01,output='c.json'),
            'plan':dict(config='config.json',step_deg=.25,output_dir='study'),
            'run':dict(plan='plan.json',max_new_cfd=1,resume=True)}
        for action,values in options.items():
            with self.subTest(action=action):
                spec=build_validation(action,**values)
                self.assertEqual(spec.script,VALIDATION_SCRIPT)
                self.assertEqual(spec.args[0],action)
        self.assertIn('D:/a b/x.res',build_validation('init',**options['init']).args)
        self.assertIn('--resume',build_validation('run',**options['run']).args)
        self.assertNotIn('--candidate',build_validation('plan',**options['plan']).args)
        with self.assertRaises(ValueError):build_validation('sweep',**{**options['sweep'],'bands':'oops'})


@unittest.skipUnless(PYSIDE_AVAILABLE,'PySide6 is not installed')
class ValidationWidgetTests(ProjectFixture):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        from blade_gui.context import AppContext
        from blade_gui.main_window import MainWindow
        self.ctx=AppContext(self.config_path,self.out)
        self.window=MainWindow(self.ctx);self.page=self.window.pages[-1]
        self.window.show();self.window._select_page(5);self.app.processEvents()

    def tearDown(self):
        self.window.close();self.window.deleteLater();self.app.processEvents()
        super().tearDown()

    def test_validation_page_paints_and_dispatches_init(self):
        self.assertEqual(self.page.nav_label,'验证')
        self.assertEqual(self.page.action_box.count(),7)
        fields=self.page.forms['init']
        for key,value in dict(res='D:/target.res',geometry_source='D:/target.cft',output='D:/spec.json').items():
            fields[key].setText(value)
        self.assertIn('blade_shape_incidence_validation.py',self.page.preview.toPlainText())
        with patch.object(self.page.runner,'start',return_value=True) as start:
            self.page.start()
        self.assertIn('D:/target.res',start.call_args.args[0])
        self.assertFalse(self.window.grab().toImage().isNull())

    def test_real_cfd_form_passes_budget_and_resume(self):
        from PySide6.QtWidgets import QMessageBox
        self.page.action_box.setCurrentIndex(6)
        fields=self.page.forms['run'];fields['plan'].setText('D:/study/plan.json')
        fields['max_new_cfd'].setValue(2);fields['resume'].setChecked(True)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Yes),patch.object(self.page.runner,'start') as start:
            self.page.start()
        argv=start.call_args.args[0]
        self.assertIn('--resume',argv);self.assertEqual(argv[argv.index('--max-new-cfd')+1],'2')

    def test_config_defaults_and_cross_page_busy_guard(self):
        config=self.window.pages[2]
        self.assertEqual(config._bindings['cfx_convergence.rms_target'][1].value(),1e-5)
        self.assertEqual(config._bindings['cfx_convergence.restart_iterations'][1].value(),2000)
        from blade_gui.runner import CommandRunner
        with patch.object(CommandRunner,'running',new_callable=PropertyMock,return_value=True),patch.object(self.page.runner,'start') as start:
            self.page.start();start.assert_not_called()
        self.assertIn('已有',self.page.status.text())


if __name__=='__main__':unittest.main()
