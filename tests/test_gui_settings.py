from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_gui import PYSIDE_AVAILABLE, ProjectFixture


@unittest.skipUnless(PYSIDE_AVAILABLE, 'PySide6 is not installed')
class RememberedSettingsTests(ProjectFixture):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def open_window(self, config_path=None, data_dir=None):
        from blade_gui.context import AppContext
        from blade_gui.main_window import MainWindow
        ctx = AppContext(config_path, data_dir)
        window = MainWindow(ctx)
        window.show()
        self.app.processEvents()
        return ctx, window

    def close(self, window):
        window.close()
        window.deleteLater()
        self.app.processEvents()

    def test_first_launch_starts_without_config(self):
        ctx, window = self.open_window()
        try:
            self.assertIsNone(ctx.project.config_path)
            self.assertFalse(ctx.project.config_valid)
            self.assertIn('未选择配置', window.config_chip.text.text())
            run = window.pages[3]
            for index in range(run.action_box.count()):
                run.action_box.setCurrentIndex(index)
                self.app.processEvents()
                self.assertIn('尚未选择配置文件', run.preview.toPlainText())
            with patch.object(run.runner, 'start') as start, \
                    patch('blade_gui.pages.run_page.QMessageBox.warning'):
                run.start()
            start.assert_not_called()
            page = window.pages[5]
            for action in ('extract', 'sweep', 'aca'):
                self.assertEqual(page.forms[action]['post_exe'].text(), '')
            self.assertEqual(page.forms['plan']['config'].text(), '')
        finally:
            self.close(window)

    def test_first_config_choice_copies_run_defaults(self):
        ctx, window = self.open_window()
        try:
            run = window.pages[3]
            ctx.set_config_path(self.config_path)
            self.app.processEvents()
            self.assertEqual(run.run_max.value(), 2)
            self.assertIn('--max-new-cfd 2', run.preview.toPlainText())
            run.run_max.setValue(1)
            run.boundary_stage.setCurrentText('extension')
            run.cand_offline.setChecked(True)
        finally:
            self.close(window)
        ctx, window = self.open_window()
        try:
            run = window.pages[3]
            self.assertEqual(run.run_max.value(), 1)
            self.assertEqual(run.boundary_stage.currentText(), 'extension')
            self.assertTrue(run.cand_offline.isChecked())
        finally:
            self.close(window)

    def test_inputs_layout_and_config_survive_restart(self):
        ctx, window = self.open_window()
        ctx.set_config_path(self.config_path)
        page = window.pages[5]
        page.action_box.setCurrentIndex(page.action_box.findData('aca'))
        page.forms['aca']['res'].setText('D:/Kn/sliptip_007.res')
        page.forms['aca']['session'].setText('D:/ansys_work/extract_aca.cse')
        page.forms['aca']['post_exe'].setText('D:/ANSYS Inc/v251/CFD-Post/bin/cfx5post.exe')
        page.forms['aca']['output_dir'].setText(str(self.root / 'aca_new'))
        page.forms['legacy']['hub_beta_deg'].setValue(70.356)
        window.pages[3].plan_path.setText('D:/plans/b.json')
        window._select_page(5)
        window.resize(1300, 800)
        window.move(40, 30)
        self.app.processEvents()
        self.close(window)
        saved = ctx.read_bytes('window/geometry')
        self.assertIsNotNone(saved)

        from blade_gui.main_window import MainWindow
        with patch.object(MainWindow, 'restoreGeometry') as restore:
            ctx, window = self.open_window()
        try:
            # the offscreen test screen clamps sizes, so check the hand-off instead of pixels
            self.assertEqual(bytes(restore.call_args.args[0]), bytes(saved))
            self.assertEqual(ctx.project.config_path, self.config_path)
            page = window.pages[5]
            self.assertEqual(window.stack.currentIndex(), 5)
            self.assertEqual(page.action_box.currentData(), 'aca')
            self.assertEqual(page.forms['aca']['res'].text(), 'D:/Kn/sliptip_007.res')
            self.assertEqual(page.forms['aca']['session'].text(), 'D:/ansys_work/extract_aca.cse')
            self.assertAlmostEqual(page.forms['legacy']['hub_beta_deg'].value(), 70.356)
            self.assertEqual(window.pages[3].plan_path.text(), 'D:/plans/b.json')
            self.assertIn('--session', page.preview.toPlainText())
        finally:
            self.close(window)
        self.assertTrue(Path(os.environ['BLADE_GUI_SETTINGS']).is_file())

    def test_finished_aca_fills_legacy_csv(self):
        ctx, window = self.open_window(self.config_path, self.out)
        try:
            page = window.pages[5]
            out = self.root / 'aca'
            out.mkdir()
            aca_csv = out / 'x_beta_aca_20.csv'
            aca_csv.write_text('span,beta_cfx_deg\n')
            (out / 'extraction_summary.json').write_text(json.dumps(
                dict(status='complete', aca_csv=str(aca_csv), legacy_aca_quadrant_ok=True)))
            page.action_box.setCurrentIndex(page.action_box.findData('aca'))
            page.forms['aca']['output_dir'].setText(str(out))
            page._running_action = 'aca'
            page.finished(0, 'normal')
            self.assertEqual(page.forms['legacy']['csv'].text(), str(aca_csv))
            self.assertIn('ACA CSV 已导出', page.status.text())
        finally:
            self.close(window)


if __name__ == '__main__':
    unittest.main()
