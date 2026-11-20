import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from indexer import vector_store
from indexer.sqlite_vectors import LocalVectorClient
from memory import knowledge
from ops.vector_migration import import_export, verify_export
from security.vector_client import local_vector_client


class LocalVectorClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = LocalVectorClient(self.root / 'vectors')

    def collection(self, **kwargs):
        return self.client.create_collection('source', **kwargs)

    def test_environment_and_persisted_plugins_cannot_load_code_or_network(self):
        poison = {'CHROMA_API_IMPL': 'untrusted.Client', 'CHROMA_SYSDB_IMPL': 'untrusted.Database',
                  'CHROMA_PRODUCT_TELEMETRY_IMPL': 'untrusted.Telemetry', 'ALLOW_RESET': 'true'}
        with patch.dict(os.environ, poison):
            collection = self.collection(configuration={'hnsw': {'space': 'l2'}, 'embedding_function': {'name': 'untrusted.Loader', 'config': {'trust_remote_code': True}}})
            collection.add(ids=['a'], documents=['safe'], embeddings=[[1., 0.]])
            self.assertEqual(collection.query(query_embeddings=[[1., 0.]])['ids'], [['a']])
            with self.assertRaises(ValueError):
                collection.query(query_texts=['cannot hydrate persisted configuration'])
            with self.assertRaises(PermissionError):
                self.client.reset()
        self.assertFalse(any(x.startswith('untrusted') for x in sys.modules))

    def test_restart_readback_and_application_import_never_load_chroma(self):
        collection = self.collection()
        collection.add(ids=['a'], documents=['原始文本'], metadatas=[{'owner_id': 'alice'}], embeddings=[[1., 2.]])
        code = """from security.vector_client import local_vector_client
from indexer import vector_store
from memory import knowledge
import sys
c=local_vector_client(sys.argv[1]).get_collection('source')
assert c.get()['documents']==['原始文本']
assert c.query(query_embeddings=[[1.,2.]])['distances']==[[0.0]]
assert not any(n=='chromadb' or n.startswith('chromadb.') for n in sys.modules)
print('SQLITE_ONLY')
"""
        result = subprocess.run([sys.executable, '-c', code, str(self.root / 'vectors')], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'SQLITE_ONLY')

    def test_both_source_and_private_cache_use_same_local_boundary(self):
        for module in (vector_store, knowledge):
            with patch.object(module, 'CHROMA_DIR', 'fixture-directory'), patch.object(module, 'local_vector_client', return_value='fixture-client') as factory:
                self.assertEqual(module._get_client(), 'fixture-client')
                factory.assert_called_once_with('fixture-directory')

    def test_real_legacy_database_requires_migration_before_access(self):
        directory = self.root / 'legacy'
        directory.mkdir()
        (directory / 'chroma.sqlite3').write_bytes(b'legacy fixture')
        with self.assertRaisesRegex(ValueError, '迁移'):
            local_vector_client(directory)
        self.assertFalse((directory / 'vectors.sqlite3').exists())

    def test_distance_metrics_have_known_values_and_stable_ties(self):
        for metric, expected in (('l2', [0., 1., 2.]), ('cosine', [0., 0., 1.]), ('ip', [-1., 0., 1.])):
            c = self.client.create_collection(metric, configuration={'hnsw': {'space': metric}})
            c.add(ids=['unit', 'scaled', 'orthogonal'], documents=['x', 'y', 'z'], embeddings=[[1.,0.],[2.,0.],[0.,1.]])
            result = c.query(query_embeddings=[[1.,0.]], n_results=3)
            self.assertTrue(np.allclose(result['distances'][0], expected), (metric, result))
            if metric == 'cosine':
                self.assertEqual(result['ids'][0], ['unit', 'scaled', 'orthogonal'])
        zero = self.client.get_collection('cosine').query(query_embeddings=[[0.,0.]])
        self.assertTrue(all(x == 1. for x in zero['distances'][0]))

    def test_atomic_invalid_duplicate_and_oversized_batches_leave_original(self):
        c = self.collection()
        c.add(ids=['original'], documents=['text'], embeddings=[[1.,0.]])
        for args in ({'ids':['new','bad'], 'documents':['x','y'], 'embeddings':[[1.,0.],[1.,2.,3.]]},
                     {'ids':['new','original'], 'documents':['x','y'], 'embeddings':[[1.,0.],[1.,0.]]},
                     {'ids':['new'], 'documents':['x'], 'embeddings':[[float('nan'),0.]]}):
            with self.assertRaises(ValueError):
                c.add(**args)
            self.assertEqual(c.get()['ids'], ['original'])
        with patch('indexer.sqlite_vectors.MAX_COLLECTION_BYTES', 1), self.assertRaises(ValueError):
            c.add(ids=['new'], documents=['x'], embeddings=[[1.,0.]])
        self.assertEqual(c.count(), 1)
        with self.assertRaises(KeyError):
            c.update(ids=['original','missing'], metadatas=[{'x':1},{'x':2}])
        self.assertEqual(c.get()['metadatas'], [{}])

    def test_metadata_merge_approval_filter_pagination_and_sql_parameters(self):
        c = self.collection(metadata={'repo_url':'https://github.com/example/fixture'})
        c.add(ids=['a','b'], documents=['one','two'], metadatas=[{'owner_id':'alice','approval_status':'pending'},{'owner_id':'bob','approval_status':'approved'}], embeddings=[[1.,0.],[0.,1.]])
        c.update(ids=['a'], metadatas=[{'approval_status':'approved','hits':1}])
        self.assertEqual(c.get(ids=['a'])['metadatas'][0]['owner_id'], 'alice')
        self.assertEqual(c.query(query_embeddings=[[1.,0.]], where={'owner_id':'alice','approval_status':'approved'})['ids'], [['a']])
        self.assertEqual(c.get(limit=1, offset=1, include=[])['ids'], ['b'])
        self.assertEqual(c.get(where={'owner_id': "' OR 1=1 --"})['ids'], [])
        injection = self.client.create_collection("'; DROP TABLE vector_records; --")
        self.assertEqual(c.count(), 2)
        self.assertEqual(injection.count(), 0)

    def test_stale_handle_cannot_read_or_overwrite_replacement(self):
        c = self.collection()
        self.client.delete_collection('source')
        self.collection()
        with self.assertRaises(ValueError):
            c.get()
        with self.assertRaises(ValueError):
            c.add(ids=['a'], documents=['x'], embeddings=[[1.,0.]])
        self.assertEqual(self.client.get_collection('source').count(), 0)

    def test_symlinks_and_nonfinite_or_structured_metadata_are_rejected(self):
        (self.root/'linked').symlink_to(self.root/'vectors', target_is_directory=True)
        with self.assertRaises(ValueError):
            LocalVectorClient(self.root/'linked')
        self.assertEqual(self.client.path.stat().st_mode & 0o777, 0o600)
        c = self.collection()
        for meta in ({'x':float('nan')}, {'x':{'class':'untrusted'}}):
            with self.assertRaises(ValueError):
                c.add(ids=['a'], documents=['x'], embeddings=[[1.,0.]], metadatas=[meta])
        for query in ({'query_embeddings':[[1.]], 'n_results':0}, {'query_embeddings':[[1.]], 'where':{'x':{'$ne':'alice'}}}):
            with self.assertRaises(ValueError):
                c.query(**query)

    def test_concurrent_writers_preserve_all_distinct_records(self):
        c = self.collection()
        def add(index):
            c.add(ids=[str(index)], documents=[str(index)], embeddings=[[1.,float(index)]])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(add, range(12)))
        self.assertEqual(c.count(), 12)

    def test_concurrent_initialization_and_private_file_and_schema_guards(self):
        directory = self.root/'concurrent'
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            clients = list(executor.map(lambda _: LocalVectorClient(directory), range(8)))
        self.assertEqual(len(clients), 8)
        self.client.path.chmod(0o644)
        with self.assertRaises(ValueError):
            LocalVectorClient(self.root/'vectors')
        self.client.path.chmod(0o600)
        with self.client.connect() as db:
            db.execute('PRAGMA user_version=999')
        with self.assertRaises(ValueError):
            LocalVectorClient(self.root/'vectors')

    def test_large_finite_vectors_return_finite_distances(self):
        c = self.collection()
        c.add(ids=['large'], documents=['x'], embeddings=[[3e38,3e38]])
        result = c.query(query_embeddings=[[-3e38,-3e38]])
        self.assertTrue(np.isfinite(result['distances'][0][0]))

    def test_embedding_parameters_match_the_existing_model_and_ignore_stored_configs(self):
        from indexer.embeddings import SentenceTransformerEmbeddingFunction
        with self.assertRaises(ValueError):
            SentenceTransformerEmbeddingFunction(model_name='untrusted/remote-code')
        with patch('indexer.embeddings._model') as model:
            model.return_value.encode.return_value = np.array([[1.,2.]])
            self.assertEqual(SentenceTransformerEmbeddingFunction()(input=['text'])[0].dtype, np.float32)
            model.return_value.encode.assert_called_once_with(['text'], convert_to_numpy=True, normalize_embeddings=False)


class VectorMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'export.json'
        self.destination = self.root/'storage'
        self.data = {'schema':1, 'collections':[{'name':'repo-original', 'metadata':{'repo_url':'fixture','commit_sha':'abc'},
            'configuration':{'hnsw':{'space':'cosine'},'embedding_function':None},
            'records':[{'id':'file:1','document':'source text','metadata':{'owner_id':'alice'},'embedding':[.1,.2,.3]}]}]}

    def save(self):
        raw = json.dumps(self.data).encode()
        self.source.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_exact_data_migration_retains_legacy_and_refuses_overwrite(self):
        checksum = self.save()
        self.destination.mkdir()
        original = self.destination/'chroma.sqlite3'
        original.write_bytes(b'unchanged legacy bytes')
        report = import_export(self.source, self.destination, checksum, service_stopped=True)
        self.assertEqual(report['vectors'], 1)
        self.assertEqual(report['embeddings_recomputed'], 0)
        self.assertEqual(original.read_bytes(), b'unchanged legacy bytes')
        self.assertEqual(verify_export(local_vector_client(self.destination), self.data)['vectors'], 1)
        with self.assertRaises(FileExistsError):
            import_export(self.source, self.destination, checksum, service_stopped=True)

    def test_bad_hash_bad_metric_and_malicious_embedding_do_not_publish(self):
        checksum = self.save()
        with self.assertRaises(ValueError):
            import_export(self.source, self.destination, '0'*64, service_stopped=True)
        for config in ({'hnsw':{'space':'unknown'}}, {'hnsw':{'space':'l2'},'embedding_function':{'config':{'model_name':'untrusted','trust_remote_code':True}}}):
            self.data['collections'][0]['configuration'] = config
            with self.assertRaises(ValueError):
                import_export(self.source, self.destination, self.save(), service_stopped=True)
            self.assertFalse((self.destination/'vectors.sqlite3').exists())

    def test_failed_late_record_rolls_back_whole_migration(self):
        self.data['collections'][0]['records'].append({'id':'file:2','document':'bad','metadata':{},'embedding':[1.,2.]})
        with self.assertRaises(ValueError):
            import_export(self.source, self.destination, self.save(), service_stopped=True)
        self.assertFalse((self.destination/'vectors.sqlite3').exists())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_unknown_fields_duplicates_and_missing_stop_refuse_import(self):
        with self.assertRaises(ValueError):
            import_export(self.source, self.destination, self.save())
        self.data['collections'][0]['records'].append(self.data['collections'][0]['records'][0])
        with self.assertRaises(ValueError):
            import_export(self.source, self.destination, self.save(), service_stopped=True)
        self.assertFalse((self.destination/'vectors.sqlite3').exists())
        raw = b'{"schema":1,"schema":1,"collections":[]}'
        self.source.write_bytes(raw)
        with self.assertRaises(ValueError):
            import_export(self.source, self.destination, hashlib.sha256(raw).hexdigest(), service_stopped=True)


if __name__ == '__main__':
    unittest.main()
