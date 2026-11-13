from pathlib import Path
import os
from copy import deepcopy
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from streamlit.testing.v1 import AppTest

from evaluation.dataset import Review, read_json
from evaluation.review_workspace import new_workspace, open_workspace, save_grade
from evaluation.reporting import verify_report

ROOT = Path(__file__).resolve().parents[1]


class ReviewUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_directory = tempfile.TemporaryDirectory()
        cls.archive = Path(cls.source_directory.name) / "source"
        with zipfile.ZipFile(ROOT / "docs/checks/20261111-evaluation-runner-local.zip") as package:
            package.extractall(cls.archive)

    @classmethod
    def tearDownClass(cls):
        cls.source_directory.cleanup()

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CODEBASE_QA_LOCAL_MODE":"1"}))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.workspace = new_workspace(self.archive, ROOT, self.directory, purpose="simulation")
        script = ("from evaluation.review_ui import render_review_page\n"
                  f"render_review_page(repository={str(ROOT)!r}, directory={str(self.directory)!r})")
        self.app = AppTest.from_string(script).run(timeout=30)
        self.element("text_input", "运行归档目录").set_value(str(self.archive))
        self.element("selectbox", "评审记录").select(str(self.workspace.packet))
        self.click("打开评审")

    def element(self, kind, label):
        return next(item for item in getattr(self.app, kind) if item.label == label)

    def click(self, label):
        self.element("button", label).click().run(timeout=30)
        self.assertEqual(len(self.app.exception), 0)

    def fill(self):
        for label in ("正确性（40%）", "证据（30%）", "完整性（20%）", "边界说明（10%）"):
            self.element("selectbox", label).select(2)
        self.element("text_input", "评审者").set_value("synthetic-ui-test")
        self.element("text_area", "评分理由").set_value("合成界面操作，只测试保存，不是实际语义评审")

    def test_initial_scores_are_blank_and_incomplete_save_does_not_grade(self):
        self.assertEqual(self.element("selectbox", "正确性（40%）").value, None)
        self.click("保存本轮")
        self.assertTrue(any("四项分数" in item.value for item in self.app.error))
        self.assertTrue(all(row.ratings is None for row in open_workspace(self.archive, ROOT, self.workspace.packet).rows))

    def test_save_resume_report_and_clear_keep_originals(self):
        self.fill()
        self.click("保存本轮")
        self.assertEqual(open_workspace(self.archive, ROOT, self.workspace.packet).rows[0].status, "reviewed")
        self.click("生成报告")
        report = Path(self.app.session_state["review_report"])
        receipt = verify_report(report, self.archive, ROOT)
        self.assertEqual((receipt["reviewed_turns"], receipt["required_turns"]), (1, 19))
        self.assertIsNone(read_json((report / "report.json").read_bytes())["summary"]["mean_score"])
        self.assertEqual(len(self.app.get("download_button")), 2)
        self.click("打开评审")
        self.assertEqual(self.element("selectbox", "正确性（40%）").value, 2)
        self.click("清除本轮评分")
        self.assertEqual(open_workspace(self.archive, ROOT, self.workspace.packet).rows[0].status, "pending_review")
        self.assertEqual(verify_report(report, self.archive, ROOT), receipt)

    def test_turn_selection_keeps_history_and_does_not_reuse_other_turn_scores(self):
        self.fill()
        self.click("保存本轮")
        self.element("selectbox", "题目").select("U04").run(timeout=30)
        self.element("selectbox", "轮次").select("T2").run(timeout=30)
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.element("selectbox", "正确性（40%）").value, None)
        context = self.workspace.context["cases"][3]["turns"]
        rendered = "\n".join(item.value for item in self.app.text)
        self.assertIn(context[0]["turn"]["question"], rendered)
        self.assertIn(context[1]["turn"]["question"], rendered)
        self.assertTrue(any("此前对话" == item.label for item in self.app.expander))
        self.element("selectbox", "题目").select("P04").run(timeout=30)
        self.assertTrue(any("本轮不强制" in item.value for item in self.app.text))

    def test_external_edit_is_visible_and_stale_form_cannot_overwrite_it(self):
        row = self.workspace.rows[0]
        self.fill()
        saved = save_grade(self.workspace, row.case_id, row.turn_id,
                           ratings=dict(correctness=0, evidence=0, completeness=0, boundaries=2),
                           reviewer="other-editor", rationale="合成并发编辑测试")
        self.click("保存本轮")
        self.assertTrue(any("已被更新" in item.value for item in self.app.error))
        self.assertEqual(open_workspace(self.archive, ROOT, self.workspace.packet).rows, saved.rows)
        self.click("打开评审")
        self.assertEqual(self.element("selectbox", "正确性（40%）").value, 0)

    def test_failed_open_clears_old_work_and_reading_does_not_call_model(self):
        with patch("agent.qa_agent._get_agent", side_effect=AssertionError("MODEL_MUST_NOT_RUN")):
            self.app.run(timeout=30)
            self.assertEqual(len(self.app.exception), 0)
        self.element("text_input", "运行归档目录").set_value(str(self.directory / "missing"))
        self.click("打开评审")
        self.assertNotIn("review_active", self.app.session_state)
        self.assertEqual(len(self.app.get("download_button")), 0)

    def test_new_packet_and_fresh_page_resume_keep_scores_unfilled(self):
        self.element("selectbox", "评审记录").select(None)
        self.click("打开评审")
        active = self.app.session_state["review_active"]
        self.assertNotEqual(active[1], str(self.workspace.packet))
        workspace = open_workspace(self.archive, ROOT, active[1])
        self.assertEqual(workspace.header.purpose, "human")
        self.assertTrue(all(row.ratings is None for row in workspace.rows))
        script = ("from evaluation.review_ui import render_review_page\n"
                  f"render_review_page(repository={str(ROOT)!r}, directory={str(self.directory)!r})")
        self.app = AppTest.from_string(script).run(timeout=30)
        self.element("text_input", "运行归档目录").set_value(str(self.archive))
        self.element("selectbox", "评审记录").select(active[1])
        self.click("打开评审")
        self.assertEqual(self.element("selectbox", "正确性（40%）").value, None)

    def test_return_from_another_page_restores_active_packet_inputs(self):
        active = self.app.session_state["review_active"]
        script = ("from evaluation.review_ui import render_review_page\n"
                  f"render_review_page(repository={str(ROOT)!r}, directory={str(self.directory)!r})")
        self.app = AppTest.from_string(script)
        self.app.session_state["review_active"] = active
        self.app.run(timeout=30)
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(Path(self.element("text_input", "运行归档目录").value).resolve(), self.archive.resolve())
        self.assertEqual(self.element("selectbox", "评审记录").value, str(self.workspace.packet))

    def test_uncompleted_turn_has_no_grading_form_and_retains_original_error(self):
        context = deepcopy(self.workspace.context)
        context["cases"][0]["turns"][0].update(checked_answer=None,
            outcome={"status": "failed"}, error={"message": "ORIGINAL_FAILURE"})
        row = Review.model_validate(dict(self.workspace.rows[0].model_dump(), status="execution_failed",
                                        output_sha256=None, error="ORIGINAL_FAILURE"))
        workspace = replace(self.workspace, context=context, rows=[row, *self.workspace.rows[1:]])
        with patch("evaluation.review_ui.open_workspace", return_value=workspace):
            self.app.run(timeout=30)
        self.assertEqual(len(self.app.exception), 0)
        self.assertFalse(any(item.label == "保存本轮" for item in self.app.button))
        self.assertTrue(any("ORIGINAL_FAILURE" in item.value for item in self.app.code))


if __name__ == "__main__":
    unittest.main()
