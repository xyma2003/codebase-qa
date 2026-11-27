import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from indexer import vector_store
from indexer.sqlite_vectors import LocalVectorClient
from scripts import check_real_evaluation as checker


class SimulationIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app_directory = self.root / 'application'
        self.application = LocalVectorClient(self.app_directory)
        collection = self.application.create_collection('existing', embedding_function=None)
        collection.add(ids=['original'], documents=['existing application text'], embeddings=[[1., 2.]])
        self.enterContext(patch.object(vector_store, 'CHROMA_DIR', str(self.app_directory)))

    def application_rows(self):
        with self.application.connect() as db:
            return {table: db.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()
                    for table in ('vector_collections', 'vector_records')}

    def write_evaluation_vectors(self, directory, package):
        collection = vector_store._get_client().create_collection('evaluation-fixture', embedding_function=None)
        collection.add(ids=['test'], documents=['local fixture'], embeddings=[[3., 4.]])
        return {'fixture_saved': True}

    def test_simulated_check_keeps_real_vector_writes_out_of_application_storage(self):
        before = self.application_rows()
        destination = self.root / 'check'
        with patch.object(checker, '_check', side_effect=self.write_evaluation_vectors):
            result = checker.check(destination, self.root / 'package.zip')
        self.assertEqual(result, {'fixture_saved': True})
        self.assertEqual(self.application_rows(), before)
        isolated = LocalVectorClient(destination / 'vectors')
        self.assertEqual(isolated.get_collection('evaluation-fixture', embedding_function=None).get()['ids'], ['test'])
        self.assertEqual([item.name for item in vector_store._get_client().list_collections()], ['existing'])

    def test_failed_simulation_retains_its_evidence_and_restores_the_default_client(self):
        before = self.application_rows()
        destination = self.root / 'failed-check'
        def fail(directory, package):
            self.write_evaluation_vectors(directory, package)
            raise RuntimeError('ORIGINAL_SIMULATION_ERROR')
        with patch.object(checker, '_check', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'ORIGINAL_SIMULATION_ERROR'):
                checker.check(destination, self.root / 'package.zip')
        self.assertEqual(self.application_rows(), before)
        self.assertEqual([item.name for item in vector_store._get_client().list_collections()], ['existing'])
        isolated = LocalVectorClient(destination / 'vectors')
        self.assertEqual(isolated.get_collection('evaluation-fixture', embedding_function=None).get()['ids'], ['test'])


if __name__ == '__main__':
    unittest.main()
