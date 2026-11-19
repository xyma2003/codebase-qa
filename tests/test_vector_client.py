import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from chromadb.errors import AuthorizationError

from indexer import vector_store
from memory import knowledge
from security.vector_client import _LocalSettings, local_chroma_client


class LocalVectorClientTests(unittest.TestCase):
    def test_environment_cannot_select_remote_clients_plugins_or_disable_limits(self):
        values = {"CHROMA_API_IMPL": "chromadb.api.fastapi.FastAPI", "CHROMA_SERVER_HOST": "example.invalid",
                  "CHROMA_SERVER_HTTP_PORT": "443", "CHROMA_CLIENT_AUTH_PROVIDER": "untrusted.Auth",
                  "CHROMA_SERVER_AUTHN_PROVIDER": "untrusted.ServerAuth", "CHROMA_SYSDB_IMPL": "untrusted.Database",
                  "CHROMA_PRODUCT_TELEMETRY_IMPL": "untrusted.Telemetry", "CHROMA_OTEL_COLLECTION_ENDPOINT": "https://example.invalid",
                  "ALLOW_RESET": "true", "ANONYMIZED_TELEMETRY": "true", "PERSIST_DIRECTORY": "/untrusted"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, values):
            client = local_chroma_client(directory)
            settings = client.get_settings()
            self.assertEqual(settings.chroma_api_impl, "chromadb.api.rust.RustBindingsAPI")
            self.assertEqual(settings.persist_directory, directory)
            self.assertIsNone(settings.chroma_server_host)
            self.assertIsNone(settings.chroma_client_auth_provider)
            self.assertIsNone(settings.chroma_server_authn_provider)
            self.assertEqual(settings.chroma_sysdb_impl, "chromadb.db.impl.sqlite.SqliteDB")
            self.assertEqual(settings.chroma_product_telemetry_impl, "chromadb.telemetry.product.posthog.Posthog")
            self.assertEqual(settings.chroma_otel_collection_endpoint, "")
            self.assertFalse(settings.allow_reset)
            self.assertFalse(settings.anonymized_telemetry)
            collection = client.create_collection("local-fixture", embedding_function=None)
            collection.add(ids=["source:1"], embeddings=[[1., 0.]], documents=["safe fixture"])
            self.assertEqual(collection.query(query_embeddings=[[1., 0.]], n_results=1)["ids"], [["source:1"]])
            with self.assertRaisesRegex(AuthorizationError, "Reset is disabled"):
                client.reset()

    def test_dotenv_and_secret_files_cannot_override_embedded_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text("CHROMA_API_IMPL=untrusted.Client\nALLOW_RESET=true\n")
            secrets = root / "secrets"
            secrets.mkdir()
            (secrets / "chroma_api_impl").write_text("untrusted.Client")
            settings = _LocalSettings(_env_file=env_file, _secrets_dir=secrets)
            self.assertEqual(settings.chroma_api_impl, "chromadb.api.rust.RustBindingsAPI")
            self.assertFalse(settings.allow_reset)

    def test_both_source_and_private_cache_clients_use_the_same_boundary(self):
        for module in (vector_store, knowledge):
            with self.subTest(module=module.__name__), patch.object(module, "CHROMA_DIR", "fixture-directory"), \
                    patch.object(module, "local_chroma_client", return_value="fixture-client") as factory:
                self.assertEqual(module._get_client(), "fixture-client")
                factory.assert_called_once_with("fixture-directory")

    def test_poisoned_settings_before_import_do_not_load_external_components(self):
        with tempfile.TemporaryDirectory() as directory:
            code = """from security.vector_client import local_chroma_client
import sys
client = local_chroma_client(sys.argv[1])
assert client.get_settings().chroma_api_impl == 'chromadb.api.rust.RustBindingsAPI'
assert not client.get_settings().anonymized_telemetry
assert not any(name.startswith('untrusted') for name in sys.modules)
print('EMBEDDED_ONLY')
"""
            env = dict(os.environ, CHROMA_API_IMPL="untrusted.Client", CHROMA_SYSDB_IMPL="untrusted.Database",
                       CHROMA_PRODUCT_TELEMETRY_IMPL="untrusted.Telemetry", ALLOW_RESET="true")
            result = subprocess.run([sys.executable, "-c", code, directory], cwd=Path(__file__).resolve().parents[1],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "EMBEDDED_ONLY")


if __name__ == "__main__":
    unittest.main()
