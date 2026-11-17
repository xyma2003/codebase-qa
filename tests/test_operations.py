from datetime import datetime, timedelta, timezone
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

import yaml
from ops import backup as state_backup
from ops.dependency_audit import audit, groups, pins, release_gate
from ops.preflight import preflight
from ops.service_control import begin_drain, counts, resume, wait_idle
from tasks.store import TaskRejected
from tests import test_tasks as task_fixtures

ROOT = Path(__file__).resolve().parents[1]


class AuditFixture:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lock = self.root / "requirements.lock"
        self.lock.write_text("Sample_Package==1.2.3 \\\n    --hash=sha256:abc\n")

    def check(self, transport):
        return audit(self.lock, self.root / str(uuid4()), transport=transport)


class AuditTests(AuditFixture, unittest.TestCase):

    def test_exact_pins_canonical_names_and_invalid_inputs(self):
        self.assertEqual(pins(self.lock), {"sample-package": "1.2.3"})
        for value in ("sample>=1", "sample==1\nsample==2", "", "https://example.invalid/evil.whl"):
            self.lock.write_text(value)
            with self.assertRaises(ValueError):
                pins(self.lock)

    def test_full_clean_audit_can_pass_and_altered_summary_is_rejected(self):
        report = self.check(lambda endpoint, payload: {"results": [{}]})
        self.assertTrue(report["complete"])
        release_gate(report, self.lock)
        tampered = dict(report, requests=[])
        with self.assertRaisesRegex(ValueError, "不完整"):
            release_gate(tampered, self.lock)

    def test_network_failure_and_missing_coverage_are_not_clean(self):
        def failure(*args):
            raise TimeoutError("LOCAL_TIMEOUT")
        for transport in (failure, lambda *args: {"results": []}, lambda *args: {"results": [{"error": "bad"}]}):
            result = self.check(transport)
            self.assertFalse(result["complete"])
            self.assertTrue(result["error"])
            with self.assertRaises(ValueError):
                release_gate(result, self.lock)

    def test_pagination_aliases_and_known_vulnerabilities_block_release(self):
        calls = []
        def transport(endpoint, payload=None):
            calls.append(endpoint)
            if endpoint == "/querybatch":
                if "page_token" not in payload["queries"][0]:
                    return {"results": [{"vulns": [{"id": "GHSA-fixture"}], "next_page_token": "next"}]}
                return {"results": [{"vulns": [{"id": "PYSEC-fixture"}]}]}
            identity = endpoint.split("/")[-1]
            return {"id": identity, "aliases": ["CVE-LOCAL-FIXTURE"]}
        report = self.check(transport)
        self.assertEqual(len(report["findings"]), 2)
        self.assertEqual(len(groups(report["findings"])), 1)
        self.assertEqual(calls.count("/querybatch"), 2)
        with self.assertRaisesRegex(ValueError, "禁止上线"):
            release_gate(report, self.lock)
        with self.assertRaisesRegex(ValueError, "摘要被修改"):
            release_gate(dict(report, findings=[]), self.lock)

    def test_audit_expiry_future_time_and_lock_change_are_rejected(self):
        report = self.check(lambda *args: {"results": [{}]})
        for age in (8, -1):
            invalid = dict(report, queried_at=(datetime.now(timezone.utc) - timedelta(days=age)).isoformat())
            with self.assertRaisesRegex(ValueError, "过期"):
                release_gate(invalid, self.lock)
        self.lock.write_text("sample-package==2.0\n")
        with self.assertRaises(ValueError):
            release_gate(report, self.lock)


class BackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "source"
        (self.root / "chroma_db").mkdir(parents=True)
        db = sqlite3.connect(self.root / "chroma_db/conversations.sqlite3")
        db.execute("CREATE TABLE messages(owner TEXT, text TEXT)")
        db.execute("INSERT INTO messages VALUES('alice','PRIVATE_LOCAL_FIXTURE')")
        db.commit()
        db.close()
        (self.root / ".env").write_text("MUST_NOT_BACK_UP")
        (self.root / ".baseline_runtime/huggingface").mkdir(parents=True)
        (self.root / ".baseline_runtime/huggingface/weights").write_text("EXCLUDED")
        self.archive = Path(temp.name) / "backup.zip"
        self.destination = Path(temp.name) / "restored"

    def save(self):
        return state_backup.backup(self.root, self.archive, service_stopped=True)

    def test_stopped_backup_restores_exact_bytes_and_excludes_secrets_cache(self):
        result = self.save()
        self.assertGreater(result["files"], 0)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertFalse(any(".env" in name or "huggingface" in name for name in archive.namelist()))
        state_backup.restore(self.archive, self.destination, runtime_root=self.root)
        self.assertEqual((self.root / "chroma_db/conversations.sqlite3").read_bytes(),
                         (self.destination / "chroma_db/conversations.sqlite3").read_bytes())
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            state_backup.restore(self.archive, self.destination, runtime_root=self.root)

    def test_backup_requires_stop_and_refuses_running_worker(self):
        with self.assertRaisesRegex(ValueError, "停止服务"):
            state_backup.backup(self.root, self.archive)
        directory = self.root / ".baseline_runtime/tasks"
        directory.mkdir()
        with (directory / "worker.lock").open("w") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError, "仍在运行"):
                self.save()

    def test_symlinks_corrupt_sqlite_and_external_registered_archive_refuse_backup(self):
        source = self.root / "chroma_db/bad"
        source.symlink_to(self.root / ".env")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.save()
        source.unlink()
        (self.root / "chroma_db/bad.sqlite3").write_text("CORRUPT_FIXTURE")
        with self.assertRaises(sqlite3.DatabaseError):
            self.save()
        (self.root / "chroma_db/bad.sqlite3").unlink()
        directory = self.root / ".baseline_runtime/reviews"
        directory.mkdir()
        (directory / "catalog.json").write_text(json.dumps({"format_version": 1, "runs": [{"archive": "/outside/run"}]}))
        with self.assertRaisesRegex(ValueError, "不在备份范围"):
            self.save()

    def modified_archive(self, name, contents, **updates):
        raw = {"format": 1, "runtime_root": str(self.root), "files": {name: {"bytes": len(contents), "sha256": hashlib.sha256(contents).hexdigest(), **updates}}}
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr(name, contents)
            archive.writestr("backup.json", json.dumps(raw))

    def test_traversal_tampering_unknown_roots_and_wrong_runtime_are_rejected(self):
        for name in ("../escape", "/escape", "other/file", "repos/../escape", "repos/back\\slash"):
            self.modified_archive(name, b"LOCAL")
            with self.assertRaises(ValueError):
                state_backup.restore(self.archive, self.destination, runtime_root=self.root)
            self.assertFalse(self.destination.exists())
        self.modified_archive("repos/a.py", b"LOCAL", sha256="a" * 64)
        with self.assertRaisesRegex(ValueError, "摘要"):
            state_backup.restore(self.archive, self.destination, runtime_root=self.root)
        with self.assertRaisesRegex(ValueError, "根目录"):
            state_backup.restore(self.archive, self.destination, runtime_root="/different")


class DrainTests(unittest.TestCase):
    def setUp(self):
        self.helper = task_fixtures.TaskTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def test_drain_blocks_new_admission_but_keeps_private_status_and_resume(self):
        from security.identity import as_principal
        from tests.test_user_access import ALICE
        service = self.helper.service()
        first = self.helper.submit(service)
        begin_drain(service.store.root)
        with self.assertRaisesRegex(TaskRejected, "维护中"):
            self.helper.submit(service)
        with as_principal(ALICE):
            self.assertEqual(service.get(first)["state"], "queued")
            service.cancel(first)
        self.assertEqual(wait_idle(service.store.root, 0)["queued"], 0)
        resume(service.store.root)
        self.helper.submit(service)

    def test_drain_timeout_preserves_stop_marker(self):
        service = self.helper.service()
        self.helper.submit(service)
        with self.assertRaises(TimeoutError):
            wait_idle(service.store.root, 0)
        self.assertTrue(counts(service.store.root)["draining"])


