import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiohttp import web
from telethon import types
from telethon.errors import FloodWaitError
import automatic
from automatic_multi import Hub, _JsonRequest, channel_key


class AutopilotTests(unittest.IsolatedAsyncioTestCase):
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
        old = mon.add_post(self.message(1, 'Реклама: скидка на подписку'), self.now.isoformat())
        old_settings = mon.capture_settings()
        old_views = mon.view_target(old)
        await mon.configure(_JsonRequest({
            'hours': [0, 100] + [0] * 22, 'max_views': 0, 'max_reactions': 2,
            'execute_plan': False, 'views_enabled': False, 'subscribers_only': True,
            'allowed_reactions': ['💩'], 'reaction_prompt': 'Updated',
        }))
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
        entry = mon.membership_cache[('example', 'member')]
        entry.checked_at -= automatic.MEMBERSHIP_ACTION_TTL
        self.clients['member'].participant = left
        with patch('posts.ViewsJoiner._join', new_callable=AsyncMock) as perform:
            await mon.perform_action(1, 'views', action, 'example')
        perform.assert_not_awaited()
        self.assertEqual(action['status'], 'pending')
        self.assertEqual(self.clients['member'].channels, ['example', 'example'])
        self.assertEqual(await mon.subscriber_accounts(self.clients, 'example'), {})
        self.assertEqual(member_client.channels, ['example', 'example'])

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
                         ('poll', {'👍': 2, '😡': 2}))
        self.assertEqual(plan(ad + '\n\nГолосуем: 👍 — да, 😡 — нет.', 3, ['❤️', '😡']),
                         ('poll', {'😡': 3}))

    async def test_exact_reaction_target_and_dominant_preferred(self):
        for limit in [0,1,2,3,15,100]:
            for sentiment in ['neutral','sad','positive']:
                result=automatic.validated_reaction_plan({'sentiment':sentiment,'intensity':'low','reactions':[{'emoji':'🔥','count':999}]},limit)
                self.assertEqual(result['total'],limit)
                if limit:
                    self.assertIn(result['reactions'][0]['emoji'],['❤️','👍','😭'])
                    self.assertGreaterEqual(result['reactions'][0]['count'],__import__('math').ceil(limit*.65))
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
            self.assertEqual(mon.view_target(post),48)
            self.assertEqual(mon.view_target(post),48)
            choose.assert_called_once_with(32,48)
        restored=automatic.Monitor(mon.storage,lambda:self.clients)
        self.assertEqual(restored.view_target(restored.state['posts'][0]),48)
        self.clients={str(i):object() for i in range(35)}
        self.assertEqual(mon.view_target(post),35)
        another=self.post(2)
        self.assertTrue(32<=mon.view_target(another)<=35)
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
