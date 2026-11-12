from dataclasses import replace
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from evaluation.archive import verify_archive
from evaluation.dataset import Review, digest, read_json
from evaluation.reporting import verify_report
from evaluation.review_workspace import create_report, new_workspace, open_workspace, report_package, save_grade

ROOT = Path(__file__).resolve().parents[1]


class ReviewWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_directory = tempfile.TemporaryDirectory()
        cls.archive = Path(cls.source_directory.name) / "source"
        with zipfile.ZipFile(ROOT / "docs/checks/20261111-evaluation-runner-local.zip") as package:
            package.extractall(cls.archive)
        cls.source_receipt = verify_archive(cls.archive, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.source_directory.cleanup()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.workspace = new_workspace(self.archive, ROOT, self.directory, purpose="simulation")
        self.row = self.workspace.rows[0]
        self.values = dict(ratings=dict(correctness=2, evidence=2, completeness=1, boundaries=1),
                           reviewer="synthetic-ui-test", rationale="合成评分，验证界面存储流程，不是人工语义结论")

    def save(self, workspace=None, **values):
        return save_grade(workspace or self.workspace, self.row.case_id, self.row.turn_id, **(self.values | values))

    def test_blank_packet_stays_ungraded_and_save_changes_only_one_bound_turn(self):
        self.assertTrue(all(row.ratings is None for row in self.workspace.rows))
        saved = self.save()
        reopened = open_workspace(self.archive, ROOT, self.workspace.packet)
        self.assertEqual(saved.rows, reopened.rows)
        self.assertEqual(saved.rows[0].output_sha256, self.row.output_sha256)
        self.assertEqual(saved.rows[1:], self.workspace.rows[1:])
        self.assertNotEqual(saved.revision, self.workspace.revision)
        self.assertEqual(verify_archive(self.archive, ROOT), self.source_receipt)

    def test_incomplete_invalid_or_unknown_grades_leave_original_bytes_unchanged(self):
        path = self.workspace.packet / "reviews.jsonl"
        original = path.read_bytes()
        for values in (dict(ratings=None), dict(reviewer=" "), dict(rationale=" "),
                       dict(fatal_errors=["invented"]), dict(fatal_errors=["wrong_source"] * 2),
                       dict(ratings=dict(correctness=True, evidence=2, completeness=2, boundaries=2)),
                       dict(ratings=dict(correctness=3, evidence=2, completeness=2, boundaries=2))):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.save(**values)
            self.assertEqual(path.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "不存在"):
            save_grade(self.workspace, "unknown", "T1", **self.values)
        self.assertEqual(path.read_bytes(), original)

    def test_stale_editor_cannot_overwrite_a_newer_grade_or_publish_stale_report(self):
        other = open_workspace(self.archive, ROOT, self.workspace.packet)
        saved = self.save()
        with self.assertRaisesRegex(ValueError, "已被更新"):
            self.save(other, rationale="stale editor")
        with self.assertRaisesRegex(ValueError, "已被更新"):
            create_report(other, self.directory)
        self.assertFalse((self.directory / "reports").exists())
        self.assertEqual(open_workspace(self.archive, ROOT, self.workspace.packet).rows, saved.rows)

    def test_clear_restores_pending_without_changing_output_and_old_report_is_immutable(self):
        saved = self.save()
        report, receipt = create_report(saved, self.directory)
        before = (report / "report-seal.json").read_bytes()
        cleared = self.save(saved, clear=True)
        self.assertEqual(cleared.rows, self.workspace.rows)
        self.assertEqual(verify_report(report, self.archive, ROOT), receipt)
        self.assertEqual((report / "report-seal.json").read_bytes(), before)
        newer, newer_receipt = create_report(cleared, self.directory)
        self.assertNotEqual(report, newer)
        self.assertEqual(newer_receipt["reviewed_turns"], 0)

    def test_partial_report_and_download_reuse_frozen_scoring_and_remain_verifiable(self):
        saved = self.save(ratings=dict(correctness=0, evidence=0, completeness=0, boundaries=2))
        report, receipt = create_report(saved, self.directory)
        data = read_json((report / "report.json").read_bytes())
        self.assertEqual((receipt["reviewed_turns"], receipt["required_turns"]), (1, 19))
        self.assertIsNone(data["summary"]["mean_score"])
        self.assertIsNone(data["summary"]["pass_rate"])
        self.assertEqual(data["known_badcase_count"], 1)
        self.assertFalse(data["real_model_quality_evaluated"])
        package = report_package(report, self.archive, ROOT)
        with zipfile.ZipFile(BytesIO(package)) as zipped:
            self.assertEqual(len(zipped.namelist()), 9)
            destination = self.directory / "extracted"
            zipped.extractall(destination)
        self.assertEqual(verify_report(destination, self.archive, ROOT), receipt)
        (report / "report.md").write_text("tampered")
        with self.assertRaises(ValueError):
            report_package(report, self.archive, ROOT)

    def test_source_and_report_submissions_cannot_be_edited_as_working_packets(self):
        report, _ = create_report(self.workspace, self.directory)
        original = digest((report / "report-seal.json").read_bytes())
        for packet in (self.archive, report / "submission"):
            with self.subTest(packet=packet), self.assertRaisesRegex(ValueError, "封存"):
                open_workspace(self.archive, ROOT, packet)
        self.assertEqual(digest((report / "report-seal.json").read_bytes()), original)
        self.assertFalse((report / "submission/.edit.lock").exists())

    def test_failed_and_not_run_records_cannot_be_graded(self):
        for status in ("execution_failed", "not_run"):
            row = Review.model_validate(dict(self.row.model_dump(), status=status, output_sha256=None,
                                             error="original failure" if status == "execution_failed" else None))
            workspace = replace(self.workspace, rows=[row, *self.workspace.rows[1:]])
            with self.subTest(status=status), patch("evaluation.review_workspace._load", return_value=workspace):
                with self.assertRaisesRegex(ValueError, "没有完成输出"):
                    self.save(workspace)

    def test_atomic_write_failure_keeps_original_review_and_cleans_temporary_file(self):
        path = self.workspace.packet / "reviews.jsonl"
        original = path.read_bytes()
        with patch("evaluation.review_workspace.os.replace", side_effect=OSError("FIXTURE_WRITE_FAILURE")):
            with self.assertRaisesRegex(OSError, "FIXTURE_WRITE_FAILURE"):
                self.save()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.workspace.packet.glob(".reviews-*")), [])

    def test_binding_tamper_and_symlink_are_rejected_before_editing(self):
        path = self.workspace.packet / "context.json"
        original = path.read_bytes()
        path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "context"):
            self.save()
        path.write_bytes(original)
        path.unlink()
        path.symlink_to(self.archive / "inputs.json")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.save()


if __name__ == "__main__":
    unittest.main()
