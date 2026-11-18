import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from aiohttp import web
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

    async def test_caption_options_override_generic_sentiment(self):
        data = {
            'sentiment': 'positive',
            'reactions': [
                {'emoji': '🔥', 'count': 100},
                {'emoji': '😭', 'count': 80},
                {'emoji': '❤️', 'count': 20},
            ],
        }
        caption = 'Игрок поставил рекорд\n❤️ — Лучший!\n😭 — Лучше бы MVP'
        with patch('automatic.random.randint', return_value=20):
            result = automatic.validated_reaction_plan(data, 20, post_text=caption)
        self.assertEqual(result['reactions'], [{'emoji': '😭', 'count': 16}, {'emoji': '❤️', 'count': 4}])

    async def test_caption_question_and_single_option(self):
        self.assertEqual(list(automatic.explicit_reaction_choices('Согласны? 👍 / 👎')), ['👍', '👎'])
        self.assertEqual(automatic.explicit_reaction_choices('😊 Обычный заголовок'), {})
        result = automatic.validated_reaction_plan({}, 1, post_text='Он получил стикеры!\n🎈 — красава')
        self.assertEqual(result['reactions'], [{'emoji': '🎈', 'count': 1}])

    async def test_poll_labels_and_no_invented_votes(self):
        caption = '❤️ — Лучший!\n😭 — Лучше бы MVP'
        self.assertEqual(automatic.explicit_reaction_choices(caption),
                         {'❤️': 'Лучший!', '😭': 'Лучше бы MVP'})
        data = {'sentiment': 'sad', 'reactions': [{'emoji': '🔥', 'count': 100}]}
        self.assertEqual(automatic.validated_reaction_plan(data, 10, post_text=caption)['status'],
                         'unavailable')
        data['reactions'].append({'emoji': '❤️', 'count': 100})
        result = automatic.validated_reaction_plan(data, 1, post_text=caption)
        self.assertEqual(result['reactions'], [{'emoji': '❤️', 'count': 1}])
        self.assertEqual(automatic.validated_reaction_plan(data, 10, ['🔥'], caption)['status'],
                         'unavailable')

    async def test_contextual_weights_and_sentiment_fallback(self):
        data = {'sentiment': 'critical', 'reactions': [{'emoji': '👎', 'count': 4}, {'emoji': '👍', 'count': 1}]}
        with patch('automatic.random.randint', return_value=10):
            result = automatic.validated_reaction_plan(data, 10)
        self.assertEqual(result['reactions'], [{'emoji': '👎', 'count': 8}, {'emoji': '👍', 'count': 2}])
        self.assertEqual(automatic.validated_reaction_plan({'sentiment': 'sad'}, 1)['reactions'],
                         [{'emoji': '😭', 'count': 1}])

    async def test_reaction_target_uses_both_bounds(self):
        with patch('automatic.random.randint', side_effect=[12, 15]) as choose:
            self.assertEqual(automatic.validated_reaction_plan({}, 15)['total'], 12)
            self.assertEqual(automatic.validated_reaction_plan({}, 15)['total'], 15)
            self.assertEqual([call.args for call in choose.call_args_list], [(12, 15), (12, 15)])

    async def test_reaction_allowlist_and_single_choice(self):
        for allowed in [['👍'],['😭','💔'],['🔥'],['👎','🤬']]:
            result=automatic.validated_reaction_plan({'reactions':[{'emoji':'❤️','count':100}]},15,allowed)
            self.assertLessEqual(result['total'],15)
            self.assertGreaterEqual(result['total'],12)
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