class DeploymentTests(AuditFixture, unittest.TestCase):
    def test_container_command_failure_preserves_original_stderr(self):
        import contextlib
        import io
        import subprocess
        from scripts import check_container
        output = io.StringIO()
        error = subprocess.CalledProcessError(1, ["nginx", "-t"], output="LOCAL_STDOUT", stderr="NGINX_ORIGINAL_LOCAL_ERROR")
        with patch.object(check_container.subprocess, "run", side_effect=error), contextlib.redirect_stderr(output):
            with self.assertRaises(subprocess.CalledProcessError):
                check_container.run("nginx", "-t")
        self.assertIn("NGINX_ORIGINAL_LOCAL_ERROR", output.getvalue())

    def test_system_audit_rejects_findings_and_mismatched_image(self):
        from ops.os_audit import validate_os_audit
        image = "sha256:" + "b" * 64
        report = {"complete": True, "image_id": image, "revision": "a" * 40,
            "queried_at": datetime.now(timezone.utc).isoformat(), "lock_sha256": hashlib.sha256(self.lock.read_bytes()).hexdigest(),
            "raw": {"Metadata": {"ImageID": image}, "Results": [{"Class": "os-pkgs"}]}}
        validate_os_audit(report, self.lock, "a" * 40, image)
        with self.assertRaises(ValueError):
            validate_os_audit(report, self.lock, "a" * 40, "sha256:" + "c" * 64)
        report["raw"]["Results"][0]["Vulnerabilities"] = [{"VulnerabilityID": "CVE-LOCAL-FIXTURE"}]
        with self.assertRaisesRegex(ValueError, "禁止上线"):
            validate_os_audit(report, self.lock, "a" * 40, image)

    def test_proxy_renderer_rejects_injected_hostname(self):
        from scripts.render_proxy import render
        self.assertIn("server_name qa.example.org;", render("qa.example.org"))
        for value in ("qa.example.org; include /etc/secret", "*", "localhost", "https://qa.example.org"):
            with self.assertRaises(ValueError):
                render(value)

    def test_compose_privilege_network_and_secret_boundaries(self):
        data = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text())
        app, proxy = data["services"]["app"], data["services"]["proxy"]
        self.assertNotIn("ports", app)
        self.assertEqual(proxy["ports"], ["127.0.0.1:18443:8443"])
        for service in (app, proxy):
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertNotEqual(service["user"].split(":")[0], "0")
        self.assertEqual(app["environment"]["CODEBASE_QA_LOCAL_MODE"], "0")
        self.assertTrue(any("/app/.env:ro" in path for path in app["volumes"]))
        self.assertFalse(any("API_KEY" in key for key in app["environment"]))

    def test_preflight_checks_configuration_without_model_or_oidc_calls(self):
        from ops.backup import ROOTS
        self.enterContext(patch.dict(os.environ, {"CODEBASE_QA_LOCAL_MODE": "0"}))
        report = self.check(lambda *args: {"results": [{}]})
        audit_path = self.root / "audit.json"
        audit_path.write_text(json.dumps(report))
        config = self.root / ".streamlit"
        config.mkdir()
        access = config / "access.toml"
        access.write_text('issuer="https://identity.example.org"\n[[users]]\nsubject="LOCAL_FIXTURE"\nrole="user"\n')
        secrets = config / "secrets.toml"
        secrets.write_text('[auth]\nredirect_uri="https://qa.example.org/oauth2callback"\nclient_id="LOCAL_CLIENT"\nclient_secret="LOCAL_SECRET_CANARY"\ncookie_secret="' + 'X' * 48 + '"\nserver_metadata_url="https://identity.example.org/.well-known/openid-configuration"\n')
        model = self.root / ".env"
        model.write_text("OPENAI_API_KEY=LOCAL_MODEL_CANARY\n")
        for file in (access, secrets, model):
            file.chmod(0o600)
        for name in ROOTS:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        deploy = self.root / "deploy"
        deploy.mkdir()
        (deploy / "revision").write_text("a" * 40)
        image_id = "sha256:" + "b" * 64
        self.enterContext(patch.dict(os.environ, {"CODEBASE_QA_IMAGE_ID": image_id}))
        os_report = {"complete": True, "revision": "a" * 40, "image_id": image_id,
            "lock_sha256": hashlib.sha256(self.lock.read_bytes()).hexdigest(), "queried_at": datetime.now(timezone.utc).isoformat(),
            "raw": {"Metadata": {"ImageID": image_id}, "Results": [{"Class": "os-pkgs"}]}}
        (self.root / "os-audit.json").write_text(json.dumps(os_report))
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "fixture-weights").write_bytes(b"LOCAL_MODEL_CACHE_FIXTURE")
        manifest = {"model": "sentence-transformers/all-MiniLM-L6-v2", "files": {"fixture-weights": hashlib.sha256((cache / "fixture-weights").read_bytes()).hexdigest()}}
        (deploy / "embedding-cache.json").write_text(json.dumps(manifest))
        self.enterContext(patch.dict(os.environ, {"HF_HOME": str(cache)}))
        result = preflight(self.root, audit_path, self.lock, "https://qa.example.org")
        self.assertFalse(result["model_called"])
        self.assertFalse(result["oidc_callback_tested"])
        model.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "当前用户"):
            preflight(self.root, audit_path, self.lock, "https://qa.example.org")

    def test_cached_embedding_hashes_and_paths_are_checked_without_model_loading(self):
        from ops.preflight import verify_embeddings
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "weights").write_bytes(b"FIXTURE")
        manifest = self.root / "model.json"
        data = {"model": "sentence-transformers/all-MiniLM-L6-v2", "files": {"weights": hashlib.sha256(b"FIXTURE").hexdigest()}}
        manifest.write_text(json.dumps(data))
        verify_embeddings(cache, manifest)
        from scripts.seed_embeddings import seed
        (cache / "token").write_text("MUST_NOT_COPY_TOKEN")
        destination = self.root / "seeded-cache"
        self.assertEqual(seed(cache, destination, manifest)["files"], 1)
        self.assertFalse((destination / "token").exists())
        (cache / "weights").write_bytes(b"TAMPERED")
        with self.assertRaisesRegex(ValueError, "摘要"):
            verify_embeddings(cache, manifest)
        data["files"] = {"../requirements.lock": "a" * 64}
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "越界"):
            verify_embeddings(cache, manifest)
