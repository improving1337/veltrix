import asyncio
import json
import random
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import web
from telethon import types
from telethon.errors import FloodWaitError
import automatic
from automatic_multi import Hub, _JsonRequest, channel_key


class ViewCurveTests(unittest.TestCase):
    @staticmethod
    def counts(hour, minute=0, seed=1, day=25, total=200):
        published = datetime(2026, 11, day, hour, minute, tzinfo=automatic.MOSCOW)
        curve = automatic.view_curve(published, automatic.DEFAULT_VIEW_ACTIVITY, random.Random(seed))
        return automatic.view_counts(total, curve, 10)

    def test_moscow_peaks_and_smooth_night_transition(self):
        for hour in (0, 3, 5, 10, 15, 20, 23):
            with self.subTest(hour=hour):
                counts = self.counts(hour)
                self.assertEqual(len(counts), 48)
                self.assertEqual((sum(counts[:24]), sum(counts[24:])), (180, 20))
                if hour in (0, 3, 5, 23):
                    peak = max(range(24), key=counts.__getitem__)
                    self.assertIn((hour + peak) % 24, (7, 8, 9))
        before, after = self.counts(22, 59), self.counts(23)
        self.assertLessEqual(sum(abs(a - b) for a, b in zip(before, after)), 6)
        self.assertGreater(self.counts(0)[0], self.counts(3)[0])
        self.assertGreater(self.counts(20)[0], self.counts(10)[0])
        self.assertGreater(self.counts(15)[4], self.counts(15)[2])

    def test_each_post_gets_a_new_shape_and_small_counts_are_conserved(self):
        self.assertNotEqual(self.counts(22, 30, 1), self.counts(22, 30, 2, 26))
        for total in (1, 5, 11, 25, 30):
            self.assertEqual(sum(self.counts(23, total=total)), total)

    def test_late_schedule_respects_remaining_window(self):
        published = datetime(2026, 11, 25, 0, tzinfo=automatic.MOSCOW)
        curve = automatic.view_curve(published, automatic.DEFAULT_VIEW_ACTIVITY, random.Random(1))
        later = published + timedelta(hours=27)
        due = automatic.timed_slots(10, curve, published, later, day2_pct=10)
        self.assertEqual(len(due), 10)
        self.assertTrue(all(later <= automatic.parse_timestamp(slot) < published + timedelta(hours=48)
                            for slot in due))
        self.assertEqual(automatic.timed_slots(10, curve, published, later, day2_pct=0), [])


class RepostBudgetTests(unittest.TestCase):
    def test_ordinary_news_giveaway_and_long_term_impact(self):
        with patch('automatic.random.randint', side_effect=lambda low, high: high):
            routine = automatic.repost_percent('Игрок дал короткое интервью.\n\nРеклама: розыгрыш приза.', {})
            major = automatic.repost_percent('Команда объявила о закрытии состава.', {})
            giveaway = automatic.repost_percent('Запускаем розыгрыш трёх билетов.', {})
            ai = automatic.repost_percent('Вышло заявление организаторов.', {'repost_priority': 'major'})
        self.assertEqual([routine, major, giveaway, ai], [3, 8, 10, 8])
        self.assertEqual(automatic.repost_target(200, routine), 6)
        self.assertEqual(automatic.repost_target(29, giveaway), 2)
        self.assertEqual(automatic.repost_target(9, giveaway), 0)
        for views in range(200):
            for percent in range(2, 11):
                self.assertLessEqual(automatic.repost_target(views, percent) * 10, views)


class AutopilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_spreads_are_frozen_for_each_post(self):
        mon = self.monitor
        settings = {'view_spread': 25, 'reaction_spread': 30}
        mon.preferences = lambda: settings
        mon.state.update(max_views=40, max_reactions=20)
        with patch('automatic.random.randint', side_effect=lambda lo, hi: lo):
            old = mon.add_post(self.message(1, 'Test'), self.now.isoformat())
            settings.update(view_spread=0, reaction_spread=0)
            self.assertEqual(mon.view_target(old), 30)
            self.assertEqual(old['reaction_budget'], 14)
            new = mon.add_post(self.message(2, 'Test'), self.now.isoformat())
            self.assertEqual(mon.view_target(new), 40)
            self.assertEqual(new['reaction_budget'], 20)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clients = {str(i): object() for i in range(100)}
        self.monitor = automatic.Monitor(self.root/'channel.json', lambda:self.clients)
        self.now = datetime(2026,11,17,12,tzinfo=timezone.utc)

    async def asyncTearDown(self):
        await self.monitor.close()
        self.temp.cleanup()

    def post(self, ident=1, age=1):
        return {'post_id':ident,'post_date':(self.now-timedelta(hours=age)).isoformat(),
                'post_text':'Test','samples':[],'execution':{},'suggestion':None}

    def message(self, ident, text):
        return SimpleNamespace(id=ident, message=text, date=self.now)

    async def test_discovery_settings_survive_changes_and_restart(self):
        mon = self.monitor
        mon.state.update(max_views=20, max_reactions=10, execute_plan=True)
        with patch('automatic.random.randint', side_effect=lambda low, high: high):
            old = mon.add_post(self.message(1, 'Реклама: скидка на подписку'), self.now.isoformat())
        old_settings = mon.capture_settings()
        old_views = mon.view_target(old)
        await mon.configure(_JsonRequest({
            'hours': [0, 100] + [0] * 22, 'max_views': 0, 'max_reactions': 2,
            'execute_plan': False, 'views_enabled': False, 'subscribers_only': True,
            'allowed_reactions': ['💩'], 'reaction_prompt': 'Updated',
        }))
        with patch('automatic.random.randint', side_effect=lambda low, high: high):
            new = mon.add_post(self.message(2, 'Реклама: скидка на подписку'), self.now.isoformat())
        mon.save()
        restored = automatic.Monitor(mon.storage, lambda: self.clients)
        self.assertEqual(restored.find_post(1)['settings'], old_settings)
        self.assertEqual(restored.find_post(2)['settings'], new['settings'])
        self.assertEqual(restored.view_target(restored.find_post(1)), old_views)
        self.assertEqual(restored.view_target(restored.find_post(2)), 0)
        with (patch.object(restored, 'channel_reactions', AsyncMock(return_value={'❤️', '💩', '😡'})),
              patch('automatic.env_value', return_value='')):
            await restored.analyze(1)
            await restored.analyze(2)
        self.assertEqual(restored.find_post(1)['suggestion']['total'], 10)
        self.assertEqual(restored.find_post(2)['suggestion']['reactions'], [{'emoji': '💩', 'count': 2}])
        restored.state['active'] = True
        with patch('automatic.utc_now', return_value=self.now):
            await restored.dispatch_actions()
        old_plans = restored.find_post(1)['execution']
        self.assertEqual(old_plans['views']['signature']['hours'], old_settings['hours'])
        self.assertEqual(old_plans['reactions']['target'], 10)
        self.assertEqual(restored.find_post(2)['execution'], {})

    async def test_two_day_due_times_persist_and_legacy_plan_stays_one_day(self):
        mon = self.monitor
        mon.preferences = lambda: {'view_spread': 0}
        mon.state.update(max_views=100, max_reactions=10, execute_plan=True)
        self.assertEqual(mon.state['view_mode'], 'legacy')
        old = mon.add_post(self.message(2, 'Test'), self.now.isoformat())
        mon.state['view_mode'] = 'realistic'
        new = mon.add_post(self.message(1, 'Test'), self.now.isoformat())
        split = self.now + timedelta(hours=24)
        mon.update_execution_plan(new, 'views', 100, [], self.now)
        mon.update_execution_plan(old, 'views', 100, [], self.now)
        initial = new['execution']['views']['actions']
        self.assertEqual(sum(automatic.parse_timestamp(a['due']) >= split for a in initial), 10)
        self.assertTrue(all(automatic.parse_timestamp(a['due']) < split
                            for a in old['execution']['views']['actions']))
        new['suggestion'] = automatic.validated_reaction_plan({'post_kind': 'ad'}, 10)
        old_curve = new['view_curve'].copy()
        with patch('automatic.utc_now', return_value=self.now):
            await mon.configure(_JsonRequest({
                'hours': automatic.DEFAULT_HOURS, 'max_views': 100, 'max_reactions': 10,
                'view_activity': [100] * 24, 'view_day2_pct': 25, 'view_mode': 'legacy',
            }))
        restored = automatic.Monitor(mon.storage, lambda: self.clients)
        restored.state['active'] = True
        tracked = restored.find_post(1)
        self.assertEqual(tracked['view_curve'], old_curve)
        self.assertEqual(tracked['settings']['view_day2_pct'], 10)
        self.assertEqual(tracked['execution']['views']['actions'], initial)
        self.assertEqual(restored.state['view_day2_pct'], 25)
        self.assertEqual(restored.state['view_mode'], 'legacy')
        self.assertEqual(automatic.view_window(restored.find_post(2)), 24)
        first_client = next(iter(self.clients))
        partial = {first_client: self.clients[first_client]}
        restored.update_execution_plan(tracked, 'views', 0, [], self.now, {})
        restored.update_execution_plan(tracked, 'views', 1, [], self.now, partial)
        self.assertEqual(tracked['execution']['views']['actions'], initial)
        for action in tracked['execution']['views']['actions'][:90]:
            action['status'] = 'done'
        action = tracked['execution']['views']['actions'][90]
        now = automatic.parse_timestamp(action['due'])
        with (patch('automatic.utc_now', return_value=now),
              patch.object(restored, 'perform_action', AsyncMock()) as perform):
            await restored.dispatch_actions()
            await asyncio.gather(*restored.action_tasks.values())
        perform.assert_awaited_once()
        self.assertEqual(perform.await_args.args[1], 'views')
        self.assertNotIn('reactions', tracked['execution'])
        self.assertEqual(restored.snapshot_post(tracked, now)['status'], 'active')
        self.assertEqual(restored.snapshot_post(restored.find_post(2), now)['status'], 'completed')

    async def test_preview_is_read_only_and_rejects_invalid_settings(self):
        data = {'published': '2026-11-25T22:30:00+03:00', 'total': 200, 'view_day2_pct': 15}
        legacy = json.loads((await self.monitor.preview_views(_JsonRequest(data))).text)
        self.assertEqual(legacy['counts'], automatic.apportion(200, automatic.DEFAULT_HOURS))
        self.monitor.state['view_mode'] = 'realistic'
        response = await self.monitor.preview_views(_JsonRequest(data))
        counts = json.loads(response.text)['counts']
        self.assertEqual((sum(counts[:24]), sum(counts[24:])), (170, 30))
        self.assertEqual(self.monitor.state['view_day2_pct'], 10)
        self.assertFalse(self.monitor.storage.exists())
        with self.assertRaises(web.HTTPBadRequest):
            await self.monitor.preview_views(_JsonRequest(data | {'view_activity': [0] * 24}))

    async def test_realistic_reactions_follow_local_peak_and_keep_old_plans(self):
        mon = self.monitor
        mon.state['view_mode'] = 'realistic'
        published = datetime(2026, 11, 25, 3, tzinfo=automatic.MOSCOW)
        message = self.message(1, 'Test')
        message.date = published
        new = mon.add_post(message, published.isoformat())
        old = new | {'post_id': 2, 'execution': {}}
        old.pop('reaction_timing')
        mon.state['posts'].append(old)
        for post in (new, old):
            mon.update_execution_plan(post, 'reactions', 100, [('❤️', 100)], published)
        expected = automatic.apportion(100, new['view_curve'][:24])
        actual = [0] * 24
        for action in new['execution']['reactions']['actions']:
            bucket = (automatic.parse_timestamp(action['due']) - published).total_seconds() // 3600
            self.assertLess(bucket, 24)
            actual[int(bucket)] += 1
        self.assertEqual(actual, expected)
        self.assertIn(actual.index(max(actual)) + 3, (7, 8, 9))
        self.assertGreater(max(actual), actual[0])
        self.assertEqual(mon.snapshot_post(new, published)['model_reaction_hours'], actual)
        self.assertEqual(mon.snapshot_post(old, published)['model_reaction_hours'], automatic.DEFAULT_HOURS)
        prior = old['execution']['reactions']['actions']
        mon.state['view_mode'] = 'legacy'
        mon.save()
        restored = automatic.Monitor(mon.storage, lambda: self.clients)
        self.assertEqual(restored.snapshot_post(restored.find_post(1), published)['model_reaction_hours'], actual)
        self.assertFalse(restored.update_execution_plan(
            restored.find_post(2), 'reactions', 100, [('❤️', 100)], published))
        self.assertEqual(restored.find_post(2)['execution']['reactions']['actions'], prior)

    async def test_reposts_are_saved_with_unique_accounts_and_use_existing_service(self):
        mon = self.monitor
        mon.preferences = lambda: {'view_spread': 0}
        mon.account_ids = lambda: {name: int(name) // 2 for name in self.clients}
        mon.state.update(max_views=50, max_reactions=0, execute_plan=True, active=True,
                         view_mode='realistic', subscribers_only=True, channel='https://t.me/example')
        post = mon.add_post(self.message(3, 'Начался розыгрыш билетов'), self.now.isoformat())
        subscribers = {name: self.clients[name] for name in map(str, range(0, 40, 2))}
        with (patch('automatic.env_value', return_value=''), patch('automatic.utc_now', return_value=self.now),
              patch('automatic.random.randint', side_effect=lambda low, high: high),
              patch.object(mon, 'subscriber_accounts', AsyncMock(return_value=subscribers))):
            await mon.analyze(3)
            await mon.dispatch_actions()
        restored = automatic.Monitor(mon.storage, lambda: self.clients, mon.account_ids)
        saved = restored.find_post(3)
        plan = saved['execution']['reposts']
        self.assertEqual(saved['repost_pct'], 10)
        self.assertEqual(plan['target'], 2)
        actions = plan['actions']
        self.assertEqual(len({action['account_id'] for action in actions}), 2)
        self.assertLessEqual({action['account'] for action in actions}, subscribers.keys())
        with (patch('posts.RepostJoiner._join', AsyncMock(return_value=('ok', 'Переслано'))) as send,
              patch.object(restored, 'is_subscriber', AsyncMock(return_value=True))):
            await restored.perform_action(3, 'reposts', actions[0], 'example')
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[2], ('example', 3))
        self.assertEqual(restored.snapshot_post(saved, self.now)['execution_reposts']['done'], 1)

    async def test_deepseek_repost_score_works_with_reactions_disabled(self):
        mon = self.monitor
        mon.state.update(max_views=50, max_reactions=0)
        post = mon.add_post(self.message(4, 'Появились подробности заявления.'), self.now.isoformat())
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={'choices': [
            {'message': {'content': '{"repost_priority":"major","post_kind":"news"}'}}
        ]}))
        session = MagicMock()
        session.post.return_value.__aenter__.return_value = response
        with (patch('automatic.env_value', return_value='test-key'),
              patch('automatic.random.randint', side_effect=lambda low, high: high),
              patch('automatic.aiohttp.ClientSession') as http,
              patch.object(mon, 'channel_reactions', AsyncMock()) as reactions):
            http.return_value.__aenter__.return_value = session
            await mon.analyze(4)
        reactions.assert_not_awaited()
        self.assertEqual(post['repost_pct'], 8)
        self.assertEqual(post['suggestion']['total'], 0)

    def poll_message(self, text='', close_period=25):
        poll = types.Poll(id=100, question=types.TextWithEntities('Кто победит?', []),
                          answers=[types.PollAnswer(types.TextWithEntities(option, []), key)
                                   for option, key in [('Синие', b'a'), ('Красные', b'b')]],
                          hash=0, close_period=close_period)
        msg = self.message(4, text)
        msg.media = types.MessageMediaPoll(poll, types.PollResults())
        return msg

    async def test_native_poll_ai_weights_schedule_and_restart(self):
        mon = self.monitor
        mon.preferences = lambda: {'view_spread': 0, 'reaction_spread': 0}
        mon.state.update(max_views=80, max_reactions=10, execute_plan=True)
        msg = self.poll_message('Описание матча')
        post = mon.add_post(msg, self.now.isoformat())
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={'choices': [
            {'message': {'content': '{"poll_weights":{"1":75,"2":25,"999":100},"reactions":[],"post_kind":"news"}'}}
        ]}))
        session = MagicMock()
        session.post.return_value.__aenter__.return_value = response
        with (patch('automatic.env_value', return_value='test-key'),
              patch('automatic.utc_now', return_value=self.now),
              patch('automatic.aiohttp.ClientSession') as http,
              patch.object(mon, 'channel_reactions', AsyncMock(return_value={'❤️', '👍'}))):
            http.return_value.__aenter__.return_value = session
            await mon.analyze(4)
            mon.state['active'] = True
            await mon.dispatch_actions()
        messages = session.post.call_args.kwargs['json']['messages']
        self.assertEqual(json.loads(messages[1]['content']), {
            'question': 'Кто победит?', 'description': 'Описание матча', 'quiz': False,
            'options': {'1': 'Синие', '2': 'Красные'},
        })
        view_actions = post['execution']['views']['actions']
        self.assertEqual(len(view_actions), 80)
        self.assertEqual(len(post['execution']['reactions']['actions']), 10)
        plan = post['execution']['votes']
        self.assertTrue(32 <= plan['target'] <= 48)
        self.assertEqual(len(plan['actions']), plan['target'])
        self.assertEqual(sum(action['option'] == '61' for action in plan['actions']),
                         automatic.apportion(plan['target'], [75, 25])[0])
        self.assertEqual(len({action['account_id'] for action in plan['actions']}), plan['target'])
        self.assertLessEqual({a['account'] for a in plan['actions']}, {a['account'] for a in view_actions})
        end = self.now + timedelta(seconds=25)
        self.assertTrue(all(self.now <= automatic.parse_timestamp(action['due']) < end for action in plan['actions']))
        restored = automatic.Monitor(mon.storage, lambda: self.clients)
        saved = restored.find_post(4)
        self.assertEqual(saved['execution']['votes'], plan)
        self.assertFalse(automatic.poll_open(saved, end))
        action = saved['execution']['votes']['actions'][0]
        with (patch('posts.PollJoiner._join', AsyncMock(return_value=('ok', 'Голос'))) as send,
              patch('automatic.utc_now', return_value=self.now)):
            await restored.perform_action(4, 'votes', action, 'example')
        self.assertEqual(send.await_args.args[-1]['option'], action['option'])
        self.assertEqual(restored.snapshot_post(saved, self.now)['execution_votes']['done'], 1)

    async def test_poll_without_caption_keeps_reactions_when_key_is_missing(self):
        mon = self.monitor
        mon.state.update(max_views=20, max_reactions=10)
        post = mon.add_post(self.poll_message(''), self.now.isoformat())
        self.assertTrue(mon.needs_analysis(post, self.now))
        with (patch('automatic.env_value', return_value=''),
              patch('automatic.utc_now', return_value=self.now),
              patch.object(mon, 'channel_reactions', AsyncMock(return_value={'❤️'}))):
            await mon.analyze(4)
            self.assertFalse(mon.needs_analysis(post, self.now))
        self.assertEqual(post['suggestion']['status'], 'ok')
        self.assertGreater(post['suggestion']['total'], 0)
        self.assertEqual(post['poll']['analysis'], 'missing_key')
        self.assertEqual(post['poll']['weights'], {})

    async def test_existing_vote_is_kept(self):
        from posts import PollJoiner

        msg = self.poll_message()
        client = AsyncMock()
        client.get_messages.return_value = msg
        extra = {'option': '62', 'poll_id': '100'}
        msg.media.results.results = [SimpleNamespace(chosen=True)]
        status, _ = await PollJoiner._join(client, 'post', ('example', 4), extra)
        self.assertEqual(status, 'err')
        client.assert_not_awaited()
        msg.media.results.results = []
        status, _ = await PollJoiner._join(client, 'post', ('example', 4), extra)
        self.assertEqual(status, 'ok')
        self.assertEqual(client.await_args.args[0].options, [b'b'])

    async def test_subscribers_only_for_views_and_reactions(self):
        class Client:
            def __init__(self, participant):
                self.participant = participant
                self.channels = []

            async def get_input_entity(self, channel):
                self.channels.append(channel)
                return types.InputChannel(1, 1)

            async def __call__(self, request):
                if self.participant is None:
                    raise ConnectionError()
                return SimpleNamespace(participant=self.participant)

        member = types.ChannelParticipant(1, self.now)
        left = types.ChannelParticipantLeft(types.PeerUser(2))
        self.clients = {'member': Client(member), 'left': Client(left), 'unknown': Client(None)}
        mon = self.monitor
        mon.state.update(active=True, channel='https://t.me/example', max_views=3, max_reactions=3,
                         subscribers_only=True, execute_plan=True)
        post = mon.add_post(self.message(1, 'Test'), self.now.isoformat())
        post['suggestion'] = automatic.validated_reaction_plan({'post_kind': 'ad'}, 3)
        with patch('automatic.utc_now', return_value=self.now):
            await mon.dispatch_actions()
        for service in ('views', 'reactions'):
            actions = post['execution'][service]['actions']
            self.assertEqual([action['account'] for action in actions], ['member'])
        action = post['execution']['views']['actions'][0]
        action['status'] = 'running'
        member_client = self.clients['member']
        for _ in range(2):
            self.assertEqual(list(await mon.subscriber_accounts(self.clients, 'example')), ['member'])
        self.assertEqual(member_client.channels, ['example'])
        # The allowlist survives restart and is reused for each action.
        self.clients['member'].participant = left
        restored = automatic.Monitor(mon.storage, lambda: self.clients)
        saved = restored.find_post(1)['execution']['views']['actions'][0]
        with patch('posts.ViewsJoiner._join', new=AsyncMock(return_value=('ok','просмотр'))) as perform:
            await restored.perform_action(1, 'views', saved, 'example')
        perform.assert_awaited_once()
        self.assertEqual(member_client.channels, ['example'])
        await restored.refresh_subscriber_roster('https://t.me/example', force=True)
        self.assertEqual(restored.snapshot()['subscriber_count'], 0)
        saved['status'] = 'running'
        with patch('posts.ViewsJoiner._join', new_callable=AsyncMock) as perform:
            await restored.perform_action(1, 'views', saved, 'example')
        perform.assert_not_awaited()
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(member_client.channels, ['example','example'])

    async def test_start_saves_confirmed_list_before_monitoring_begins(self):
        mon = self.monitor
        mon.state['subscribers_only'] = True
        with (patch.object(mon, 'latest_message', new=AsyncMock(return_value=SimpleNamespace(id=17))),
              patch.object(mon, 'loop', new=AsyncMock()),
              patch.object(mon, 'subscriber_accounts', new=AsyncMock(return_value={'2':self.clients['2']})) as scan):
            await mon.start(_JsonRequest({'channel':'https://t.me/example'}))
            await mon.task
        scan.assert_awaited_once()
        stored = json.loads(mon.storage.read_text(encoding='utf-8'))
        self.assertEqual(stored['subscriber_roster']['identities'], ['name:2'])
        self.assertEqual(stored['baseline_post_id'], 17)

    async def test_subscriber_limit_clamps_and_prompt_keeps_full_guidance(self):
        mon = self.monitor
        mon.state['channel'] = 'https://t.me/example'
        guidance = 'Начало\n' + 'абв'*1000 + '\nКонец'
        roster = dict(list(self.clients.items())[:50])
        with patch.object(mon, 'subscriber_accounts', new=AsyncMock(return_value=roster)) as scan:
            await mon.configure(_JsonRequest({'hours':automatic.DEFAULT_HOURS,'max_views':90,'max_reactions':10,
                                              'subscribers_only':True,'reaction_prompt':guidance}))
            await mon.configure(_JsonRequest({'hours':automatic.DEFAULT_HOURS,'max_views':90,'max_reactions':10}))
        scan.assert_awaited_once()
        restored = automatic.Monitor(mon.storage, lambda: self.clients)
        result = restored.snapshot()
        self.assertEqual(result['subscriber_count'], 50)
        self.assertEqual(result['max_views'], 50)
        self.assertNotIn('subscriber_roster', result)
        self.assertEqual(result['reaction_prompt'], guidance)
        response = await restored.preview_reaction_prompt(_JsonRequest({'poll':True}))
        prompt = json.loads(response.text)['prompt']
        self.assertIn(guidance, prompt)
        self.assertIn('poll_weights', prompt)
        self.assertIn('Распредели ровно 10 реакций', prompt)

    async def test_recent_membership_is_shared_across_concurrent_actions(self):
        mon = self.monitor
        client = self.clients['0']
        clock = 1000

        async def check(*args):
            await asyncio.sleep(0)
            return True, clock + automatic.MEMBERSHIP_ROSTER_TTL

        with (patch('automatic.monotonic', side_effect=lambda: clock),
              patch.object(mon.membership_requests, 'check', new_callable=AsyncMock, side_effect=check) as lookup):
            self.assertTrue(await mon.is_subscriber('0', client, 'example'))
            clock += automatic.MEMBERSHIP_ACTION_TTL
            confirmations = await asyncio.gather(
                mon.is_subscriber('0', client, 'example', before_action=True),
                mon.is_subscriber('0', client, 'example', before_action=True))
            self.assertEqual(confirmations, [True, True])
            self.assertEqual(lookup.await_count, 2)
            clock += 600
            self.assertTrue(await mon.is_subscriber('0', client, 'example'))
            self.assertEqual(lookup.await_count, 2)

    async def test_membership_flood_wait_applies_across_channels(self):
        class Client:
            count = 0

            async def get_input_entity(self, name):
                return types.InputChannel(1, 1)

            async def __call__(self, request):
                self.count += 1
                if self.count == 1:
                    raise FloodWaitError(request, capture=180)
                return SimpleNamespace(participant=types.ChannelParticipant(1, self.now))

        client = Client()
        client.now = self.now
        requests = automatic.MembershipRequests()
        clock = 1000
        with patch('automatic.monotonic', side_effect=lambda: clock):
            self.assertEqual(await requests.check('tg:1', client, 'first'), (False, 1180))
            clock = 1179
            self.assertEqual(await requests.check('tg:1', client, 'second'), (False, 1180))
            self.assertEqual(client.count, 1)
            clock = 1180
            self.assertTrue((await requests.check('tg:1', client, 'second'))[0])
            self.assertEqual(client.count, 2)

    async def test_no_membership_scan_after_all_queued_actions_finish(self):
        mon = self.monitor
        mon.state.update(active=True, channel='https://t.me/example', execute_plan=True,
                         subscribers_only=True, max_views=10)
        post = mon.add_post(self.message(1, 'Test'), self.now.isoformat())
        post['execution']['views'] = {'target': 1, 'actions': [{'status': 'done'}]}
        with (patch('automatic.utc_now', return_value=self.now),
              patch.object(mon, 'subscriber_accounts', new_callable=AsyncMock) as scan):
            await mon.dispatch_actions()
        scan.assert_not_awaited()

    async def test_ad_news_footer_and_reaction_vote(self):
        def plan(text, count, allowed):
            context = automatic.post_reaction_context(text)
            result = automatic.validated_reaction_plan(context, count, allowed)
            return result['post_kind'], {r['emoji']: r['count'] for r in result['reactions']}

        allowed = ['❤️', '👍', '💩', '😡']
        ad = 'Реклама: скидка на подписку'
        kind, allocation = plan(ad, 100, allowed)
        self.assertEqual(kind, 'ad')
        self.assertEqual(allocation['❤️'], 75)
        self.assertEqual(allocation['💩'] + allocation['😡'], 25)
        self.assertEqual(plan(ad, 4, ['❤️', '😡']), ('ad', {'❤️': 3, '😡': 1}))
        self.assertEqual(plan(ad, 1, allowed), ('ad', {'❤️': 1}))
        self.assertEqual(plan('Команда выиграла финал.\n\nРеклама: купите билеты.', 20, allowed)[0], 'news')
        self.assertEqual(plan(ad + '\n\nГолосуем: 👍 — да, 😡 — нет.', 4, allowed),
                         ('poll', {'❤️': 2, '👍': 1, '😡': 1}))
        self.assertEqual(plan(ad + '\n\nГолосуем: 👍 — да, 😡 — нет.', 3, ['❤️', '😡']),
                         ('poll', {'❤️': 2, '😡': 1}))

    async def test_falcons_poll_hearts_dominate_and_telegram_alias_is_used(self):
        text = ('🟢 **THORIN: СМЕНА IGL НЕ СПАСЁТ** 🟢 **FALCONS**\n\n'
                'По мнению аналитика, ни один из действующих капитанов не способен заметно '
                'улучшить результаты Falcons и заставить нынешний ростер работать.\n\n'
                'Что должно измениться в Falcons в первую очередь?\n❤️ — капитан\n🔥 — состав')
        mon = self.monitor
        mon.state.update(max_reactions=15)
        with patch('automatic.random.randint', return_value=15):
            post = mon.add_post(self.message(1, text), self.now.isoformat())
        with (patch.object(mon, 'channel_reactions', AsyncMock(return_value={'❤', '🔥'})),
              patch('automatic.env_value', return_value='')):
            await mon.analyze(1)
        suggestion = post['suggestion']
        self.assertEqual(suggestion['post_kind'], 'poll')
        self.assertEqual({r['emoji']: r['count'] for r in suggestion['reactions']}, {'❤️': 9, '🔥': 6})
        action = {'account': '0', 'emoji': '❤️', 'status': 'running'}
        with patch('posts.ReactionJoiner._join', AsyncMock(return_value=('ok', ''))) as send:
            await mon.perform_action(1, 'reactions', action, 'example')
        send.assert_awaited_once_with(self.clients['0'], 'post', ('example', 1), {'emoji': '❤'})

    async def test_bait_leaves_at_least_forty_percent_hearts_after_pool_cap(self):
        examples = [('Согласны? 👍👎', {'👍', '👎'}), ('😭 - такого не ожидали', {'😭'}),
                    ('Поставьте 😭 если расстроены', {'😭'})]
        for text, options in examples:
            context = automatic.post_reaction_context(text)
            self.assertEqual(set(context['poll_options']), options)
            result = automatic.validated_reaction_plan(context, 20, sorted(options | {'❤️'}))
            self.assertEqual({r['emoji']: r['count'] for r in result['reactions']}['❤️'], 8)
            self.assertEqual(automatic.reaction_allocation(6, result['proportions'])['❤️'], 3)

    async def test_random_reaction_budgets_vary_within_limit_and_survive_restart(self):
        mon = self.monitor
        mon.state.update(max_reactions=15)
        with patch('automatic.random.randint', side_effect=random.Random(41).randint):
            posts = [mon.add_post(self.message(i, 'Согласны? 👍👎'), self.now.isoformat()) for i in range(10)]
        budgets = [post['reaction_budget'] for post in posts]
        self.assertGreater(len(set(budgets)), 1)
        self.assertTrue(all(12 <= budget <= 15 for budget in budgets))
        mon.save()
        restored = automatic.Monitor(mon.storage, lambda: self.clients)
        self.assertEqual([post['reaction_budget'] for post in restored.state['posts']], budgets)
        with (patch.object(restored, 'channel_reactions', AsyncMock(return_value={'❤', '👍', '👎'})),
              patch('automatic.env_value', return_value='')):
            await restored.analyze(0)
        self.assertEqual(restored.find_post(0)['suggestion']['total'], budgets[0])

    async def test_dispatch_applies_heart_floor_at_the_smaller_executable_target(self):
        self.clients = {str(i): object() for i in range(6)}
        mon = self.monitor
        mon.state.update(active=True, execute_plan=True, max_reactions=20)
        post = mon.add_post(self.message(1, 'Согласны? 👍👎'), self.now.isoformat())
        post['suggestion'] = automatic.validated_reaction_plan(post['reaction_context'], 20)
        with patch('automatic.utc_now', return_value=self.now):
            await mon.dispatch_actions()
        actions = post['execution']['reactions']['actions']
        self.assertEqual(len(actions), 6)
        self.assertEqual(sum(a['emoji'] == '❤️' for a in actions), 3)

    async def test_persisted_targets_keep_their_previous_policy(self):
        mon = self.monitor
        mon.state.update(active=True, execute_plan=True, max_views=40, max_reactions=15)
        post = self.post(age=0)
        post['settings'] = mon.capture_settings()
        post['view_budget'] = {'base': 40, 'target': 48}
        post['suggestion'] = {'status': 'ok', 'policy_version': 2, 'total': 15,
                              'reactions': [{'emoji': '🔥', 'count': 15}]}
        mon.state['posts'] = [post]
        with patch('automatic.utc_now', return_value=self.now):
            await mon.dispatch_actions()
        self.assertEqual(post['execution']['views']['target'], 48)
        self.assertEqual(post['execution']['reactions']['target'], 15)
        self.assertEqual({a['emoji'] for a in post['execution']['reactions']['actions']}, {'🔥'})

    async def test_exact_reaction_target_and_dominant_preferred(self):
        for limit in [0,1,2,3,15,100]:
            for sentiment in ['neutral','sad','positive']:
                result=automatic.validated_reaction_plan({'sentiment':sentiment,'intensity':'low','reactions':[{'emoji':'🔥','count':999}]},limit)
                self.assertEqual(result['total'],limit)
                if limit:
                    self.assertIn(result['reactions'][0]['emoji'],['❤️','👍','😭'])
                    hearts = sum(r['count'] for r in result['reactions'] if r['emoji'] == '❤️')
                    self.assertGreaterEqual(hearts, (2 * limit + 4) // 5)
                    fire=sum(r['count'] for r in result['reactions'] if r['emoji']=='🔥')
                    self.assertLessEqual(fire,limit//10)

    async def test_reaction_allowlist_and_single_choice(self):
        for allowed in [['👍'],['😭','💔'],['🔥'],['👎','🤬']]:
            result=automatic.validated_reaction_plan({'reactions':[{'emoji':'❤️','count':100}]},15,allowed)
            self.assertEqual(result['total'],15)
            self.assertTrue(all(r['emoji'] in allowed for r in result['reactions']))

    async def test_view_target_persists_and_clamps_to_accounts(self):
        mon=self.monitor
        mon.state['max_views']=40
        post=self.post();mon.state['posts']=[post]
        with patch('automatic.random.randint',side_effect=lambda low,high:high) as choose:
            self.assertEqual(mon.view_target(post),44)
            self.assertEqual(mon.view_target(post),44)
            choose.assert_called_once_with(36,44)
        restored=automatic.Monitor(mon.storage,lambda:self.clients)
        self.assertEqual(restored.view_target(restored.state['posts'][0]),44)
        self.clients={str(i):object() for i in range(35)}
        self.assertEqual(mon.view_target(post),35)
        another=self.post(2)
        self.assertEqual(mon.view_target(another),35)
        self.clients={}
        empty=self.post(3)
        self.assertEqual(mon.view_target(empty),0)
        self.assertNotIn('view_budget',empty)

    async def test_samples_at_600_seconds_discovery_every_refresh(self):
        mon=self.monitor;post=self.post();post['post_text']=''
        post['samples']=[{'at':self.now.isoformat(),'views':10,'reactions':0}]
        mon.state.update(active=True,channel='https://t.me/example',last_post_id=1,posts=[post])
        mon.channel_entity=AsyncMock(return_value=object());mon.new_messages=AsyncMock(return_value=[])
        mon.sample_existing=AsyncMock()
        for seconds in [10,599]:
            with patch('automatic.utc_now',return_value=self.now+timedelta(seconds=seconds)):
                await mon.refresh()
            mon.sample_existing.assert_not_awaited()
        with patch('automatic.utc_now',return_value=self.now+timedelta(seconds=600)):
            await mon.refresh()
        mon.sample_existing.assert_awaited_once()
        self.assertEqual(mon.new_messages.await_count,3)
        self.assertEqual(automatic.DISCOVERY_SECONDS,10)
        self.assertEqual(automatic.POLL_SECONDS,600)

    async def test_visible_active_and_two_latest_completed_only(self):
        self.monitor.state['posts']=[self.post(i,25+i) for i in range(1,7)]+[self.post(7,1),self.post(8,2)]
        with patch('automatic.utc_now',return_value=self.now):snapshot=self.monitor.snapshot()
        self.assertEqual([p['post_id'] for p in snapshot['posts']],[8,7,6,5])
        self.assertEqual(len(self.monitor.state['posts']),8)

    async def test_pending_work_follows_reissued_session_without_repeating_done(self):
        self.clients={'old':object()}
        ids={'old':123,'new':123,'third':123}
        mon=self.monitor;mon.account_ids=lambda:ids
        post=self.post()
        mon.update_execution_plan(post,'reactions',1,[('👍',1)],self.now)
        original=self.clients['old'];self.clients={'new':original}
        mon.update_execution_plan(post,'reactions',1,[('👍',1)],self.now)
        actions=post['execution']['reactions']['actions']
        self.assertEqual(len(actions),1)
        self.assertEqual(actions[0]['account'],'new')
        actions[0]['status']='done'
        self.clients={'third':original}
        mon.update_execution_plan(post,'reactions',1,[('👍',1)],self.now)
        self.assertEqual(len(post['execution']['reactions']['actions']),1)
        self.assertEqual(post['execution']['reactions']['actions'][0]['status'],'done')

    async def test_per_channel_policy_persists_and_is_independent(self):
        hub=Hub(self.root,lambda:self.clients)
        a=hub._add_local('https://t.me/channel_a');b=hub._add_local('https://t.me/channel_b')
        for ident,link in [(a,'https://t.me/channel_a'),(b,'https://t.me/channel_b')]:hub.monitors[ident].state['channel']=link
        await hub.monitors[a].configure(_JsonRequest({'hours':automatic.DEFAULT_HOURS,'max_views':40,'max_reactions':15,'allowed_reactions':['👍','😭'],'reaction_prompt':'Channel A only'}))
        self.assertEqual(hub.monitors[b].state['reaction_prompt'],automatic.DEFAULT_PROMPT)
        restored=automatic.Monitor(hub.monitors[a].storage,lambda:self.clients)
        self.assertEqual(restored.state['allowed_reactions'],['👍','😭'])
        self.assertEqual(restored.state['reaction_prompt'],'Channel A only')
        for bad in [[],['not-emoji']]:
            with self.assertRaises(web.HTTPBadRequest):
                await restored.configure(_JsonRequest({'hours':automatic.DEFAULT_HOURS,'max_views':40,'max_reactions':15,'allowed_reactions':bad}))
        await hub.shutdown(None)

if __name__=='__main__':unittest.main()
