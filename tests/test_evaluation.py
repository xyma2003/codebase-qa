from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from git import Actor, Repo
from pydantic import ValidationError

from evaluation.dataset import PAYLOADS, Review, Rubric, read_json, validate_bundle
from evaluation.scoring import summarize_reviews

ROOT = Path(__file__).resolve().parents[1]


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.work, self.data = self.root / "repo", self.root / "dataset"
        self.data.mkdir()
        self.repo = Repo.init(self.work)
        self.addCleanup(self.repo.close)
        self.repo.create_remote("origin", "https://github.com/example/eval-fixture.git")
        marker = self.root / "executed"
        source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('BAD')\nVALUE = 1\n"
        (self.work / "sample.py").write_text(source)
        (self.work / "link.py").symlink_to("sample.py")
        self.repo.index.add(["sample.py", "link.py"])
        actor = Actor("Fixture", "fixture@example.invalid")
        commit = self.repo.index.commit("fixed source", author=actor, committer=actor)
        self.cases = []
        self.gold = []
        self.reviews = []
        for number, category in enumerate(("understanding", "localization", "planning", "unanswerable"), 1):
            identity = f"C{number}"
            turns = [dict(turn_id="T1", mode="qa", question="固定问题")]
            if number == 1:
                turns.append(dict(turn_id="T2", mode="qa", question="继续追问"))
            self.cases.append(dict(case_id=identity, category=category, source_id="fixture", turns=turns))
            self.gold.append(dict(case_id=identity, turns=[
                dict(turn_id=turn["turn_id"], checkpoints=["检查一", "检查二"], evidence_required=True,
                     evidence_groups=[dict(group_id="G1", any_of=["E1"])], forbidden_claims=["不能编造"],
                     expected_touchpoints=["sample.py"], review_note="核对结论含义") for turn in turns]))
            self.reviews += [dict(case_id=identity, turn_id=turn["turn_id"], status="not_run", ratings=None,
                                  fatal_errors=[], output_sha256=None, reviewer=None, rationale="", error=None) for turn in turns]
        self.evidence = [dict(evidence_id="E1", source_id="fixture", file_path="sample.py", start_line=3,
                              end_line=3, source_text="VALUE = 1", file_sha256=hashlib.sha256(source.encode()).hexdigest())]
        self.rubric = read_json((ROOT / "evals/codebase-qa-v1/rubric.json").read_bytes())
        self.manifest = dict(dataset_id="fixture", version=1, split="development",
                             sources=[dict(source_id="fixture", repo_url="https://github.com/example/eval-fixture",
                                           commit_sha=commit.hexsha, tree_sha=commit.tree.hexsha)],
                             case_count=4, turn_count=5,
                             category_counts={case["category"]: 1 for case in self.cases}, payload_sha256={})
        self.save()

    def save(self):
        for name, rows in (("cases.jsonl", self.cases), ("expectations.jsonl", self.gold),
                           ("evidence.jsonl", self.evidence), ("review-template.jsonl", self.reviews)):
            (self.data / name).write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
        (self.data / "rubric.json").write_text(json.dumps(self.rubric, ensure_ascii=False))
        self.manifest["payload_sha256"] = {name: hashlib.sha256((self.data / name).read_bytes()).hexdigest() for name in PAYLOADS}
        (self.data / "manifest.json").write_text(json.dumps(self.manifest))

    def load(self):
        return validate_bundle(self.data, self.work)

    def completed(self):
        return [dict(row, status="reviewed", ratings=dict(correctness=2, evidence=2, completeness=2, boundaries=2),
                     output_sha256="a" * 64, reviewer="fixture-reviewer", rationale="合成评分记录，仅验证算分规则") for row in self.reviews]

    def test_frozen_git_evidence_ignores_dirty_worktree_and_never_executes_source(self):
        (self.work / "sample.py").write_text("UNCOMMITTED = 99\n")
        bundle = self.load()
        self.assertEqual(bundle.evidence[0].source_text, "VALUE = 1")
        self.assertFalse((self.root / "executed").exists())

    def test_model_inputs_exclude_gold_categories_evidence_and_reviewer_data(self):
        for row in self.load().model_inputs():
            self.assertEqual(set(row), {"case_id", "turns", "source"})
            self.assertEqual(set(row["turns"][0]), {"turn_id", "mode", "question"})
            self.assertNotIn("检查一", json.dumps(row, ensure_ascii=False))

    def test_payload_changes_require_manifest_update(self):
        with (self.data / "cases.jsonl").open("a") as file:
            file.write("{}\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.load()

    def test_duplicate_case_gold_evidence_and_turn_ids_are_rejected(self):
        for field in ("cases", "gold", "evidence"):
            rows = getattr(self, field)
            rows.append(deepcopy(rows[0]))
            self.save()
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load()
            rows.pop()
        self.cases[0]["turns"][1]["turn_id"] = "T1"
        self.save()
        with self.assertRaisesRegex(ValueError, "Turn IDs"):
            self.load()

    def test_missing_or_extra_expected_turn_is_rejected(self):
        self.gold[0]["turns"].pop()
        self.save()
        with self.assertRaisesRegex(ValueError, "Expected turns"):
            self.load()

    def test_case_turn_and_category_counts_are_fixed(self):
        for field in ("case_count", "turn_count"):
            self.manifest[field] += 1
            self.save()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "count mismatch"):
                self.load()
            self.manifest[field] -= 1
        self.manifest["category_counts"].pop("planning")
        self.save()
        with self.assertRaisesRegex(ValueError, "Category"):
            self.load()

    def test_unknown_cross_source_and_unused_evidence_are_rejected(self):
        self.gold[0]["turns"][0]["evidence_groups"][0]["any_of"] = ["E99"]
        self.save()
        with self.assertRaisesRegex(ValueError, "Unknown or cross-version"):
            self.load()
        self.gold[0]["turns"][0]["evidence_groups"][0]["any_of"] = ["E1"]
        self.evidence.append(dict(self.evidence[0], evidence_id="E2"))
        self.save()
        with self.assertRaisesRegex(ValueError, "Unused evidence"):
            self.load()
        self.evidence.pop()
        self.evidence[0]["source_id"] = "different-version"
        self.save()
        with self.assertRaisesRegex(ValueError, "Unknown or cross-version"):
            self.load()

    def test_wrong_evidence_text_hash_and_out_of_bounds_lines_are_rejected(self):
        original = deepcopy(self.evidence[0])
        for field, value in (("source_text", "VALUE = 999"), ("file_sha256", "a" * 64), ("end_line", 999)):
            self.evidence[0] = dict(original, **{field: value})
            self.save()
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load()

    def test_source_paths_reject_escape_metadata_and_symlink_blobs(self):
        for path in ("../sample.py", "/tmp/sample.py", "C:/sample.py", ".git/config", "./sample.py", "link.py"):
            self.evidence[0]["file_path"] = path
            self.save()
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.load()

    def test_wrong_git_origin_tree_and_missing_touchpoints_are_rejected(self):
        self.manifest["sources"][0]["repo_url"] = "https://github.com/other/fixture"
        self.save()
        with self.assertRaisesRegex(ValueError, "origin"):
            self.load()
        self.manifest["sources"][0]["repo_url"] = "https://github.com/example/eval-fixture"
        self.manifest["sources"][0]["tree_sha"] = "a" * 40
        self.save()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.load()
        self.manifest["sources"][0]["tree_sha"] = self.repo.head.commit.tree.hexsha
        self.gold[0]["turns"][0]["expected_touchpoints"] = ["absent.py"]
        self.save()
        with self.assertRaises(KeyError):
            self.load()

    def test_review_template_cannot_prepopulate_execution_or_scores(self):
        self.reviews[0] = self.completed()[0]
        self.save()
        with self.assertRaisesRegex(ValueError, "unexecuted"):
            self.load()

    def test_extra_input_fields_blank_jsonl_and_duplicate_json_keys_are_rejected(self):
        self.cases[0]["expected_answer"] = "leaked gold"
        self.save()
        with self.assertRaises(ValidationError):
            self.load()
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            read_json('{"id":1,"id":2}')
        self.cases[0].pop("expected_answer")
        self.save()
        path = self.data / "cases.jsonl"
        path.write_text(path.read_text() + "\n")
        self.manifest["payload_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.data / "manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "blank lines"):
            self.load()

    def test_rubric_requires_weights_all_anchors_and_all_passing_rules(self):
        for edit in (lambda r: r["dimensions"][0].update(weight=99),
                     lambda r: r["dimensions"][0]["anchors"].pop("1"),
                     lambda r: r["minimum_passing_ratings"].pop("evidence"),
                     lambda r: r["dimensions"].append(deepcopy(r["dimensions"][0]))):
            rubric = deepcopy(self.rubric)
            edit(rubric)
            with self.assertRaises(ValidationError):
                Rubric.model_validate(rubric)

    def test_unrun_or_pending_reviews_do_not_publish_zero_or_full_accuracy(self):
        bundle = self.load()
        for records in (self.reviews, [dict(row, status="pending_review", output_sha256="a" * 64) for row in self.reviews]):
            summary = summarize_reviews(bundle, records)
            self.assertFalse(summary["finalized"])
            self.assertIsNone(summary["mean_score"])
            self.assertIsNone(summary["pass_rate"])

    def test_synthetic_scores_follow_weights_and_all_turns_must_pass(self):
        rows = self.completed()
        rows[1]["ratings"] = dict(correctness=2, evidence=2, completeness=1, boundaries=1)
        summary = summarize_reviews(self.load(), rows)
        self.assertEqual(summary["cases"][0]["score"], 92.5)  # Mean of 100 and 85.
        self.assertEqual(summary["pass_rate"], 1)
        rows[1]["ratings"]["correctness"] = 1
        summary = summarize_reviews(self.load(), rows)
        self.assertFalse(summary["cases"][0]["passed"])
        self.assertEqual(summary["pass_rate"], 0.75)

    def test_execution_failure_and_fatal_error_stay_in_original_denominator(self):
        rows = self.completed()
        rows[1] = dict(self.reviews[1], status="execution_failed", error="fixture: prior tool failed")
        rows[2]["fatal_errors"] = ["wrong_source"]
        summary = summarize_reviews(self.load(), rows)
        self.assertEqual((summary["required_cases"], summary["required_turns"]), (4, 5))
        self.assertEqual(summary["cases"][0]["score"], 50)
        self.assertEqual(summary["cases"][1]["score"], 0)
        self.assertEqual(summary["pass_rate"], 0.5)
        self.assertEqual(summary["mean_score"], 62.5)

    def test_missing_duplicate_reviews_and_unknown_fatal_codes_are_rejected(self):
        bundle, rows = self.load(), self.completed()
        for records in (rows[:-1], rows + [rows[0]]):
            with self.assertRaisesRegex(ValueError, "denominator"):
                summarize_reviews(bundle, records)
        rows[0]["fatal_errors"] = ["unknown-error"]
        with self.assertRaisesRegex(ValueError, "Unknown fatal"):
            summarize_reviews(bundle, rows)

    def test_reviews_reject_boolean_ratings_or_unbound_human_conclusions(self):
        rows = self.completed()
        rows[0]["ratings"]["correctness"] = True
        with self.assertRaises(ValidationError):
            Review.model_validate(rows[0])
        for field in ("output_sha256", "reviewer", "ratings"):
            row = dict(self.completed()[0], **{field: None})
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Review.model_validate(row)
        row = dict(self.reviews[0], status="pending_review", output_sha256="a" * 64, rationale="unreviewed conclusion")
        with self.assertRaises(ValidationError):
            Review.model_validate(row)


if __name__ == "__main__":
    unittest.main()
