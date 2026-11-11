import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from evaluation.archive import digest, read_json, write_json
from evaluation.local_model import LocalModel
from evaluation.reporting import _inventory, import_review, verify_report
from evaluation.reviews import prepare_review
from evaluation.runner import run_local
from tests import test_evaluation_runner as fixtures

ROOT = Path(__file__).resolve().parents[1]


class ReviewImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = fixtures.RunnerTests()
        cls.helper.setUp()
        cls.helper.run_fixture()
        cls.source = cls.helper.run_dir
        cls.repository = cls.helper.fixture.work
        cls.failed = cls.helper.fixture.root / "failed-run"
        asyncio.run(run_local(cls.helper.fixture.data, cls.repository, cls.failed,
                             backend=LocalModel(fail_question="fixture C1 T1")))

    @classmethod
    def tearDownClass(cls):
        cls.helper.doCleanups()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.packet, self.report = self.root / "packet", self.root / "report"
        prepare_review(self.source, self.repository, self.packet, purpose="simulation")

    def rows(self):
        return [read_json(line) for line in (self.packet / "reviews.jsonl").read_text().splitlines()]

    def save(self, rows):
        (self.packet / "reviews.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")

    def reviewed(self, row, ratings=None):
        return dict(row, status="reviewed", ratings=ratings or dict(correctness=2, evidence=2, completeness=2, boundaries=2),
                    reviewer="local-review-fixture", rationale="合成评分记录，仅验证导入和算分，不代表人工完成了语义评审")

    def submit(self):
        return import_review(self.source, self.repository, self.packet, self.report)

    def reseal(self):
        (self.report / "report-seal.json").unlink()
        write_json(self.report / "report-seal.json", {"format_version": 1, "sha256": _inventory(self.report)})

    def test_packet_has_full_context_gold_rubric_and_no_prefilled_scores(self):
        context = read_json((self.packet / "context.json").read_bytes())
        self.assertEqual(len(context["cases"][0]["turns"]), 2)
        self.assertTrue(context["cases"][0]["turns"][0]["checked_answer"])
        self.assertTrue(context["cases"][0]["turns"][0]["expected"]["checkpoints"])
        self.assertTrue(all(row["ratings"] is None for row in self.rows()))

    def test_unedited_packet_is_a_pending_report_without_final_metrics(self):
        receipt = self.submit()
        report = read_json((self.report / "report.json").read_bytes())
        self.assertFalse(receipt["finalized"])
        self.assertEqual(report["summary"]["reviewed_turns"], 0)
        self.assertIsNone(report["summary"]["mean_score"])
        self.assertTrue(all(row["pass_rate"] is None for row in report["categories"]))

    def test_partial_subset_keeps_denominator_and_known_failures_visible(self):
        row = self.reviewed(self.rows()[0], dict(correctness=0, evidence=0, completeness=0, boundaries=2))
        self.save([row])
        receipt = self.submit()
        report = read_json((self.report / "report.json").read_bytes())
        self.assertEqual((receipt["required_turns"], receipt["reviewed_turns"]), (5, 1))
        self.assertEqual(len((self.report / "merged-reviews.jsonl").read_text().splitlines()), 5)
        self.assertFalse(receipt["finalized"])
        self.assertIsNone(report["summary"]["pass_rate"])
        self.assertEqual(receipt["known_badcase_count"], 1)
        bad = read_json((self.report / "badcases.jsonl").read_text().splitlines()[0])
        self.assertEqual(len(bad["turns"]), 2)
        self.assertIsNone(bad["passed"])

    def test_full_report_uses_case_macro_average_and_four_category_totals(self):
        rows = [self.reviewed(row) for row in self.rows()]
        rows[1]["ratings"].update(completeness=1, boundaries=1)
        self.save(rows)
        receipt = self.submit()
        report = read_json((self.report / "report.json").read_bytes())
        self.assertTrue(receipt["finalized"])
        self.assertEqual(report["summary"]["mean_score"], 98.125)
        self.assertEqual(report["summary"]["cases"][0]["score"], 92.5)
        self.assertEqual(len(report["categories"]), 4)
        self.assertEqual(report["report_kind"], "synthetic_review_demo")
        self.assertFalse(report["real_model_quality_evaluated"])

    def test_fatal_error_zeros_turn_and_produces_a_bound_badcase(self):
        rows = [self.reviewed(row) for row in self.rows()]
        rows[0]["fatal_errors"] = ["wrong_source"]
        self.save(rows)
        self.submit()
        report = read_json((self.report / "report.json").read_bytes())
        self.assertEqual(report["cases"][0]["turns"][0]["score"], 0)
        self.assertFalse(report["cases"][0]["passed"])
        self.assertEqual(report["cases"][0]["turns"][0]["review"]["output_sha256"], rows[0]["output_sha256"])
        text = (self.report / "report.md").read_text()
        self.assertIn("合成评分流程样例", text)
        self.assertIn("对话上下文", text)
        self.assertIn("https://github.com/example/eval-fixture/blob/", text)

    def test_wrong_run_dataset_archive_and_rubric_bindings_are_rejected(self):
        path = self.packet / "review.json"
        original = path.read_bytes()
        for field in ("source_run_id", "source_archive_sha256", "dataset_fingerprint", "rubric_sha256"):
            data = read_json(original)
            data[field] = "wrong-run" if field == "source_run_id" else "a" * 64
            path.write_text(json.dumps(data))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "another run"):
                self.submit()
            self.assertFalse(self.report.exists())
        path.write_bytes(original)

    def test_wrong_output_and_execution_state_downgrade_are_rejected(self):
        original = self.rows()
        row = self.reviewed(original[0])
        row["output_sha256"] = "a" * 64
        self.save([row])
        with self.assertRaisesRegex(ValueError, "another output"):
            self.submit()
        row = dict(original[0], status="not_run", output_sha256=None)
        self.save([row])
        with self.assertRaisesRegex(ValueError, "execution state"):
            self.submit()

    def test_duplicate_unknown_ids_and_invalid_rating_records_are_rejected(self):
        original = self.rows()
        for rows in ([original[0], original[0]], [dict(original[0], case_id="unknown")]):
            self.save(rows)
            with self.assertRaisesRegex(ValueError, "Duplicate or unknown"):
                self.submit()
        for field, value in (("correctness", True), ("evidence", 3)):
            row = self.reviewed(original[0])
            row["ratings"][field] = value
            self.save([row])
            with self.assertRaises(ValueError):
                self.submit()
        row = self.reviewed(original[0])
        row["fatal_errors"] = ["unknown-code"]
        self.save([row])
        with self.assertRaisesRegex(ValueError, "Unknown fatal"):
            self.submit()

    def test_reviewed_rows_need_reviewer_rationale_and_output_hash(self):
        original = self.rows()[0]
        for field in ("reviewer", "output_sha256", "rationale"):
            row = self.reviewed(original)
            row[field] = "" if field == "rationale" else None
            self.save([row])
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.submit()

    def test_context_or_rubric_cannot_be_changed_to_match_a_preferred_score(self):
        context = self.packet / "context.json"
        original = context.read_bytes()
        data = read_json(original)
        data["cases"][0]["turns"][0]["expected"]["checkpoints"] = ["invented expected answer"]
        context.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "Reviewer context"):
            self.submit()
        context.write_bytes(original)
        (self.packet / "rubric.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "frozen rubric"):
            self.submit()

    def test_failed_and_unexecuted_turns_cannot_be_manually_turned_into_outputs(self):
        packet = self.root / "failure-packet"
        prepare_review(self.failed, self.repository, packet, purpose="simulation")
        rows = [read_json(line) for line in (packet / "reviews.jsonl").read_text().splitlines()]
        for index in (0, 1, 2):
            row = self.reviewed(rows[index])
            row.update(output_sha256="a" * 64, error=None)
            (packet / "reviews.jsonl").write_text(json.dumps(row) + "\n")
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "uncompleted"):
                import_review(self.failed, self.repository, packet, self.report)

    def test_failure_reasons_and_not_run_denominator_are_preserved(self):
        packet = self.root / "failure-packet"
        prepare_review(self.failed, self.repository, packet, purpose="simulation")
        receipt = import_review(self.failed, self.repository, packet, self.report)
        data = read_json((self.report / "report.json").read_bytes())
        self.assertFalse(receipt["finalized"])
        self.assertEqual(data["summary"]["execution_failed_turns"], 2)
        self.assertEqual(data["summary"]["required_turns"], 5)
        self.assertIn("LOCAL_FIXTURE_FAILURE_AFTER_TOOL", data["cases"][0]["turns"][0]["review"]["error"])

    def test_human_declared_reviews_of_local_outputs_do_not_become_real_model_metrics(self):
        packet = self.root / "human-packet"
        prepare_review(self.source, self.repository, packet, purpose="human")
        result = import_review(self.source, self.repository, packet, self.report)
        self.assertEqual(result["report_kind"], "human_review_of_local_fixture")
        self.assertFalse(result["real_model_quality_evaluated"])

    def test_source_archive_is_unchanged_and_existing_outputs_are_not_overwritten(self):
        before = (self.source / "archive.json").read_bytes()
        self.submit()
        report_bytes = (self.report / "report-seal.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.submit()
        with self.assertRaises(FileExistsError):
            prepare_review(self.source, self.repository, self.packet)
        self.assertEqual((self.source / "archive.json").read_bytes(), before)
        self.assertEqual((self.report / "report-seal.json").read_bytes(), report_bytes)
        self.assertTrue(all(row["status"] == "pending_review" for row in
                            [read_json(line) for line in (self.source / "reviews.jsonl").read_text().splitlines()]))

    def test_reports_and_packets_cannot_be_written_inside_the_sealed_source(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            prepare_review(self.source, self.repository, self.source / "packet")
        with self.assertRaisesRegex(ValueError, "outside"):
            import_review(self.source, self.repository, self.packet, self.packet / "report")

    def test_report_rejects_changed_scores_even_after_inventory_is_resealed(self):
        self.save([self.reviewed(row) for row in self.rows()])
        self.submit()
        path = self.report / "report.json"
        report = read_json(path.read_bytes())
        report["summary"]["mean_score"] = 1
        path.write_text(json.dumps(report))
        self.reseal()
        with self.assertRaisesRegex(ValueError, "scores or provenance"):
            verify_report(self.report, self.source, self.repository)

    def test_report_rejects_rendered_or_badcase_changes_and_missing_seal(self):
        self.submit()
        path = self.report / "report.md"
        original = path.read_bytes()
        path.write_text("false report")
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify_report(self.report, self.source, self.repository)
        path.write_bytes(original)
        self.reseal()
        (self.report / "badcases.jsonl").write_text('{"invented":"case"}\n')
        self.reseal()
        with self.assertRaisesRegex(ValueError, "badcase"):
            verify_report(self.report, self.source, self.repository)
        (self.report / "report-seal.json").unlink()
        with self.assertRaises(FileNotFoundError):
            verify_report(self.report, self.source, self.repository)

    def test_review_text_is_escaped_and_never_executed(self):
        row = self.reviewed(self.rows()[0], dict(correctness=0, evidence=0, completeness=0, boundaries=0))
        marker = self.root / "executed"
        row["rationale"] = f"![remote](https://example.invalid/x) <script> touch {marker}"
        self.save([row])
        self.submit()
        text = (self.report / "report.md").read_text()
        self.assertNotIn("![remote]", text)
        self.assertNotIn("<script>", text)
        self.assertFalse(marker.exists())

    def test_report_is_verified_in_a_fresh_process_without_editing_the_packet(self):
        self.save([self.reviewed(row) for row in self.rows()])
        receipt = self.submit()
        shutil.rmtree(self.packet)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/review_evaluation.py"), "verify",
                                 "--archive", str(self.source), "--repository", str(self.repository), "--report", str(self.report)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), receipt)


if __name__ == "__main__":
    unittest.main()
