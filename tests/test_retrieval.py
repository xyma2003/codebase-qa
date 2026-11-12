from types import SimpleNamespace
import hashlib
import unittest
from unittest.mock import patch

import chromadb

from evaluation.retrieval import evidence_coverage, group_coverage, _summary
from indexer.chunker import chunk_repo
from indexer.module_chunks import module_supplements
from indexer.snapshots import create_snapshot
from tests import test_evaluation as fixtures
from tests.test_real_evaluation import FixtureEmbedding
from evaluation.indexes import build_indexes
from evaluation.retrieval import compare_module_chunks
from indexer import vector_store


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EvaluationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_module_supplements_preserve_original_chunks_and_cover_imports_and_constants(self):
        from git import Actor
        source = self.fixture.work / "sample.py"
        source.write_text("import os\nVALUE = 1\n\ndef alpha():\n    return VALUE\n\nFLAG = True\n")
        self.fixture.repo.index.add(["sample.py"])
        actor = Actor("Fixture", "fixture@example.invalid")
        commit = self.fixture.repo.index.commit("module context", author=actor, committer=actor)
        snapshot = create_snapshot("https://github.com/example/eval-fixture", str(self.fixture.work), commit.hexsha,
                                   storage_dir=self.fixture.root / "snapshots")
        original = list(chunk_repo(snapshot.root))
        before = [(chunk.file_path, chunk.start_line, chunk.end_line, chunk.content) for chunk in original]
        extra = module_supplements(snapshot, original)
        self.assertEqual(before, [(chunk.file_path, chunk.start_line, chunk.end_line, chunk.content) for chunk in original])
        self.assertTrue(any("import os" in chunk.content for chunk in extra))
        self.assertTrue(any("FLAG = True" in chunk.content for chunk in extra))
        for chunk in extra:
            self.assertEqual(chunk.chunk_type, "module")
            self.assertEqual(chunk.content, "\n".join(source.read_text().splitlines()[chunk.start_line-1:chunk.end_line]))
            self.assertFalse(any(chunk.start_line <= old.end_line and old.start_line <= chunk.end_line for old in original))

    def test_files_without_definitions_do_not_gain_duplicate_module_chunks(self):
        snapshot = create_snapshot("https://github.com/example/eval-fixture", str(self.fixture.work),
                                   storage_dir=self.fixture.root / "snapshots")
        self.assertEqual(module_supplements(snapshot, list(chunk_repo(snapshot.root))), [])

    def test_full_coverage_requires_contiguous_source_and_matching_file(self):
        evidence = SimpleNamespace(file_path="a.py", start_line=10, end_line=20)
        hit = lambda start, end, name="a.py": {"file_path": name, "start_line": start, "end_line": end}
        self.assertEqual(evidence_coverage([hit(10, 12)], evidence), {"overlap": True, "full_coverage": False})
        self.assertEqual(evidence_coverage([hit(10, 14), hit(15, 20)], evidence), {"overlap": True, "full_coverage": True})
        self.assertEqual(evidence_coverage([hit(10, 13), hit(15, 20)], evidence), {"overlap": True, "full_coverage": False})
        self.assertEqual(evidence_coverage([hit(1, 50, "other.py")], evidence), {"overlap": False, "full_coverage": False})

    def test_alternative_evidence_and_clarification_denominator_are_explicit(self):
        evidence = {"A": SimpleNamespace(file_path="a.py", start_line=1, end_line=2),
                    "B": SimpleNamespace(file_path="b.py", start_line=1, end_line=2)}
        groups = [SimpleNamespace(group_id="G", any_of=["A", "B"])]
        actual = group_coverage([{"file_path": "b.py", "start_line": 1, "end_line": 2}], groups, evidence)
        self.assertTrue(actual[0]["full_coverage"])
        summary = _summary([{"evidence_required": True, "groups": actual}, {"evidence_required": False, "groups": []}])
        self.assertEqual((summary["required_turns"], summary["required_groups"], summary["group_full_coverage_recall"]), (1, 1, 1.0))


class RetrievalIndexTests(unittest.TestCase):
    def setUp(self):
        from git import Actor
        self.fixture = fixtures.EvaluationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        source = self.fixture.work / "sample.py"
        source.write_text(source.read_text() + "\ndef alpha():\n    return VALUE\n")
        self.fixture.repo.index.add(["sample.py"])
        actor = Actor("Fixture", "fixture@example.invalid")
        commit = self.fixture.repo.index.commit("module retrieval fixture", author=actor, committer=actor)
        self.fixture.manifest["sources"][0].update(commit_sha=commit.hexsha, tree_sha=commit.tree.hexsha)
        self.fixture.evidence[0]["file_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        self.fixture.save()
        self.client = chromadb.PersistentClient(path=str(self.fixture.root / "vectors"))
        for context in (patch.object(vector_store, "_get_client", return_value=self.client),
                        patch.object(vector_store, "_get_embed_fn", return_value=FixtureEmbedding())):
            context.start()
            self.addCleanup(context.stop)
        self.indexes = self.fixture.root / "indexes"
        build_indexes(self.fixture.data, self.fixture.work, self.indexes)

    def test_actual_index_comparison_reuses_vectors_covers_module_and_leaves_defaults(self):
        report = compare_module_chunks(self.fixture.data, self.fixture.work, self.indexes, self.fixture.root / "comparison")
        self.assertEqual(report["variants"]["baseline"]["summary"]["fully_covered_groups"], 0)
        self.assertEqual(report["variants"]["module_supplement"]["summary"]["group_full_coverage_recall"], 1.0)
        self.assertEqual(report["counts"][0]["reused_vectors"], report["counts"][0]["baseline_chunks"])
        self.assertFalse(report["production_default_changed"])
        self.assertFalse(report["answer_quality_evaluated"])
        self.assertFalse((self.fixture.root / "executed").exists())
        candidate = report["counts"][0]["candidate_collection"]
        with patch.object(vector_store, "build_collection", side_effect=AssertionError("DO_NOT_REBUILD")):
            reused = compare_module_chunks(self.fixture.data, self.fixture.work, self.indexes, self.fixture.root / "reused",
                                          candidate_collections={"fixture": candidate})
        self.assertTrue(reused["counts"][0]["existing_candidate_reused"])
        before = len(self.client.list_collections())
        with self.assertRaises(FileExistsError):
            compare_module_chunks(self.fixture.data, self.fixture.work, self.indexes, self.fixture.root / "comparison")
        self.assertEqual(len(self.client.list_collections()), before)

    def test_damaged_successful_index_is_rejected_before_creating_candidate(self):
        name = self.client.list_collections()[0].name
        collection = self.client.get_collection(name, embedding_function=None)
        collection.update(ids=[collection.get()["ids"][0]], documents=["changed"])
        destination = self.fixture.root / "comparison"
        with self.assertRaisesRegex(ValueError, "向量存储"):
            compare_module_chunks(self.fixture.data, self.fixture.work, self.indexes, destination)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
