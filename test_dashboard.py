import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from dashboard import Dashboard, ORDER_SERVICES
from plan_options import account_filter, quiet_end, schedule_order


class DashboardTests(unittest.TestCase):
    def test_completed_actions_and_history_survive_pruned_jobs(self):
        class Client:
            def is_connected(self):
                return True

        accounts = {
            "active": SimpleNamespace(me=SimpleNamespace(id=1), client=Client()),
            "frozen": SimpleNamespace(me=SimpleNamespace(id=2), client=Client(), freeze={"frozen": True}),
        }
        jobs = {"order": {"id": "order", "title": "Views", "status": "done",
                          "items": [{"state": "ok"} for _ in range(100)] + [{"state": "err"}]}}
        app = {ORDER_SERVICES: {"views": SimpleNamespace(jobs=jobs)}}
        balances = {"active": {"balance": "42.5"}, "frozen": {"balance": "7.5"}}
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Dashboard(directory, lambda: accounts, lambda: balances).snapshot(app, save=True)
            self.assertEqual((snapshot["accounts"], snapshot["active"], snapshot["frozen"]), (2, 1, 1))
            self.assertEqual((snapshot["balance"], snapshot["largest"]), (50, 42.5))
            self.assertEqual(snapshot["series"][0]["active"], 1)
            jobs.clear()
            restored = Dashboard(directory, lambda: accounts, lambda: balances).snapshot(app)
            self.assertEqual(restored["completed"], 100)

    def test_only_matching_cached_profile_attributes_are_selected(self):
        metadata = {"public": {"flag": "public", "avatar_count": 3, "music_count": 0},
                    "general": {"flag": "general", "avatar_count": 1, "music_count": 2},
                    "unknown": {"flag": "general", "avatar_count": None}}
        rules = [{"field": "avatar_count", "op": ">", "value": 2},
                 {"field": "flag", "op": "=", "value": "public"}]
        both = account_filter({"account_filters": rules}, metadata.get)
        either = account_filter({"account_filters": rules + [{"field": "music_count", "op": ">=", "value": 2}],
                                 "filter_mode": "or"}, metadata.get)
        self.assertEqual([name for name in metadata if both(name)], ["public"])
        self.assertEqual([name for name in metadata if either(name)], ["public", "general"])

    def test_realistic_schedule_respects_quiet_hours_and_deadline(self):
        start = datetime(2026, 11, 26, 18, tzinfo=timezone.utc).timestamp()
        finish = datetime(2026, 11, 27, 9, tzinfo=timezone.utc).timestamp()
        quiet = (23 * 3600, 7 * 3600)
        items = [{"base_delay": 0} for _ in range(70)]
        schedule_order(items, start, finish, quiet, (120, 70))
        times = [start + item["at"] for item in items]
        self.assertEqual(times, sorted(set(times)))
        self.assertTrue(all(quiet_end(when, quiet) is None for when in times))
        self.assertTrue(all(start < when < finish for when in times))
        self.assertTrue(any(when >= datetime(2026, 11, 27, 4, tzinfo=timezone.utc).timestamp() for when in times))
