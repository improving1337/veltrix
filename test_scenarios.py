import base64
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import main
import scenarios


class CompilerTests(unittest.TestCase):
    def test_group_exclusions_and_action_order(self):
        code = '''
accounts = group("Основные") - [20]
for acc in accounts:
    views(acc, param("post"), unique=True)
    sub(account=acc, channel="@channel")
    wait(10, 15)
poll(account=20, post=param("post"), option=1)
'''
        steps = scenarios.Compiler({10: "Анна", 20: "Борис", 30: "Вера"},
                                   {"Основные": [10, 20, 30]}, {"post": "t.me/Channel/1"}).compile(code)
        self.assertEqual([(s["action"], s.get("account")) for s in steps],
                         [("views", 10), ("sub", 10), ("wait", None),
                          ("views", 30), ("sub", 30), ("wait", None), ("poll", 20)])
        self.assertEqual(steps[0]["target"], "https://t.me/channel/1")
        self.assertTrue(steps[0]["unique"])
        self.assertEqual(steps[2]["delay"], [10, 15])

    def test_rejects_python_execution_and_unknown_accounts(self):
        for code in ('import os', 'open("secrets")', 'item = (1).__class__',
                     'views(11, "t.me/channel/1")', 'wait(15, 10)', 'views(all, "t.me/channel/1", unique=1)'):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "Строка 1"):
                scenarios.Compiler({10: "Анна"}, {}, {}).compile(code)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        me = SimpleNamespace(id=10, first_name="Анна", last_name="", username=None)
        self.accounts = lambda: {"account": SimpleNamespace(me=me, client=SimpleNamespace(is_connected=lambda: True))}
        self.service = scenarios.Scenarios(self.directory, self.accounts)

    async def test_preview_confirmation_and_persisted_unique(self):
        app = web.Application(middlewares=[main.auth_mw])
        self.service.register(app)
        auth = {"Authorization": "Basic " + base64.b64encode(f"{main.PANEL_USER}:test".encode()).decode(),
                "X-Panel": "1"}
        path = "/api/scenarios"
        body = {"code": 'views(all, "t.me/channel/1", unique=True)\nviews(all, "t.me/channel/1", unique=True)'}
        with (patch.object(main, "PANEL_PASSWORD", "test"),
              patch.object(self.service, "execute", AsyncMock(return_value=("ok", "Просмотр"))) as action):
            async with TestClient(TestServer(app)) as client:
                self.assertEqual((await client.get(path)).status, 401)
                preview = await (await client.post(path + "/preview", headers=auth, json=body)).json()
                self.assertEqual(action.await_count, 0)
                self.assertIsNone(self.service.state["run"])
                self.assertFalse(self.service.path.exists())
                self.assertEqual([step["status"] for step in preview["simulation"]["steps"]],
                                 ["planned", "depends"])
                response = await client.post(path + "/start", headers=auth, json=body | {"token": "bad"})
                self.assertEqual(response.status, 400)
                response = await client.post(path + "/start", headers=auth, json=body | {"token": preview["token"]})
                self.assertEqual(response.status, 200)
                await self.service.task
                self.assertEqual(action.await_count, 1)
        restored = scenarios.Scenarios(self.directory, self.accounts)
        self.assertEqual(restored.snapshot()["run"]["status"], "finished")
        self.assertEqual([x["status"] for x in restored.snapshot()["run"]["log"]], ["ok", "skip"])
        self.assertEqual(len(restored.state["journal"]), 1)

    async def test_simulation_observes_midnight_pause_and_local_history(self):
        now = datetime(2026, 11, 24, 20, 59, 45, tzinfo=timezone.utc).timestamp()
        plan = self.service.plan({
            "code": 'wait(25, 30)\nviews(all, "t.me/channel/1", unique=True)\nsub(all, "@channel", unique=True)',
            "quiet_hours": {"from": "00:00", "to": "07:00"},
        })
        journal = {scenarios.action_key(plan["steps"][1]): True}
        with patch("scenarios.random.uniform", side_effect=[25, 360]):
            simulation = scenarios.simulate(plan, journal, now)
        wait, view, subscription = simulation["steps"]
        self.assertEqual(wait["ends"], now + 25)
        self.assertEqual(view["begins"], datetime(2026, 11, 25, 4, 6, tzinfo=timezone.utc).timestamp())
        self.assertEqual(view["status"], "recorded")
        self.assertEqual(subscription["status"], "membership")
        self.assertEqual(subscription["begins"], view["begins"])

    async def test_in_flight_restart_waits_for_operator(self):
        plan = self.service.plan({"code": 'send(all, "me", "hello")'})
        self.service.state["run"] = plan | {"index": 0, "in_flight": True, "status": "running", "log": [], "due": 0}
        self.service.save()
        restored = scenarios.Scenarios(self.directory, self.accounts)
        async for _ in restored.lifecycle(None):
            self.assertIsNone(restored.task)
            self.assertEqual(restored.snapshot()["run"]["status"], "uncertain")
            self.assertEqual(restored.snapshot()["run"]["index"], 0)

    async def test_quiet_hours_and_flood_wait_defer_current_step(self):
        for case in ("quiet", "flood"):
            with self.subTest(case=case):
                plan = self.service.plan({"code": 'views(all, "t.me/channel/1")'})
                run = plan | {"index": 0, "in_flight": False, "status": "running", "log": [], "due": 0}
                self.service.state["run"] = run
                self.service.execute = AsyncMock(return_value=("err", "FloodWait: подождать 25 сек"))
                def pause(_):
                    run["status"] = "paused"
                with (patch("scenarios.time.time", return_value=1000),
                      patch("scenarios.plan_options.quiet_end", return_value=2000 if case == "quiet" else None),
                      patch("scenarios.plan_options.after_quiet", return_value=2020),
                      patch("scenarios.asyncio.sleep", side_effect=pause)):
                    await self.service.worker()
                self.assertEqual(run["index"], 0)
                self.assertEqual(run["due"], 2020 if case == "quiet" else 1025)
                self.assertEqual(self.service.execute.await_count, 0 if case == "quiet" else 1)
