import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from session_admin import SessionAdmin
from account_security import AccountSecurity
from test_account_deploy import session_bytes


class Request:
    def __init__(self, data): self.data=data
    async def json(self): return self.data


class SessionAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        for folder in ['Account','Session','temp']:(self.root/folder).mkdir()
        self.source=NS(name='imported',me=NS(id=123,phone='79991234567'))
        self.source.client=NS(session=NS(auth_key=NS(key=b'old')),get_me=AsyncMock(return_value=self.source.me))
        async def messages(*args,**kwargs):
            if kwargs.get('min_id') is not None:
                return [NS(id=2,date=datetime.now(timezone.utc),out=False,message='Login code: 12345. Do not share.')]
            return [NS(id=1)]
        self.source.client.get_messages=messages
        self.accounts={'imported':self.source}
        self.security=AccountSecurity(self.root,lambda:self.accounts)
        self.new=None
        def factory(path,*args,**kwargs):
            Path(path+'.session').write_bytes(session_bytes())
            self.new=NS(session=NS(auth_key=NS(key=b'new'),close=lambda:None),connect=AsyncMock(),disconnect=AsyncMock(),send_code_request=AsyncMock(return_value=NS(phone_code_hash='hash')),sign_in=AsyncMock(),get_me=AsyncMock(return_value=self.source.me))
            return self.new
        async def connect():
            for file in (self.root/'Account').glob('*.json'):
                self.accounts[file.stem]=NS(name=file.stem,me=self.source.me,client=self.new)
        async def retire(source):self.accounts.pop(source.name)
        self.retire=AsyncMock(side_effect=retire)
        self.manager=SessionAdmin(self.root,lambda:self.accounts,self.security,connect,self.retire,factory)

    async def asyncTearDown(self):
        await self.manager.close();self.temp.cleanup()

    async def test_reissue_preserves_identity_and_marks_new_key(self):
        await self.manager.replace(self.source)
        self.retire.assert_awaited_once_with(self.source)
        new=next(iter(self.accounts.values()))
        self.assertTrue(self.manager.owned(new))
        self.assertFalse(self.manager.owned(self.source))
        self.new.sign_in.assert_awaited_once_with(phone='79991234567',code='12345',phone_code_hash='hash')
        self.assertEqual(self.security.suspended,set())
        restarted=SessionAdmin(self.root,lambda:self.accounts,self.security,None,None)
        self.assertTrue(restarted.owned(new))
        new.client.session.auth_key.key=b'replaced-external-key'
        self.assertFalse(restarted.owned(new))

    async def test_different_account_does_not_retire_source(self):
        factory=self.manager.factory
        def different(*args,**kwargs):
            result=factory(*args,**kwargs)
            result.get_me=AsyncMock(return_value=NS(id=999))
            return result
        self.manager.factory=different
        with self.assertRaises(RuntimeError):await self.manager.replace(self.source)
        self.retire.assert_not_awaited()
        self.assertIn('imported',self.accounts)
        self.assertEqual(list((self.root/'Account').glob('*.json')),[])
        self.assertEqual(self.security.suspended,set())

    async def test_unknown_2fa_waits_without_retiring(self):
        from telethon.errors import SessionPasswordNeededError
        factory=self.manager.factory
        def protected(*args,**kwargs):
            result=factory(*args,**kwargs)
            result.sign_in.side_effect=SessionPasswordNeededError(None)
            return result
        self.manager.factory=protected
        await self.manager.replace(self.source)
        self.assertIn('imported',self.manager.pending)
        self.assertIn('imported',self.security.suspended)
        self.retire.assert_not_awaited()
        self.new.sign_in.side_effect=None
        response=await self.manager.password(Request({'name':'imported','password':'current-password'}))
        self.assertEqual(response.status,200)
        self.retire.assert_awaited_once()
        self.assertEqual(self.security.suspended,set())

    async def test_single_selection_never_processes_other_accounts(self):
        other=NS(name='other',me=NS(id=456),client=self.source.client)
        self.accounts['other']=other
        self.manager.replace=AsyncMock()
        response=await self.manager.start_one(Request({'name':'other'}))
        self.assertEqual(response.status,200)
        await self.manager.task
        self.manager.replace.assert_awaited_once_with(other)
        self.assertEqual(self.manager.job['account'],'other')

    async def test_invalid_selection_never_falls_back_to_bulk(self):
        for data,status in [({'name':'missing'},404),({'name':''},400),({'name':None},400),([],400)]:
            response=await self.manager.start(Request(data))
            self.assertEqual(response.status,status)
            self.assertIsNone(self.manager.task)

    async def test_single_endpoint_requires_explicit_account(self):
        response=await self.manager.start_one(Request({}))
        self.assertEqual(response.status,400)
        self.assertIsNone(self.manager.task)

    async def test_bulk_still_processes_all_external_accounts(self):
        other=NS(name='other',me=NS(id=456),client=self.source.client)
        self.accounts['other']=other
        self.manager.replace=AsyncMock()
        response=await self.manager.start(Request({}))
        self.assertEqual(response.status,200)
        await self.manager.task
        self.assertEqual([call.args[0].name for call in self.manager.replace.await_args_list],['imported','other'])

    async def test_parallel_reissue_rejected(self):
        import asyncio
        blocker=asyncio.Event()
        self.manager.replace=AsyncMock(side_effect=lambda source: None)
        async def wait(source):await blocker.wait()
        self.manager.replace.side_effect=wait
        await self.manager.start(Request({'name':'imported'}))
        response=await self.manager.start(Request({'name':'imported'}))
        self.assertEqual(response.status,409)
        blocker.set()
        await self.manager.task

if __name__=='__main__':unittest.main()
