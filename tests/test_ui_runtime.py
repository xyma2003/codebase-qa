import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, get_ident
import unittest

from agent.ui_runtime import run_ui_stream
from security.identity import Principal, as_principal, current_principal, user_id

ROOT = Path(__file__).resolve().parents[1]


class UIRuntimeTests(unittest.TestCase):
    def test_loop_is_stable_across_calls_without_reusing_user_identity(self):
        loops = []
        async def stream(observe):
            loops.append(id(asyncio.get_running_loop()))
            yield current_principal().owner_id
        for subject in ("first","second"):
            actor = Principal(user_id("https://identity.example.invalid",subject),"user")
            with as_principal(actor):
                self.assertEqual(run_ui_stream(stream),actor.owner_id)
        self.assertEqual(loops[0],loops[1])

    def test_concurrent_users_keep_identity_and_events_on_their_caller_thread(self):
        def call(subject):
            actor = Principal(user_id("https://identity.example.invalid",subject),"user")
            caller = get_ident()
            observed = []
            async def stream(observe):
                self.assertNotEqual(get_ident(),caller)
                await asyncio.sleep(.01)
                observe(current_principal().owner_id)
                yield current_principal().owner_id
            def render(event):
                self.assertEqual(get_ident(),caller)
                observed.append(event)
            with as_principal(actor):
                output = run_ui_stream(stream,on_event=render)
            self.assertEqual(observed,[actor.owner_id])
            self.assertEqual(output,actor.owner_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(call,["alice","bob"]))

    def test_interrupted_ui_cancels_its_pending_model_task(self):
        cancelled = Event()
        async def stream(observe):
            try:
                observe("started")
                await asyncio.sleep(30)
                yield "must not finish"
            finally:
                cancelled.set()
        def stop(event):
            raise RuntimeError("UI_STOP_FIXTURE")
        with self.assertRaisesRegex(RuntimeError,"UI_STOP_FIXTURE"):
            run_ui_stream(stream,on_event=stop)
        self.assertTrue(cancelled.wait(2))

    def test_subprocess_exits_after_real_sdk_threads_and_app_import(self):
        # Reproduce the shutdown boundary: importing the app used to replace
        # asyncio.run globally, stranding an idle AnyIO worker on interpreter exit.
        code = '''
import asyncio, os
from streamlit.testing.v1 import AppTest
os.environ['CODEBASE_QA_LOCAL_MODE']='1'
AppTest.from_file('app.py').run(timeout=30)
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart
def model(messages, info):
    return ModelResponse(parts=[TextPart('LOCAL_ONLY')])
async def check():
    assert (await Agent(FunctionModel(model)).run('local')).output == 'LOCAL_ONLY'
asyncio.run(check())
from agent.ui_runtime import run_ui_stream
async def stream(observe):
    result = await Agent(FunctionModel(model)).run('local')
    yield result.output
assert run_ui_stream(stream) == 'LOCAL_ONLY'
print('SDK_AND_UI_PROCESS_EXIT_OK')
'''
        result = subprocess.run([sys.executable,"-u","-c",code],cwd=ROOT,capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr[-3000:])
        self.assertIn("SDK_AND_UI_PROCESS_EXIT_OK",result.stdout)
