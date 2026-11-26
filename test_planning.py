import asyncio
import time
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telethon.errors import ChatForwardsRestrictedError
from telethon import functions, types

import automatic
import channels
import multi
import plan_options
import posts
import unsub


class ScheduleTests(unittest.TestCase):
    @staticmethod
    def moscow(day, hour, minute=0):
        return datetime(2026, 11, day, hour, minute, tzinfo=timezone(timedelta(hours=3))).timestamp()

    def test_quiet_hours_shift_and_rebase_relative_delays(self):
        start = self.moscow(19, 23)
        quiet = plan_options.quiet_hours({'quiet_hours': {'from': '00:00', 'to': '07:00'}})
        items = [{'base_delay': 1500} for _ in range(4)]
        with patch('plan_options.random.uniform', return_value=360):
            plan_options.schedule_order(items, start, None, quiet)
        self.assertEqual([start + item['at'] for item in items], [
            self.moscow(19, 23, 25), self.moscow(19, 23, 50),
            self.moscow(20, 7, 6), self.moscow(20, 7, 31)])

    def test_deadline_uses_open_time_across_midnight(self):
        quiet = plan_options.quiet_hours({'quiet_hours': {'from': '23:30', 'to': '07:00'}})
        start, finish = self.moscow(19, 23), self.moscow(20, 8)
        items = [{} for _ in range(10)]
        plan_options.schedule_order(items, start, finish, quiet)
        due = [start + item['at'] for item in items]
        self.assertEqual(due[-1], finish)
        self.assertTrue(all(plan_options.quiet_end(at, quiet) is None for at in due))
        self.assertLess(due[0], self.moscow(19, 23, 30))
        with self.assertRaisesRegex(ValueError, 'интервал пропуска'):
            plan_options.schedule_order(items, start, self.moscow(20, 6), quiet)
        with self.assertRaisesRegex(ValueError, 'нет доступного времени'):
            plan_options.schedule_order(items, self.moscow(20, 6), self.moscow(20, 7), quiet)

    def test_invalid_quiet_hours(self):
        for value in [True, {'from': '07:00', 'to': '07:00'}, {'from': '24:00', 'to': '7:00'}]:
            with self.assertRaises(ValueError):
                plan_options.quiet_hours({'quiet_hours': value})

    def test_views_and_reactions_share_burst_and_migrate_without_repeating(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = {str(n): object() for n in range(100)}
            monitor = automatic.Monitor(Path(directory)/'state.json', lambda: clients)
            monitor.state['hours'] = [34, 66]+[0]*22
            published = datetime(2026, 11, 17, tzinfo=timezone.utc)
            post = {'post_date': published.isoformat(), 'execution': {}}
            for service, emojis in [('views', []), ('reactions', [('👍', 100)])]:
                self.assertTrue(monitor.update_execution_plan(post, service, 100, emojis, published))
                plan = post['execution'][service]
                self.assertEqual(sum(datetime.fromisoformat(a['due']) < published+timedelta(seconds=30)
                                     for a in plan['actions']), 17)
                self.assertFalse(monitor.update_execution_plan(post, service, 100, emojis, published))
                plan['actions'][0]['status'] = 'done'
                done_account = plan['actions'][0]['account']
                plan.pop('timing_version')
                monitor.update_execution_plan(post, service, 100, emojis, published)
                actions = post['execution'][service]['actions']
                self.assertEqual(len(actions), 100)
                self.assertEqual(sum(a['account'] == done_account for a in actions), 1)
                self.assertEqual(sum(a['status'] == 'done' for a in actions), 1)

    def test_deadline_exact_and_each_interval_jittered(self):
        for count in [1, 2, 3, 5, 6, 21, 100]:
            for _ in range(20):
                items = [{} for i in range(count)]
                plan_options.schedule(items, 28800)
                self.assertEqual(items[-1]['at'], 28800)
                self.assertAlmostEqual(sum(i['delay'] for i in items), 28800)
                self.assertEqual(sorted(i['at'] for i in items), [i['at'] for i in items])
                for item in items:
                    deviation = abs(item['delay'] / (28800/count) - 1)
                    if count > 1:
                        self.assertGreaterEqual(deviation, .1 - 1e-10)
                        self.assertLessEqual(deviation, .4 + 1e-10)

    def test_first_30_seconds_half_of_first_hour(self):
        published = datetime(2026, 11, 17, tzinfo=timezone.utc)
        slots = [datetime.fromisoformat(s) for s in automatic.timed_slots(100, [34, 66]+[0]*22, published.isoformat(), published)]
        self.assertEqual(len(slots), 100)
        self.assertEqual(sum(s < published+timedelta(seconds=30) for s in slots), 17)
        self.assertEqual(sum(s < published+timedelta(hours=1) for s in slots), 34)

    def test_late_discovery_no_backdated_burst(self):
        published = datetime(2026, 11, 17, tzinfo=timezone.utc)
        now = published+timedelta(seconds=40)
        slots = automatic.timed_slots(100, [34, 66]+[0]*22, published, now)
        self.assertEqual(len(slots), 100)
        self.assertTrue(all(datetime.fromisoformat(s) > now for s in slots))

    def test_expired_deadline_rejected(self):
        for value in [time.time()-1, float('nan'), float('inf'), 'bad']:
            with self.assertRaises(ValueError):
                plan_options.deadline(value)


def client(age=None, broken=False):
    c = NS(is_connected=lambda: True)
    c.get_profile_photos = AsyncMock(side_effect=RuntimeError() if broken else None,
                                    return_value=[] if age is None else [NS(date=datetime.now(timezone.utc)-timedelta(days=age))])
    c.get_entity = AsyncMock(return_value=NS(title='Channel'))
    c.get_messages = AsyncMock(return_value=NS(buttons=[[NS(text='Button')]]))
    return c


class EligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_options_and_scheduled_votes(self):
        choices = [types.PollAnswer(types.TextWithEntities(label, []), key)
                   for label, key in [('Первый', b'a'), ('Второй', b'b')]]
        poll = types.Poll(1, types.TextWithEntities('Вопрос', []), choices, 0)
        clients = {str(i): AsyncMock() for i in range(3)}
        for selected in clients.values():
            selected.is_connected = lambda: True
            selected.get_entity = AsyncMock(return_value=NS(title='Channel'))
            selected.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(1, 2))
            selected.get_messages = AsyncMock(return_value=NS(media=types.MessageMediaPoll(poll, types.PollResults())))
        joiner = posts.PollJoiner(lambda: clients)
        link = 'https://t.me/example/123'
        self.assertEqual((await joiner.options(link))['options'][1], {'id': '62', 'text': 'Второй'})
        plan = await joiner.make_plan(link, None, 0, 0, {'votes': {'61': 2, '62': 1}})
        self.assertCountEqual([item['option'] for item in plan['items']], ['Первый', 'Первый', 'Второй'])
        await joiner.start(plan['id'])
        await joiner.jobs[plan['id']]['task']
        requests = [client.await_args.args[0] for client in clients.values()]
        self.assertTrue(all(isinstance(request, functions.messages.SendVoteRequest) for request in requests))
        self.assertCountEqual([request.options[0] for request in requests], [b'a', b'a', b'b'])

    async def test_acceleration_cannot_trigger_action_in_quiet_hours(self):
        start = ScheduleTests.moscow(20, 0)
        resume = ScheduleTests.moscow(20, 7) + 360
        quiet = plan_options.quiet_hours({'quiet_hours': {'from': '00:00', 'to': '07:00'}})
        joiner = channels.ChannelJoiner(lambda: {'a': client()})
        item = {'account': 'a', 'base_delay': 1500, 'at': resume - start, 'state': 'queue'}
        job = {'id': 'job', 'status': 'running', 'quiet': quiet, 'items': [item],
               'stop': asyncio.Event(), 'skip': asyncio.Event()}
        joiner.jobs['job'] = job
        attempts = []
        now = start

        async def accelerated(job, delay):
            joiner.skip('job')
            self.assertTrue(job['skip'].is_set())
            return False

        async def wait_out_pause(wait, timeout):
            nonlocal now
            wait.close()
            self.assertEqual(timeout, resume - start)
            now = resume
            raise asyncio.TimeoutError

        async def action(*args):
            attempts.append(now)
            return 'ok', ''

        with (patch('channels.time.time', side_effect=lambda: now),
              patch('plan_options.random.uniform', return_value=360),
              patch.object(joiner, '_sleep', accelerated),
              patch.object(joiner, '_join', action),
              patch('channels.asyncio.wait_for', wait_out_pause)):
            await joiner._run(job, 'public', 'example')
        self.assertEqual(attempts, [resume])

    async def test_restart_keeps_due_time_and_waits_for_same_account(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / 'orders.json'
            available = {'a': client()}
            joiner = channels.ChannelJoiner(lambda: available)
            joiner.load_jobs(storage)
            joiner.account_ids = lambda: {'a': 99}
            joiner._subscribed = AsyncMock(return_value=(set(), 0))
            plan = await joiner.make_plan('@example', 1, 100, 100)
            waiting = asyncio.Event()

            async def pause(*args):
                waiting.set()
                await asyncio.Future()

            joiner._sleep = pause
            await joiner.start(plan['id'])
            await asyncio.wait_for(waiting.wait(), 1)
            item = joiner.jobs[plan['id']]['items'][0]
            due = item['until']
            await joiner.suspend_jobs()

            restored = channels.ChannelJoiner(lambda: available)
            restored.account_connecting = lambda name: name == 'a'
            restored.account_ids = lambda: {'a': 99}
            restored.load_jobs(storage)
            available.clear()

            async def wait_done(job, delay):
                self.assertAlmostEqual(time.time() + delay, due, places=1)
                return False

            async def connect():
                await asyncio.sleep(.01)
                available['a'] = client()

            restored._sleep = wait_done
            restored._join = AsyncMock(return_value=('ok', ''))
            await restored.resume_jobs()
            await connect()
            await asyncio.wait_for(restored.jobs[plan['id']]['task'], 3)
            restored._join.assert_awaited_once()
            self.assertEqual(restored.jobs[plan['id']]['status'], 'done')

    async def test_restart_does_not_repeat_in_flight_action(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / 'orders.json'
            clients = {'a': client(), 'b': client()}
            joiner = channels.ChannelJoiner(lambda: clients)
            joiner.load_jobs(storage)
            joiner._subscribed = AsyncMock(return_value=(set(), 0))
            plan = await joiner.make_plan('@example', 2, 0, 0)
            started = asyncio.Event()

            async def do_action(*args):
                started.set()
                await asyncio.Future()

            joiner._join = do_action
            await joiner.start(plan['id'])
            await asyncio.wait_for(started.wait(), 1)
            await joiner.suspend_jobs()

            restored = channels.ChannelJoiner(lambda: clients)
            restored.load_jobs(storage)
            restored._join = AsyncMock(return_value=('ok', ''))
            await restored.resume_jobs()
            job = restored.jobs[plan['id']]
            await asyncio.wait_for(job['task'], 1)
            restored._join.assert_awaited_once_with(clients[plan['items'][1]['account']], 'public', 'example', None)
            self.assertIn('неизвестен', job['items'][0]['note'])
            self.assertEqual([item['state'] for item in job['items']], ['err', 'ok'])

    async def test_stop_is_persisted_before_the_worker_can_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / 'orders.json'
            clients = {'a': client()}
            joiner = channels.ChannelJoiner(lambda: clients)
            joiner.load_jobs(storage)
            joiner._subscribed = AsyncMock(return_value=(set(), 0))
            plan = await joiner.make_plan('@example', 1, 90, 90)
            await joiner.start(plan['id'])
            joiner.stop(plan['id'])
            await joiner.suspend_jobs()
            restored = channels.ChannelJoiner(lambda: clients)
            restored.load_jobs(storage)
            await restored.resume_jobs()
            job = restored.jobs[plan['id']]
            self.assertEqual(job['status'], 'stopped')
            self.assertEqual(job['items'][0]['state'], 'skip')
            self.assertNotIn('task', job)

    async def test_freshness_recent_old_absent_unknown(self):
        clients = {'recent': client(2), 'old': client(8), 'no_photo': client(), 'unknown': client(broken=True)}
        filtered = await plan_options.eligible(clients, {'freshness_days': 7})
        self.assertEqual(set(filtered), {'old', 'no_photo'})
        self.assertEqual(await plan_options.eligible(clients, {'freshness_days': 0}), clients)

    async def test_noninteger_days_rejected(self):
        for days in ['1.2', 1.2, -1, 36501]:
            with self.assertRaises(ValueError):
                await plan_options.eligible({}, {'freshness_days': days})

    async def test_every_service_applies_options(self):
        clients = {'recent': client(2), 'old': client(9)}
        for cls in [channels.ChannelJoiner, posts.ViewsJoiner, posts.ReactionJoiner, posts.RepostJoiner,
                    multi.MultiJoiner, multi.ButtonsJoiner, multi.StartsJoiner, unsub.UnsubJoiner]:
            joiner = cls(lambda: clients)
            subscribed = {'old'} if cls is unsub.UnsubJoiner else set()
            joiner._subscribed = AsyncMock(return_value=(subscribed, 0))
            joiner._title = AsyncMock(return_value='Test')
            link = 'https://t.me/example/123' if cls not in [channels.ChannelJoiner, unsub.UnsubJoiner, multi.StartsJoiner] else '@example'
            finish = time.time()+28800
            plan = await joiner.make_plan(link, 10, 0, 0,
                                         {'freshness_days': 7, 'finish_at': finish, 'emoji': '👍'})
            self.assertEqual([i['account'] for i in plan['items']], ['old'], cls.__name__)
            self.assertEqual(plan['finish_at'], finish, cls.__name__)
            self.assertGreater(plan['total'], 28790)

    async def test_multi_reactions_are_evenly_spaced_and_match_started_job(self):
        clients = {str(i): client() for i in range(10)}
        for connected in clients.values():
            connected.get_messages = AsyncMock(return_value=NS(buttons=[]))
        joiner = multi.MultiJoiner(lambda: clients)
        link = 'https://t.me/example/123'
        for percent in (0, 30, 50, 70, 100):
            with self.subTest(percent=percent):
                plan = await joiner.make_plan(link, 10, 0, 0, {'reaction_percent': percent, 'emoji': '👍'})
                self.assertEqual(plan['reaction_count'], percent // 10)
                marked = [index for index, item in enumerate(plan['items'], 1) if item['reaction_planned']]
                if percent == 50:
                    self.assertEqual(marked, [2, 4, 6, 8, 10])
                for index in range(1, 11):
                    self.assertLess(abs(sum(position <= index for position in marked) - index*percent/100), 1)
        plan = joiner.exclude(plan['id'], plan['items'][0]['account'])
        self.assertEqual(plan['reaction_count'], 9)
        plan = await joiner.make_plan(link, 10, 0, 0, {'reaction_percent': 50, 'emoji': '👍'})
        plan = joiner.exclude(plan['id'], plan['items'][0]['account'])
        expected = [item['account'] for item in plan['items'] if item['reaction_planned']]
        with patch.object(joiner, '_run', new=AsyncMock()):
            await joiner.start(plan['id'])
            job = joiner.jobs[plan['id']]
            await job['task']
        self.assertEqual(job['extra']['reaction_accounts'], expected)
        for item in job['items']:
            with patch.object(multi.ViewsJoiner, '_join', new=AsyncMock(return_value=('ok','просмотр'))), \
                 patch.object(multi.ReactionJoiner, '_join', new=AsyncMock(return_value=('ok','реакция'))) as reaction, \
                 patch.object(multi, 'click_first_button', new=AsyncMock(return_value=('ok','кнопка'))), \
                 patch.object(multi.random, 'uniform', return_value=0):
                await joiner._steps(job, item, clients[item['account']], job['key'])
                self.assertEqual(reaction.await_count, int(item['account'] in expected))

    async def test_reposts_forward_once_to_each_selected_accounts_saved_messages(self):
        clients = {'first': client(), 'second': client()}
        sources = {name: object() for name in clients}
        for name, selected in clients.items():
            selected.get_input_entity = AsyncMock(return_value=sources[name])
            selected.forward_messages = AsyncMock(return_value=object())
        app = web.Application()
        channels.register(app, lambda: clients)
        async with TestClient(TestServer(app)) as http:
            response = await http.post('/api/reposts/plan', json={
                'link': 'https://t.me/example/123', 'count': 3, 'delay_from': 0, 'delay_to': 0,
            })
            self.assertEqual(response.status, 200)
            plan = await response.json()
            self.assertCountEqual([item['account'] for item in plan['items']], clients)
            for selected in clients.values():
                selected.forward_messages.assert_not_awaited()
            response = await http.post('/api/reposts/start', json={'plan_id': plan['id']})
            self.assertEqual(response.status, 200)
            async with asyncio.timeout(3):
                while True:
                    response = await http.get('/api/reposts/job')
                    job = (await response.json())['jobs'][0]
                    if job['status'] != 'running':
                        break
                    await asyncio.sleep(.01)
            self.assertEqual(job['status'], 'done')
            self.assertEqual([item['state'] for item in job['items']], ['ok', 'ok'])
            for name, selected in clients.items():
                selected.get_input_entity.assert_awaited_once_with('example')
                selected.forward_messages.assert_awaited_once_with('me', 123, from_peer=sources[name])

    async def test_repost_respects_wait_guard_and_reports_protected_channel(self):
        selected = client()
        selected.get_input_entity = AsyncMock()
        selected.forward_messages = AsyncMock(side_effect=ChatForwardsRestrictedError(None))
        guard = AsyncMock(return_value=True)
        state, _ = await posts.RepostJoiner._join(selected, 'post', ('example', 123),
                                                 {'wait_before_action': guard})
        self.assertEqual(state, 'skip')
        guard.assert_awaited_once()
        selected.forward_messages.assert_not_awaited()
        state, note = await posts.RepostJoiner._join(selected, 'post', ('example', 123))
        self.assertEqual(state, 'err')
        self.assertIn('запретил пересылку', note)

    async def test_max_checks_real_service_pool_and_leaves_no_plan(self):
        clients = {'recent': client(2), 'old': client(9), 'subscribed': client(10)}
        class Joiner(channels.ChannelJoiner):
            async def _title(self, *args):
                return 'Test'
            async def _subscribed(self, *args):
                return {'subscribed'}, 0
        app = web.Application()
        joiner = channels.register(app, lambda: clients, prefix='test', joiner_cls=Joiner)
        async with TestClient(TestServer(app)) as http:
            response = await http.post('/api/test/available', json={'link': '@example', 'extra': {'freshness_days': 7}})
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {'count': 1})
            self.assertEqual(joiner.plans, {})

    async def test_start_rebases_to_deadline_and_expired_plan_retained(self):
        clients = {'a': client(), 'b': client()}
        joiner = channels.ChannelJoiner(lambda: clients)
        joiner._title = AsyncMock(return_value='Test')
        joiner._subscribed = AsyncMock(return_value=(set(), 0))
        joiner._run = AsyncMock()
        plan = await joiner.make_plan('@example', 2, 0, 0, {'finish_at': time.time()+60})
        job = (await joiner.start(plan['id']))['job']
        self.assertAlmostEqual(job['started']+job['items'][-1]['at'], plan['finish_at'], places=2)
        await joiner.jobs[plan['id']]['task']
        plan = await joiner.make_plan('@example', 2, 0, 0, {'finish_at': time.time()+60})
        joiner.plans[plan['id']]['finish_at'] = time.time()-1
        with self.assertRaises(ValueError):
            await joiner.start(plan['id'])
        self.assertIn(plan['id'], joiner.plans)

    async def test_exclude_keeps_deadline(self):
        clients = {str(n): client() for n in range(5)}
        joiner = channels.ChannelJoiner(lambda: clients)
        joiner._title = AsyncMock(return_value='Test')
        joiner._subscribed = AsyncMock(return_value=(set(), 0))
        finish = time.time()+28800
        plan = await joiner.make_plan('@example', 5, 0, 0, {'finish_at': finish})
        plan = joiner.exclude(plan['id'], plan['items'][0]['account'])
        self.assertEqual(len(plan['items']), 4)
        self.assertAlmostEqual(plan['items'][-1]['at'], finish-time.time(), places=1)


if __name__ == '__main__':
    unittest.main()
