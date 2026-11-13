"""No IdP or model requests: exercise identity contracts and actual private stores."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from streamlit.testing.v1 import AppTest
import streamlit as st

from agent import conversations, qa_agent
from agent.evidence import CheckedAnswer
from agent.tools import RepoDeps
from evaluation import review_workspace as review
from memory import knowledge as cache
from security.identity import AccessDenied, Principal, as_principal, current_principal, load_access, user_id, verified_principal
from security import review_access
from tests import test_conversations as fixtures
from tests import test_security_cache as cache_fixtures

ROOT = Path(__file__).resolve().parents[1]
ISSUER = "https://identity.example.invalid"
ALICE = Principal(user_id(ISSUER,"alice"),"user")
BOB = Principal(user_id(ISSUER,"bob"),"user")
ADMIN = Principal(user_id(ISSUER,"admin"),"admin")


def access_file(path, *, alice_role="user"):
    path.write_text(f'issuer = "{ISSUER}"\n[[users]]\nsubject = "alice"\nrole = "{alice_role}"\n'
                    '[[users]]\nsubject = "bob"\nrole = "user"\n[[users]]\nsubject = "admin"\nrole = "admin"\n')


def claims(subject="alice", **updates):
    now = time.time()
    return dict({"is_logged_in":True,"iss":ISSUER,"sub":subject,"iat":now-10,"exp":now+3600},**updates)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/"access.toml"
        access_file(self.path)

    def test_verified_subject_and_server_role_ignore_claimed_email_and_role(self):
        policy = load_access(self.path)
        actor = verified_principal(claims(role="admin", email="admin@example.invalid"),policy)
        self.assertEqual((actor.owner_id,actor.role),(ALICE.owner_id,"user"))
        self.assertNotEqual(user_id(ISSUER,"alice"),user_id(ISSUER+"/other","alice"))
        self.assertEqual(actor.owner_id,verified_principal(claims(email="changed@example.invalid"),policy).owner_id)

    def test_missing_unknown_expired_future_and_wrong_issuer_are_denied(self):
        policy = load_access(self.path)
        for values in (dict(is_logged_in=False),dict(sub="unknown"),dict(iss=ISSUER+"/other"),dict(exp=0),
                       dict(iat=time.time()+1000),dict(nbf=time.time()+1000),dict(exp=True),dict(exp=float("nan")),dict(sub=None)):
            with self.subTest(values=values),self.assertRaises(AccessDenied):
                verified_principal(claims(**values),policy)
        for field in ("sub","exp","iat"):
            data = claims()
            data.pop(field)
            with self.assertRaises(AccessDenied):
                verified_principal(data,policy)

    def test_live_revocation_role_change_and_expiry_stop_existing_context(self):
        actor = verified_principal(claims(),load_access(self.path))
        with as_principal(actor):
            self.assertEqual(current_principal().owner_id,ALICE.owner_id)
            access_file(self.path,alice_role="admin")
            with self.assertRaises(AccessDenied):
                current_principal()
        with as_principal(replace(ALICE,expires_at=time.time()-1)),self.assertRaises(AccessDenied):
            current_principal()

    def test_missing_invalid_and_duplicate_access_configuration_fail_closed(self):
        for text in ('issuer="http://identity.example.invalid"\nusers=[]', 'not toml',
                     f'issuer="{ISSUER}"\n[[users]]\nsubject="a"\nrole="root"',
                     f'issuer="{ISSUER}"\n[[users]]\nsubject="a"\nrole="user"\n[[users]]\nsubject="a"\nrole="admin"'):
            self.path.write_text(text)
            with self.assertRaises(AccessDenied):
                load_access(self.path)
        self.path.unlink()
        with self.assertRaises(AccessDenied):
            load_access(self.path)

    def test_unbound_web_service_denies_before_private_storage(self):
        with as_principal(None),patch("streamlit.runtime.scriptrunner.get_script_run_ctx",return_value=object()), \
             patch.object(cache,"_get_client",side_effect=AssertionError("NO_STORAGE")), \
             patch.object(conversations,"_connect",side_effect=AssertionError("NO_STORAGE")):
            with self.assertRaises(AccessDenied):
                conversations.create_conversation(fixtures.URL,"a"*40,"fixture")
            with self.assertRaises(AccessDenied):
                cache.list_cache(fixtures.URL,"a"*40)

    def test_login_credentials_join_source_guards_and_log_redaction(self):
        from security import sources
        from security.output import redact_text
        from security.web_auth import _access_settings
        cookie = "COOKIE_LOCAL_CANARY_"+"A"*32
        client = "CLIENT_LOCAL_CANARY_"+"B"*32
        auth = {"redirect_uri":"https://app.example.invalid/oauth2callback","cookie_secret":cookie,
                "client_id":"LOCAL_TEST_NOT_A_SECRET","client_secret":client,
                "server_metadata_url":ISSUER+"/.well-known/openid-configuration"}
        with patch.object(sources,"_runtime_credentials",()),patch.object(st,"secrets",{"auth":auth}), \
             patch("security.web_auth.load_access",return_value=load_access(self.path)):
            _access_settings()
            for secret in (cookie,client):
                with self.assertRaises(sources.SourcePolicyError):
                    sources.ensure_safe_text(secret)
                self.assertNotIn(secret,redact_text("provider error: "+secret))


class ConversationOwnerTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ConversationTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def create(self,actor):
        with as_principal(actor):
            return conversations.create_conversation(fixtures.URL,self.helper.sha,fixtures.COLLECTION)

    def read(self,identity):
        return conversations.load_conversation(identity,fixtures.URL,self.helper.sha,fixtures.COLLECTION)

    def test_user_list_read_write_and_forged_state_are_owner_checked(self):
        alice,bob = self.create(ALICE),self.create(BOB)
        with as_principal(ALICE):
            state = self.read(alice)
            self.helper.append(state,question="ALICE_PRIVATE_QUESTION")
            self.assertEqual([row["conversation_id"] for row in conversations.list_conversations(fixtures.URL,self.helper.sha,fixtures.COLLECTION)],[alice])
            with self.assertRaises(ValueError):
                self.read(bob)
        with as_principal(BOB):
            with self.assertRaises(ValueError):
                self.read(alice)
            with self.assertRaises(AccessDenied):
                self.helper.append(state)
            forged = replace(state,owner_id=BOB.owner_id)
            with self.assertRaises(conversations.ConversationConflict):
                self.helper.append(forged)
            self.assertEqual(self.read(bob).revision,0)
        with as_principal(ADMIN),self.assertRaises(ValueError):
            self.read(alice)

    def test_old_sqlite_schema_is_preserved_for_local_operator_only(self):
        storage = self.helper.root/"legacy"
        storage.mkdir()
        identity = str(uuid4())
        with sqlite3.connect(storage/"conversations.sqlite3") as db:
            db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, repo_url TEXT,commit_sha TEXT,collection_name TEXT,revision INTEGER DEFAULT 0,reference_count INTEGER DEFAULT 0,format_version INTEGER DEFAULT 1)")
            db.execute("INSERT INTO conversations(id,repo_url,commit_sha,collection_name) VALUES(?,?,?,?)",(identity,fixtures.URL,self.helper.sha,fixtures.COLLECTION))
        with as_principal(ALICE),self.assertRaises(ValueError):
            conversations.load_conversation(identity,fixtures.URL,self.helper.sha,fixtures.COLLECTION,storage_dir=storage)
        state = conversations.load_conversation(identity,fixtures.URL,self.helper.sha,fixtures.COLLECTION,storage_dir=storage)
        self.assertEqual((state.owner_id,state.revision),("local",0))
        with sqlite3.connect(storage/"conversations.sqlite3") as db:
            self.assertEqual(db.execute("SELECT owner_id FROM conversations").fetchone(),("local",))

    def test_async_model_runs_do_not_mix_private_histories(self):
        identities = {actor.owner_id:self.create(actor) for actor in (ALICE,BOB)}
        async def task(actor):
            with as_principal(actor):
                async def respond(messages,info):
                    await asyncio.sleep(0)
                    self.assertEqual(current_principal().owner_id,actor.owner_id)
                    return ModelResponse(parts=[TextPart("PRIVATE_REPLY_"+actor.owner_id)])
                deps = RepoDeps(fixtures.URL,self.helper.snapshot.root,fixtures.COLLECTION,self.helper.sha,source_only=True)
                await qa_agent._run_answer("PRIVATE_QUESTION_"+actor.owner_id,deps,identities[actor.owner_id],agent=qa_agent.build_agent(FunctionModel(respond)))
        async def run():
            await asyncio.gather(task(ALICE),task(BOB))
        asyncio.run(run())
        for actor,other in ((ALICE,BOB),(BOB,ALICE)):
            with as_principal(actor):
                state = self.read(identities[actor.owner_id])
                self.assertEqual(state.revision,1)
                self.assertNotIn(other.owner_id,json.dumps([turn.question for turn in state.turns]))

    def test_revoked_identity_during_model_run_cannot_publish_a_turn(self):
        path = self.helper.root/"access.toml"
        access_file(path)
        actor = verified_principal(claims(),load_access(path))
        session = self.create(actor)
        def respond(messages,info):
            path.write_text(f'issuer="{ISSUER}"\n[[users]]\nsubject="bob"\nrole="user"')
            return ModelResponse(parts=[TextPart("SHOULD_NOT_BE_PUBLISHED")])
        with as_principal(actor),self.assertRaises(AccessDenied):
            deps = RepoDeps(fixtures.URL,self.helper.snapshot.root,fixtures.COLLECTION,self.helper.sha,source_only=True)
            asyncio.run(qa_agent._run_answer("local",deps,session,agent=qa_agent.build_agent(FunctionModel(respond))))
        with as_principal(ALICE):
            self.assertEqual(self.read(session).revision,0)


class CacheOwnerTests(unittest.TestCase):
    def setUp(self):
        self.helper = cache_fixtures.CachePolicyTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def test_private_candidates_approval_retrieval_and_revocation(self):
        with as_principal(ALICE):
            identity = self.helper.save()
            row = self.helper.rows()[0]
            self.assertEqual(row["metadata"]["owner_id"],ALICE.owner_id)
            cache.review_qa(self.helper.url,self.helper.sha,self.helper.snapshot.root,identity,row["review_token"],approve=True)
            self.assertIsNotNone(self.helper.search())
        with as_principal(BOB):
            self.assertEqual(self.helper.rows(),[])
            self.assertIsNone(self.helper.search())
            self.assertEqual(cache.get_memory_stats(self.helper.url,commit_sha=self.helper.sha)["total_count"],0)
            for approve in (True,False):
                with self.assertRaises(ValueError):
                    cache.review_qa(self.helper.url,self.helper.sha,self.helper.snapshot.root,identity,row["review_token"],approve=approve)
            self.helper.save()
        with as_principal(ADMIN):
            self.assertEqual(self.helper.rows(),[])
        with as_principal(ALICE):
            row = self.helper.rows()[0]
            cache.review_qa(self.helper.url,self.helper.sha,self.helper.snapshot.root,identity,row["review_token"],approve=False)
            self.assertIsNone(self.helper.search())

    def test_legacy_local_cache_is_not_implicitly_shared(self):
        identity = self.helper.save()
        with as_principal(ALICE):
            self.assertEqual(self.helper.rows(),[])
            self.assertIsNone(self.helper.search())
        self.assertEqual(self.helper.rows()[0]["id"],identity)

    def test_replayed_foreign_metadata_is_hidden_and_not_reviewable(self):
        with as_principal(BOB):
            identity = self.helper.save()
            foreign = self.helper.rows()[0]
        with as_principal(ALICE):
            collection = cache._collection(self.helper.url,self.helper.sha,create=True)
            collection.add(ids=[identity],documents=[foreign["document"]],metadatas=[foreign["metadata"]])
            self.assertEqual(self.helper.rows(),[])
            self.assertIsNone(self.helper.search())
            for approve in (True,False):
                with self.assertRaises(AccessDenied):
                    cache.review_qa(self.helper.url,self.helper.sha,self.helper.snapshot.root,identity,foreign["review_token"],approve=approve)


class ReviewOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TemporaryDirectory()
        cls.archive = Path(cls.source.name)/"archive"
        with zipfile.ZipFile(ROOT/"docs/checks/20261111-evaluation-runner-local.zip") as file:
            file.extractall(cls.archive)

    @classmethod
    def tearDownClass(cls):
        cls.source.cleanup()

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enterContext(patch.object(review_access,"STATE_ROOT",self.root/"managed"))

    def test_nonadmin_service_calls_fail_before_reading_or_writing_paths(self):
        fake = SimpleNamespace(archive=self.archive,packet=self.root/"other")
        calls = [lambda:review.new_workspace(self.archive,ROOT,self.root),lambda:review.open_workspace(self.archive,ROOT,fake.packet),
                 lambda:review.save_grade(fake,"case","turn"),lambda:review.create_report(fake,self.root),
                 lambda:review.report_package(self.root,self.archive,ROOT),lambda:review_access.list_review_runs()]
        with as_principal(ALICE),patch.object(review,"_editable_packet",side_effect=AssertionError("NO_FILE_READ")):
            for call in calls:
                with self.assertRaises(AccessDenied):
                    call()

    def test_admin_reviews_and_downloads_stay_inside_owned_registered_runs(self):
        alice_admin = replace(ALICE,role="admin")
        first = review_access.register_review_run(self.archive,alice_admin.owner_id,"Alice run")
        second = review_access.register_review_run(self.archive,ADMIN.owner_id,"Other run")
        with as_principal(alice_admin):
            self.assertEqual([row["id"] for row in review_access.list_review_runs()],[first])
            directory = review_access.review_directory()
            workspace = review.new_workspace(self.archive,ROOT,directory,purpose="simulation")
            row = workspace.rows[0]
            saved = review.save_grade(workspace,row.case_id,row.turn_id,ratings=dict(correctness=2,evidence=2,completeness=2,boundaries=2),reviewer="local fixture",rationale="Synthetic permission check")
            report,_ = review.create_report(saved,directory)
            self.assertTrue(review.report_package(report,self.archive,ROOT))
            with self.assertRaises(AccessDenied):
                review_access.resolve_review_run(second)
        with as_principal(ADMIN):
            with self.assertRaises(AccessDenied):
                review.open_workspace(self.archive,ROOT,workspace.packet)
            with self.assertRaises(AccessDenied):
                review.report_package(report,self.archive,ROOT)
            with self.assertRaises(AccessDenied):
                review.new_workspace(self.archive,ROOT,directory)


class FakeUser:
    def __init__(self,data):
        self.data = data
    def to_dict(self):
        return dict(self.data)


class WebAuthenticationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/"access.toml"
        access_file(self.path)
        self.user = FakeUser(claims())
        self.enterContext(patch.dict(os.environ,{"CODEBASE_QA_LOCAL_MODE":"0","CODEBASE_QA_ACCESS_FILE":str(self.path)}))
        self.enterContext(patch.object(st,"user",self.user))
        auth = {"redirect_uri":"https://app.example.invalid/oauth2callback","cookie_secret":"x"*48,
                "client_id":"LOCAL_TEST_NOT_A_SECRET","client_secret":"LOCAL_TEST_NOT_A_SECRET",
                "server_metadata_url":ISSUER+"/.well-known/openid-configuration","expose_tokens":[]}
        self.enterContext(patch.object(st,"secrets",{"auth":auth}))

    def app(self,path="app.py"):
        app = AppTest.from_file(str(ROOT/path)).run(timeout=30)
        self.assertEqual(len(app.exception),0)
        return app

    def test_missing_configuration_denies_every_page_before_private_data(self):
        self.path.unlink()
        with patch("indexer.repository_service.get_current_repository",side_effect=AssertionError("NO_DATA")):
            for page in ("app.py","pages/1_评测评审.py","pages/2_历史答案审核.py"):
                app = self.app(page)
                self.assertTrue(app.error)
                self.assertEqual(len(app.text_input),0)

    def test_anonymous_login_uses_native_oidc_and_never_opens_business_pages(self):
        self.user.data = {"is_logged_in":False}
        app = self.app()
        self.assertEqual([button.label for button in app.button],["登录"])
        with patch.object(st,"login") as login:
            app.button[0].click().run(timeout=30)
            login.assert_called_once_with()
        self.assertEqual(len(app.text_input),0)

    def test_unknown_and_expired_users_cannot_enter_even_with_claimed_admin_role(self):
        for data in (claims("unknown",role="admin"),claims(exp=0)):
            self.user.data = data
            app = self.app()
            self.assertTrue(app.error)
            self.assertEqual(len(app.text_input),0)

    def test_nonadmin_direct_management_url_is_denied_before_catalog_reads(self):
        self.user.data = claims(role="admin")
        with patch.object(review_access,"_catalog",side_effect=AssertionError("NO_CATALOG")):
            app = self.app("pages/1_评测评审.py")
        self.assertIn("没有管理权限",app.error[0].value)

    def test_account_switch_and_logout_clear_private_session_state(self):
        script = "import streamlit as st\nfrom security.web_auth import require_web_user\np=require_web_user()\nst.text(p.owner_id)"
        app = AppTest.from_string(script).run(timeout=30)
        app.session_state["messages"] = ["ALICE_PRIVATE_CANARY"]
        app.session_state["review_active"] = ("private-path","private-packet","private-version")
        self.user.data = claims("bob")
        app.run(timeout=30)
        self.assertNotIn("messages",app.session_state)
        self.assertNotIn("review_active",app.session_state)
        self.assertEqual(app.text[0].value,BOB.owner_id)
        app.session_state["messages"] = ["BOB_PRIVATE_CANARY"]
        with patch.object(st,"logout",side_effect=lambda:self.user.data.update(is_logged_in=False)) as logout:
            next(button for button in app.button if button.label=="退出登录").click().run(timeout=30)
            logout.assert_called_once()
        self.assertNotIn("messages",app.session_state)
        self.assertEqual([button.label for button in app.button],["登录"])

    def test_callback_rechecks_expiry_before_cache_approval(self):
        from indexer.repository_service import RepositoryIndex
        from memory import review_ui
        helper = cache_fixtures.CachePolicyTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with as_principal(ALICE):
            helper.save()
        app = AppTest.from_string("from memory.review_ui import render_cache_review\nrender_cache_review()").run(timeout=30)
        app.session_state["repository_index"] = RepositoryIndex(helper.url,helper.sha,helper.snapshot.root,"fixture",1,1,"fixture","fixture")
        app.run(timeout=30)
        self.assertEqual(len(app.exception),0)
        approve = next(button for button in app.button if button.label=="批准使用")
        self.user.data = claims(exp=0)
        with patch.object(review_ui,"review_qa") as write:
            approve.click().run(timeout=30)
            write.assert_not_called()
        self.assertTrue(app.error)
        with as_principal(ALICE):
            self.assertEqual(helper.rows()[0]["metadata"]["approval_status"],"pending")

    def test_web_admin_selects_registered_runs_without_server_path_inputs(self):
        self.user.data = claims("admin")
        root = self.path.parent/"managed"
        with patch.object(review_access,"STATE_ROOT",root):
            # Only the explicit local operator can register the server-side path.
            with as_principal(None):
                temp = TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                archive = Path(temp.name)/"archive"
                with zipfile.ZipFile(ROOT/"docs/checks/20261111-evaluation-runner-local.zip") as file:
                    file.extractall(archive)
                identity = review_access.register_review_run(archive,ADMIN.owner_id,"Owned fixture")
            app = self.app("pages/1_评测评审.py")
            self.assertEqual(len(app.text_input),0)
            next(item for item in app.selectbox if item.label=="运行记录").select(identity)
            next(button for button in app.button if button.label=="打开评审").click().run(timeout=60)
            self.assertEqual(len(app.exception),0)
            self.assertTrue(any(item.label=="评分理由" for item in app.text_area))
            self.assertTrue(Path(app.session_state["review_active"][1]).resolve().is_relative_to((root/"users"/ADMIN.owner_id).resolve()))

    def test_full_chat_page_only_restores_the_logged_in_users_history(self):
        from indexer.repository_service import RepositoryIndex
        helper = fixtures.ConversationTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.enterContext(patch.dict(os.environ,{"CODEBASE_QA_LOCAL_MODE":"0"}))
        index = RepositoryIndex(fixtures.URL,helper.sha,helper.snapshot.root,fixtures.COLLECTION,1,2,"fixture","fixture")
        sessions = {}
        for actor,label in ((ALICE,"ALICE_PRIVATE"),(BOB,"BOB_PRIVATE")):
            with as_principal(actor):
                identity = conversations.create_conversation(fixtures.URL,helper.sha,fixtures.COLLECTION)
                state = conversations.load_conversation(identity,fixtures.URL,helper.sha,fixtures.COLLECTION)
                conversations.append_turn(state,label,helper.messages(label),CheckedAnswer(text="local fixture"),0)
                sessions[actor.owner_id] = identity
        with patch("indexer.repository_service.get_current_repository",return_value=index), \
             patch("indexer.repository_service.list_repository_versions",return_value=[index]), \
             patch("indexer.repository_service.get_repository_version",return_value=index), \
             patch("memory.knowledge.get_memory_stats",return_value={"count":0,"pending_count":0}):
            app = self.app()
            app.text_input[0].set_value(fixtures.URL).run(timeout=30)
            history = next(item for item in app.selectbox if item.label=="历史对话")
            self.assertNotIn("BOB_PRIVATE",str(history.options))
            history.select(sessions[ALICE.owner_id]).run(timeout=30)
            self.assertIn("ALICE_PRIVATE"," ".join(item.value for item in app.text))
            self.user.data = claims("bob")
            app.run(timeout=30)
            self.assertNotIn("ALICE_PRIVATE"," ".join(item.value for item in app.text))
            app.text_input[0].set_value(fixtures.URL).run(timeout=30)
            history = next(item for item in app.selectbox if item.label=="历史对话")
            self.assertNotIn("ALICE_PRIVATE",str(history.options))
            history.select(sessions[BOB.owner_id]).run(timeout=30)
            self.assertIn("BOB_PRIVATE"," ".join(item.value for item in app.text))
            self.assertEqual(len(app.exception),0)

    def test_local_mode_refuses_a_nonloopback_listener(self):
        with patch.dict(os.environ,{"CODEBASE_QA_LOCAL_MODE":"1"}),patch.object(st,"get_option",return_value="0.0.0.0"):
            app = AppTest.from_string("from security.web_auth import require_web_user\nrequire_web_user()").run(timeout=30)
        self.assertEqual(len(app.exception),0)
        self.assertIn("只允许监听本机",app.error[0].value)
