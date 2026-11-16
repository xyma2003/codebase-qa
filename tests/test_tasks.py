import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from agent import conversations, qa_agent
from agent.ui_runtime import run_background
from indexer.repository_service import RepositoryIndex
from security.identity import AccessDenied, as_principal, current_principal, load_access, verified_principal
from tasks.context import check_task, publication
from tasks.limits import TaskLimits, load_limits
from tasks.service import TaskService
from tasks.store import TaskRejected, LIVE
from tests import test_conversations as fixtures
from tests.test_user_access import ALICE, BOB, ADMIN, access_file, claims


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(conversations, "CHROMA_DIR", str(self.root / "conversations")))
        self.repository = RepositoryIndex(fixtures.URL, "a" * 40, "FIXTURE", fixtures.COLLECTION, 1, 1, "fixture", "fixture")
        self.enterContext(patch("indexer.repository_service.get_repository_version", return_value=self.repository))
        self.services = []
        self.addCleanup(self.close_services)

    def close_services(self):
        for service in self.services:
            service.close()

    def service(self, *, limits=None, handlers=None, start=False, root=None):
        result = TaskService(root or self.root / str(uuid4()), limits=limits, handlers=handlers, start=start)
        self.services.append(result)
        return result

    def payload(self, actor=ALICE, question="TASK_LOCAL_FIXTURE"):
        with as_principal(actor):
            session = conversations.create_conversation(fixtures.URL, self.repository.commit_sha, fixtures.COLLECTION)
        return dict(question=question, repo_url=fixtures.URL, commit_sha=self.repository.commit_sha,
                    collection_name=fixtures.COLLECTION, conversation_id=session)

    def submit(self, service, actor=ALICE, payload=None, *, kind="answer", submission=None):
        payload = payload or (self.payload(actor) if kind == "answer" else {"repo_url": fixtures.URL})
        with as_principal(actor):
            return service.submit(kind, payload, submission or str(uuid4()))

    def finish(self, service, task_id, actor=ALICE):
        with as_principal(actor):
            result = service.wait(task_id, 3)
        self.assertNotIn(result["state"], LIVE, result)
        return result

    def test_unknown_operations_extra_owner_and_paths_are_rejected_before_admission(self):
        service = self.service()
        payload = self.payload()
        for kind, values in (("shell", payload), ("answer", dict(payload, owner_id=BOB.owner_id)),
                             ("answer", dict(payload, repo_root="/tmp")),
                             ("import", {"repo_url": fixtures.URL, "refresh": "yes"}),
                             ("import", {"repo_url": "http://127.0.0.1/private"})):
            with self.subTest(kind=kind, values=values), as_principal(ALICE), self.assertRaises(ValueError):
                service.submit(kind, values, str(uuid4()))
        with as_principal(ALICE):
            self.assertEqual(service.list(), [])

    def test_unavailable_dispatcher_reports_failure_before_private_storage(self):
        service = self.service()
        task_id = self.submit(service)
        service._broken = True
        with as_principal(ALICE), patch.object(service.store, "read", side_effect=AssertionError("NO_READ")):
            with self.assertRaisesRegex(TaskRejected, "暂不可用"):
                service.get(task_id)
            with self.assertRaisesRegex(TaskRejected, "暂不可用"):
                service.list()

    def test_owner_checks_cover_results_lists_cancellation_and_admin(self):
        service = self.service(handlers={"answer": lambda p, event: {"text": "ALICE_PRIVATE_CANARY"}})
        task_id = self.submit(service)
        for actor in (BOB, ADMIN):
            with as_principal(actor):
                self.assertEqual(service.list(), [])
                with self.assertRaises(TaskRejected):
                    service.get(task_id)
                with self.assertRaises(TaskRejected):
                    service.cancel(task_id)
        service.step()
        self.assertEqual(self.finish(service, task_id)["result"]["text"], "ALICE_PRIVATE_CANARY")
        with as_principal(BOB), self.assertRaises(TaskRejected):
            service.get(task_id)

    def test_conversation_authorization_is_checked_before_queue_write(self):
        service = self.service()
        alice_payload = self.payload()
        with as_principal(BOB), self.assertRaisesRegex(ValueError, "不存在"):
            service.submit("answer", alice_payload, str(uuid4()))
        self.assertEqual(service.store.queued(), [])

    def test_idempotency_is_atomic_and_different_input_is_not_reused(self):
        service = self.service()
        payload, submission = self.payload(), str(uuid4())
        def send(_):
            return self.submit(service, payload=payload, submission=submission)
        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(send, range(8)))
        self.assertEqual(len(set(ids)), 1)
        with as_principal(ALICE):
            self.assertEqual(len(service.list()), 1)
            with self.assertRaisesRegex(TaskRejected, "不同任务"):
                service.submit("answer", dict(payload, question="CHANGED"), submission)

    def test_daily_quotas_include_failed_and_cancelled_tasks_and_roll_at_utc_midnight(self):
        service = self.service(limits=replace(TaskLimits(), daily_answers_per_user=1, daily_answers_total=2))
        with patch("tasks.store.time.time", return_value=1794787199):
            first = self.submit(service)
            with as_principal(ALICE):
                service.cancel(first)
            with self.assertRaisesRegex(TaskRejected, "今日"):
                self.submit(service)
            self.submit(service, BOB)
            with self.assertRaisesRegex(TaskRejected, "今日"):
                self.submit(service, ADMIN)
        with patch("tasks.store.time.time", return_value=1794787200):
            second = self.submit(service)
        self.assertNotEqual(first, second)

    def test_pending_limits_and_same_conversation_are_enforced(self):
        service = self.service(limits=replace(TaskLimits(), queued_total=2, queued_per_user=1))
        payload = self.payload()
        self.submit(service, payload=payload)
        with self.assertRaisesRegex(TaskRejected, "对话已有"):
            self.submit(service, payload=payload)
        with self.assertRaisesRegex(TaskRejected, "排队"):
            self.submit(service)
        self.submit(service, BOB)
        with self.assertRaisesRegex(TaskRejected, "排队"):
            self.submit(service, ADMIN)

    def test_concurrent_admission_does_not_overspend_daily_quota(self):
        service = self.service(limits=replace(TaskLimits(), daily_answers_per_user=1))
        payloads = [self.payload() for _ in range(5)]
        def send(payload):
            try:
                self.submit(service, payload=payload)
                return True
            except TaskRejected:
                return False
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(sum(pool.map(send, payloads)), 1)

    def test_global_kind_and_user_concurrency_and_queued_cancel(self):
        release = Event()
        self.addCleanup(release.set)
        started, lock = [], Lock()
        def handler(payload, progress):
            with lock:
                started.append(current_principal().owner_id)
            release.wait(3)
            check_task()
            return {"text": current_principal().owner_id}
        service = self.service(handlers={"answer": handler, "import": handler})
        first = self.submit(service)
        second = self.submit(service)
        third = self.submit(service, BOB)
        fourth = self.submit(service, ADMIN)
        service.step()
        with as_principal(ALICE):
            self.assertEqual(service.get(first)["state"], "running")
            self.assertEqual(service.get(second)["state"], "queued")
            self.assertEqual(service.cancel(second), "cancelled")
        with as_principal(BOB):
            self.assertEqual(service.get(third)["state"], "running")
        with as_principal(ADMIN):
            self.assertEqual(service.get(fourth)["state"], "queued")
        release.set()
        self.finish(service, first)
        self.finish(service, third, BOB)
        service.step()
        self.finish(service, fourth, ADMIN)
        self.assertEqual(set(started), {ALICE.owner_id, BOB.owner_id, ADMIN.owner_id})

    def test_running_import_remains_in_cancelling_until_sync_work_returns(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        def handler(payload, progress):
            entered.set()
            release.wait(3)
            with publication():
                self.fail("CANCELLED_IMPORT_MUST_NOT_PUBLISH")
        service = self.service(handlers={"import": handler}, limits=replace(TaskLimits(), running_imports=2))
        first = self.submit(service, kind="import")
        second = self.submit(service, BOB, kind="import")
        service.step()
        self.assertTrue(entered.wait(2))
        with as_principal(ALICE):
            self.assertEqual(service.cancel(first), "cancelling")
        service.step()
        with as_principal(BOB):
            self.assertEqual(service.get(second)["state"], "queued")
        release.set()
        self.assertEqual(self.finish(service, first)["state"], "cancelled")

    def test_import_deadline_does_not_free_slot_before_sync_operation_returns(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        def handler(payload, progress):
            entered.set()
            release.wait(3)
            check_task()
            return {}
        service = self.service(handlers={"import": handler}, limits=replace(TaskLimits(), import_timeout_seconds=1))
        first = self.submit(service, kind="import")
        second = self.submit(service, BOB, kind="import", payload={"repo_url": "https://github.com/example/second"})
        service.step()
        self.assertTrue(entered.wait(2))
        service._running[first]["started"] -= 2
        service.step()
        with as_principal(ALICE):
            self.assertEqual(service.get(first)["state"], "cancelling")
        with as_principal(BOB):
            self.assertEqual(service.get(second)["state"], "queued")
        release.set()
        result = self.finish(service, first)
        self.assertEqual(result["state"], "failed")
        self.assertIn("时限", result["error"])

    def test_background_async_cancellation_waits_for_cleanup_and_prevents_publication(self):
        entered, cleanup, release_cleanup = Event(), Event(), Event()
        self.addCleanup(release_cleanup.set)
        published = []
        async def model_work():
            try:
                entered.set()
                await asyncio.sleep(30)
                with publication():
                    published.append(True)
                return {"text": "late"}
            finally:
                cleanup.set()
                while not release_cleanup.is_set():
                    await asyncio.sleep(.01)
        service = self.service(handlers={"answer": lambda p, progress: run_background(model_work, check_task)})
        task_id = self.submit(service)
        service.step()
        self.assertTrue(entered.wait(2))
        with as_principal(ALICE):
            service.cancel(task_id)
            self.assertTrue(cleanup.wait(2))
            self.assertEqual(service.get(task_id)["state"], "cancelling")
        release_cleanup.set()
        self.assertEqual(self.finish(service, task_id)["state"], "cancelled")
        self.assertEqual(published, [])

    def test_queued_expired_and_revoked_identities_fail_before_handler(self):
        path = self.root / "access.toml"
        access_file(path)
        actor = verified_principal(claims(), load_access(path))
        calls = []
        service = self.service(handlers={"answer": lambda p, event: calls.append(True)})
        task_id = self.submit(service, actor)
        access_file(path, alice_role="admin")
        service.step()
        # Use a fresh valid identity for reading; worker retains the old role.
        self.assertEqual(self.finish(service, task_id)["state"], "failed")
        self.assertEqual(calls, [])
        actor = replace(ALICE, expires_at=time.time() + 60)
        second = self.submit(service, actor)
        with patch("security.identity.time.time", return_value=time.time() + 120):
            service.step()
            self.assertEqual(self.finish(service, second)["state"], "failed")
        self.assertEqual(calls, [])

    def test_midrun_revocation_stops_background_await_before_private_write(self):
        path = self.root / "access.toml"
        access_file(path)
        actor = verified_principal(claims(), load_access(path))
        entered = Event()
        async def model_work():
            entered.set()
            await asyncio.sleep(30)
            return {"text": "must not publish"}
        service = self.service(handlers={"answer": lambda p, event: run_background(model_work, check_task)})
        task_id = self.submit(service, actor)
        service.step()
        self.assertTrue(entered.wait(2))
        access_file(path, alice_role="admin")
        result = self.finish(service, task_id)
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["result"])

    def test_restart_interrupts_unfinished_jobs_and_never_replays_them(self):
        root = self.root / "restart"
        service = self.service(root=root)
        task_id = self.submit(service)
        second = self.submit(service, BOB)
        service.store.state(second, "running")
        service.close()
        self.services.remove(service)
        calls = []
        restarted = self.service(root=root, handlers={"answer": lambda p, event: calls.append(True)})
        self.assertEqual(self.finish(restarted, task_id)["state"], "interrupted")
        self.assertEqual(self.finish(restarted, second, BOB)["state"], "interrupted")
        restarted.step()
        self.assertEqual(calls, [])

    def test_second_process_lease_is_rejected_before_recovery(self):
        root = self.root / "lease"
        service = self.service(root=root)
        task_id = self.submit(service)
        with self.assertRaisesRegex(TaskRejected, "其他进程"):
            TaskService(root)
        code = """
import sys
from tasks.service import TaskService
from tasks.store import TaskRejected
try:
    TaskService(sys.argv[1])
except TaskRejected:
    print('SECOND_PROCESS_DENIED')
else:
    raise AssertionError('SECOND_PROCESS_MUST_NOT_OPEN_QUEUE')
"""
        child = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn("SECOND_PROCESS_DENIED", child.stdout)
        with as_principal(ALICE):
            self.assertEqual(service.get(task_id)["state"], "queued")

    def test_storage_records_and_result_limits_reject_without_retry(self):
        calls = []
        def handler(payload, progress):
            calls.append(True)
            return {"text": "x" * 100}
        service = self.service(limits=replace(TaskLimits(), max_records=1, result_bytes=32), handlers={"answer": handler})
        task_id = self.submit(service)
        service.step()
        result = self.finish(service, task_id)
        self.assertEqual(result["state"], "failed")
        self.assertIn("存储上限", result["error"])
        with self.assertRaisesRegex(TaskRejected, "记录空间"):
            self.submit(service)
        service.step()
        self.assertEqual(calls, [True])
        tiny = self.service(limits=replace(TaskLimits(), storage_bytes=32768, result_bytes=100))
        with self.assertRaisesRegex(TaskRejected, "记录空间"):
            self.submit(tiny)

    def test_completed_idempotent_submission_survives_conversation_revision_change(self):
        def handler(payload, progress):
            conversation = conversations.load_conversation(payload["conversation_id"], fixtures.URL, self.repository.commit_sha, fixtures.COLLECTION)
            from agent.evidence import CheckedAnswer
            from pydantic_ai.messages import ModelRequest, UserPromptPart
            conversations.append_turn(conversation, payload["question"],
                [ModelRequest(parts=[UserPromptPart(payload["question"])]), ModelResponse(parts=[TextPart("LOCAL")])],
                CheckedAnswer(text="LOCAL"), 0)
            return {"text": "LOCAL"}
        service = self.service(handlers={"answer": handler})
        payload, submission = self.payload(), str(uuid4())
        task_id = self.submit(service, payload=payload, submission=submission)
        service.step()
        self.assertEqual(self.finish(service, task_id)["state"], "succeeded")
        self.assertEqual(self.submit(service, payload=payload, submission=submission), task_id)

    def test_saved_results_are_not_cancelled_after_publication(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        def handler(payload, progress):
            with publication():
                pass
            entered.set()
            release.wait(3)
            return {"text": "saved"}
        service = self.service(handlers={"answer": handler})
        task_id = self.submit(service)
        service.step()
        self.assertTrue(entered.wait(2))
        with as_principal(ALICE), self.assertRaisesRegex(TaskRejected, "已保存"):
            service.cancel(task_id)
        release.set()
        self.assertEqual(self.finish(service, task_id)["state"], "succeeded")


class TaskSDKTests(unittest.TestCase):
    def test_real_sdk_model_and_native_history_work_through_durable_queue(self):
        helper = fixtures.ConversationTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        repository = RepositoryIndex(fixtures.URL, helper.sha, helper.snapshot.root, fixtures.COLLECTION, 1, 2, "fixture", "fixture")
        seen = []
        async def respond(messages, info):
            seen.append((current_principal().owner_id, len(messages)))
            return ModelResponse(parts=[TextPart("SDK_LOCAL_ONLY")])
        service = TaskService(helper.root / "tasks")
        self.addCleanup(service.close)
        with patch("indexer.repository_service.get_repository_version", return_value=repository), \
             patch.object(qa_agent, "_get_agent", return_value=Agent(FunctionModel(respond))):
            with as_principal(ALICE):
                session = conversations.create_conversation(fixtures.URL, helper.sha, fixtures.COLLECTION)
                payload = dict(question="first", repo_url=fixtures.URL, commit_sha=helper.sha, collection_name=fixtures.COLLECTION,
                               conversation_id=session, use_answer_cache=False)
                for question in ("first", "follow up"):
                    task_id = service.submit("answer", dict(payload, question=question), str(uuid4()))
                    result = service.wait(task_id, 5)
                    self.assertEqual(result["state"], "succeeded", result)
                    self.assertEqual(result["result"]["text"], "SDK_LOCAL_ONLY")
                history = conversations.load_conversation(session, fixtures.URL, helper.sha, fixtures.COLLECTION)
                self.assertEqual(history.revision, 2)
                self.assertEqual([turn.question for turn in history.turns], ["first", "follow up"])
        self.assertEqual([owner for owner, length in seen], [ALICE.owner_id, ALICE.owner_id])
        self.assertGreater(seen[1][1], seen[0][1])
        service.close()
        self.assertFalse(service._broken)

    def test_cancel_real_sdk_prevents_native_conversation_append(self):
        helper = fixtures.ConversationTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        repository = RepositoryIndex(fixtures.URL, helper.sha, helper.snapshot.root, fixtures.COLLECTION, 1, 2, "fixture", "fixture")
        entered, stopped = Event(), Event()
        async def respond(messages, info):
            try:
                entered.set()
                await asyncio.sleep(30)
                return ModelResponse(parts=[TextPart("MUST_NOT_SAVE")])
            finally:
                stopped.set()
        service = TaskService(helper.root / "tasks")
        self.addCleanup(service.close)
        with patch("indexer.repository_service.get_repository_version", return_value=repository), \
             patch.object(qa_agent, "_get_agent", return_value=Agent(FunctionModel(respond))):
            with as_principal(ALICE):
                session = conversations.create_conversation(fixtures.URL, helper.sha, fixtures.COLLECTION)
                task_id = service.submit("answer", dict(question="cancel", repo_url=fixtures.URL, commit_sha=helper.sha,
                    collection_name=fixtures.COLLECTION, conversation_id=session, use_answer_cache=False), str(uuid4()))
                self.assertTrue(entered.wait(3))
                service.cancel(task_id)
                result = service.wait(task_id, 3)
                self.assertEqual(result["state"], "cancelled", result)
                self.assertTrue(stopped.is_set())
                self.assertEqual(conversations.load_conversation(session, fixtures.URL, helper.sha, fixtures.COLLECTION).revision, 0)


class TaskImportTests(unittest.TestCase):
    def setUp(self):
        from tests.test_repository_versions import RepositoryVersionTests
        self.helper = RepositoryVersionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.service = TaskService(self.helper.root / "tasks")
        self.addCleanup(self.service.close)

    def test_actual_git_snapshot_and_index_publication_through_import_handler(self):
        from tests.test_repository_versions import URL
        from indexer.repository_service import get_current_repository
        with as_principal(ALICE):
            task_id = self.service.submit("import", {"repo_url": URL}, str(uuid4()))
            result = self.service.wait(task_id, 5)
            self.assertEqual(result["state"], "succeeded", result)
            self.assertEqual(result["result"]["commit_sha"], self.helper.first)
            self.assertEqual(get_current_repository(URL).commit_sha, self.helper.first)

    def test_cancel_between_index_build_and_publication_keeps_previous_ready_index(self):
        from tests.test_repository_versions import URL
        from indexer.repository_service import get_current_repository, import_repository
        old = import_repository(URL)
        self.helper.commit("def value():\n    return 2\n")
        built, release = Event(), Event()
        self.addCleanup(release.set)
        def build(*args):
            self.helper.build(*args)
            built.set()
            release.wait(3)
        with patch("indexer.repository_service.vector_store.build_collection", side_effect=build):
            with as_principal(ALICE):
                task_id = self.service.submit("import", {"repo_url": URL, "refresh": True}, str(uuid4()))
                self.assertTrue(built.wait(3))
                self.service.cancel(task_id)
                release.set()
                result = self.service.wait(task_id, 3)
                self.assertEqual(result["state"], "cancelled", result)
        self.assertEqual(get_current_repository(URL).collection_name, old.collection_name)
        self.assertEqual(set(self.helper.client.collections), {old.collection_name})


class TaskUITests(unittest.TestCase):
    def setUp(self):
        from tests.test_repository_ui import RepositoryUITests
        self.helper = RepositoryUITests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.app, self.service = self.helper.app, self.helper.tasks

    def test_delayed_answer_survives_page_rerun_without_duplicate_submission(self):
        from agent.evidence import CheckedAnswer
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        calls = []
        async def stream(*args, on_result=None, **kwargs):
            calls.append(args[0])
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            on_result(CheckedAnswer(text="BACKGROUND_LOCAL_FIXTURE"))
            yield "BACKGROUND_LOCAL_FIXTURE"
        with patch.object(qa_agent, "ask_stream", side_effect=stream):
            self.app.chat_input[0].set_value("long question").run(timeout=30)
            self.assertTrue(entered.is_set())
            task_id = self.app.session_state["active_task"]
            self.app.run(timeout=30)
            self.assertEqual(calls, ["long question"])
            self.assertTrue(any("执行中" in item.value for item in self.app.text))
            self.assertTrue(any(item.value == "long question" for item in self.app.text))
            release.set()
            self.assertEqual(self.service.wait(task_id, 3)["state"], "succeeded")
            self.app.run(timeout=30)
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.app.session_state["messages"][-1]["content"], "BACKGROUND_LOCAL_FIXTURE")
        self.assertEqual(calls, ["long question"])

    def test_cancel_button_stops_pending_model_and_keeps_previous_chat(self):
        from agent.evidence import CheckedAnswer
        stopped = Event()
        async def stream(*args, **kwargs):
            try:
                await asyncio.sleep(30)
                yield "SHOULD_NOT_APPEAR"
            finally:
                stopped.set()
        self.app.session_state["messages"] = [{"role": "assistant", "content": "OLD_LOCAL_ANSWER"}]
        with patch.object(qa_agent, "ask_stream", side_effect=stream):
            self.app.chat_input[0].set_value("cancel me").run(timeout=30)
            task_id = self.app.session_state["active_task"]
            self.app.button(key="task_cancel_" + task_id).click().run(timeout=30)
            self.assertEqual(self.service.wait(task_id, 3)["state"], "cancelled")
            self.app.run(timeout=30)
        self.assertTrue(stopped.is_set())
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.app.session_state["messages"], [{"role": "assistant", "content": "OLD_LOCAL_ANSWER"}])

    def test_result_can_be_reopened_after_session_state_is_lost(self):
        from agent.evidence import CheckedAnswer
        from streamlit.testing.v1 import AppTest
        from tests.test_repository_ui import ROOT
        async def stream(*args, on_result=None, **kwargs):
            on_result(CheckedAnswer(text="DURABLE_RESULT_LOCAL_FIXTURE"))
            yield "DURABLE_RESULT_LOCAL_FIXTURE"
        with patch.object(qa_agent, "ask_stream", side_effect=stream):
            self.app.chat_input[0].set_value("saved task").run(timeout=30)
        self.app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
        completed = next(task for task in self.service.list() if task["state"] == "succeeded")
        self.app.button(key="task_open_" + completed["id"]).click().run(timeout=30)
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.app.session_state["messages"][-1]["content"], "DURABLE_RESULT_LOCAL_FIXTURE")

    def test_expired_fragment_rejects_before_task_storage_even_without_page_gate(self):
        from streamlit.testing.v1 import AppTest
        from tests.test_user_access import WebAuthenticationTests
        auth = WebAuthenticationTests()
        auth.setUp()
        self.addCleanup(auth.doCleanups)
        auth.user.data = claims(exp=0)
        with patch.object(self.service, "list", side_effect=AssertionError("NO_PRIVATE_TASK_READ")) as listing:
            app = AppTest.from_string("from tasks.ui import task_panel\ntask_panel()").run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        listing.assert_not_called()
        self.assertTrue(app.error)


class TaskConfigTests(unittest.TestCase):
    def test_server_limits_validate_types_unknown_keys_and_symlinks(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "tasks.toml"
            path.write_text("running_answers = 1\ndaily_answers_per_user = 3\n")
            self.assertEqual(load_limits(path).running_answers, 1)
            for text in ("running_answers = true", "running_answers = 0", "owner = 'any'", "not toml"):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load_limits(path)
            link = Path(temp) / "link.toml"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                load_limits(link)
            with self.assertRaises(ValueError):
                load_limits(Path(temp) / "missing.toml")
