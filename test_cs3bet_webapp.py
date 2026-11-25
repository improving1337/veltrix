import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlencode
from unittest.mock import AsyncMock, Mock, patch
from telethon import types
import cs3bet


def url(identity, start_param=None):
    fields={'user':json.dumps({'id':identity}),'auth_date':'123','hash':'test-signature'}
    if start_param:fields['start_param']=start_param
    data=urlencode(fields)
    return 'https://game.example/app#'+urlencode({'tgWebAppData':data,'tgWebAppPlatform':'web'})


class WebappTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.file=Path(self.temp.name)/'referrals.json'
        self.client=type('Client',(),{'_self_id':123,'get_entity':AsyncMock(return_value=NS(id=99, bot=True, deleted=False)),
                                     '__call__':AsyncMock(return_value=NS(url=url(123)))})()
        async def launch(request):return NS(url=url(123,getattr(request,'start_param',None)))
        self.client.__call__.side_effect=launch
        self.api=AsyncMock(return_value=(200,{'success':True,'data':{'access_token':'test-token'}}))
        self.patches=[patch('cs3bet.REFERRALS_FILE',self.file),patch('cs3bet._tokens',{}),
                      patch('cs3bet._login_locks',{}),patch('cs3bet.STATE',{}),
                      patch('cs3bet.raw_request',self.api),patch('presence.ensure_connected',new=AsyncMock())]
        for p in self.patches:p.start()
    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    async def test_first_login_uses_referral_and_alias_does_not_repeat(self):
        token=await cs3bet.login('first',self.client)
        self.assertEqual(token,'test-token')
        self.assertEqual(self.client.__call__.await_args.args[0].start_param,'ref_6J74EAKP')
        records=json.loads(self.file.read_text())
        self.assertEqual(records['123']['start_param'],'ref_6J74EAKP')
        from urllib.parse import parse_qs
        sent=self.api.await_args.kwargs['payload']['init_data']
        self.assertEqual(parse_qs(sent)['start_param'],['ref_6J74EAKP'])
        await cs3bet.login('new-session-name',self.client)
        self.assertIsNone(self.client.__call__.await_args.args[0].start_param)
        self.assertNotIn('access_token',self.file.read_text())
        self.assertNotIn('tgWebAppData',self.file.read_text())

    async def test_existing_balance_does_not_attach_referral(self):
        cs3bet.STATE['existing']={'balance':0}
        await cs3bet.login('existing',self.client)
        self.assertIsNone(self.client.__call__.await_args.args[0].start_param)

    async def test_login_can_recover_bot_through_its_own_session(self):
        bot = types.User(99, bot=True, username=cs3bet.BOT_USERNAME)
        peer = types.InputPeerUser(99, 1234)
        self.client.session = NS(get_input_entity=Mock(return_value=peer))
        self.client.get_entity.side_effect = [ValueError(f'No user has "{cs3bet.BOT_USERNAME}" as username'), bot]
        self.assertEqual(await cs3bet.login('existing', self.client), 'test-token')
        self.assertEqual(self.client.get_entity.await_args_list[-1].args, (peer,))
        self.assertIs(self.client.__call__.await_args.args[0].bot, bot)

    async def test_failed_auth_does_not_mark_registration(self):
        self.api.return_value=(500,{'success':False})
        with self.assertRaises(RuntimeError):await cs3bet.login('new',self.client)
        self.assertFalse(self.file.exists())
        self.assertNotIn('new',cs3bet._tokens)

    async def test_wrong_signed_user_never_sent_to_game_api(self):
        self.client.__call__.side_effect=None
        self.client.__call__.return_value=NS(url=url(456))
        with self.assertRaises(RuntimeError):await cs3bet.login('new',self.client)
        self.api.assert_not_awaited()
        self.assertFalse(self.file.exists())

    async def test_open_returns_verified_account_link(self):
        with patch('cs3bet.login',new=AsyncMock()):
            link,identity=await cs3bet.miniapp_url('account',self.client)
        self.assertEqual(link,url(123));self.assertEqual(identity,123)
        self.client.__call__.side_effect=None
        self.client.__call__.return_value=NS(url=url(456))
        with patch('cs3bet.login',new=AsyncMock()):
            with self.assertRaises(RuntimeError):await cs3bet.miniapp_url('account',self.client)

    async def test_missing_referral_does_not_register_new_account(self):
        self.client.__call__.side_effect=None
        self.client.__call__.return_value=NS(url=url(123))
        with self.assertRaises(RuntimeError):await cs3bet.login('new',self.client)
        self.api.assert_not_awaited()

    async def test_missing_user_and_insecure_url_are_rejected(self):
        for link in ['https://game.example/#tgWebAppData=invalid','http://game.example/#anything','javascript:alert(1)']:
            with self.assertRaises(RuntimeError):cs3bet.webview_identity(link)

if __name__=='__main__':unittest.main()
