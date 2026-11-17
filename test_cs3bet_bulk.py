import asyncio
import itertools
import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from aiohttp import web
import cs3bet
from cs3bet_bulk import build_plan, BulkGame, MAX_STAKE

PAIRS=[(1.52,2.40),(1.40,2.80),(1.12,5.50),(1.65,2.15),(1.45,2.60),(1.70,2.05),(1.45,2.60),(1.70,2.05)]
def matches():return [{'id':i+1,'team_a':f'A{i}','team_b':f'B{i}','odds_a':a,'odds_b':b,'status':'betting_open'} for i,(a,b) in enumerate(PAIRS)]
def accounts(n=25):return [{'name':f'acc{i:02}','balance':50000-i*1000} for i in range(n)]
class Request:
    def __init__(self,data):self.data=data;self.match_info={'acc':'test'}
    async def json(self):return self.data


class PlannerTests(unittest.TestCase):
    def test_screenshot_full_coverage_under_caps_and_every_account_once(self):
        plan=build_plan(matches(),accounts(),20)
        self.assertEqual(plan['variant_count'],16)
        self.assertEqual(len(plan['selected_matches']),4)
        self.assertEqual(len({i['account'] for i in plan['items']}),25)
        for i,item in enumerate(plan['items']):
            self.assertLessEqual(Decimal(item['coefficient']),30)
            self.assertLessEqual(Decimal(item['stake']),50000)
            self.assertEqual(item['variant'],i%16+1)
            self.assertEqual(item['round'],i//16+1)
            self.assertEqual(len({leg['match_id'] for leg in item['legs']}),len(item['legs']))
        maxima,totals=[],[]
        for outcome in itertools.product(('1','2'),repeat=8):
            finals=[];winners=0
            for item in plan['items']:
                win=all(outcome[leg['match_id']-1]==leg['side'] for leg in item['legs'])
                winners+=win
                finals.append(Decimal(item['remaining'])+(Decimal(item['potential']) if win else 0))
            self.assertGreaterEqual(winners,1)
            maxima.append(max(finals));totals.append(sum(finals))
        self.assertEqual(min(maxima),Decimal(plan['worst_largest_balance']))
        self.assertEqual(max(maxima),Decimal(plan['best_largest_balance']))
        self.assertEqual(min(totals),Decimal(plan['worst_total_balance']))

    def test_cap_leaves_funds_and_excludes_unknown_small_duplicate(self):
        pool=accounts(6)+[{'name':'huge','balance':100000},{'name':'unknown','balance':None},{'name':'tiny','balance':99}]
        pool[0]['identity']='same';pool.append({'name':'alias','balance':50000,'identity':'same'})
        plan=build_plan(matches(),pool)
        self.assertEqual(plan['items'][0]['account'],'huge')
        self.assertEqual(plan['items'][0]['remaining'],'50000.00')
        self.assertEqual(len(plan['skipped_accounts']),3)
        self.assertNotIn('alias',[i['account'] for i in plan['items']])

    def test_infeasible_and_bad_input_rejected(self):
        bad=[{**m,'odds_a':10,'odds_b':11} for m in matches()[:2]]
        with self.assertRaises(ValueError):build_plan(bad,accounts())
        with self.assertRaises(ValueError):build_plan(matches(),accounts(3))
        for count in [0,3,65,True,20.5]:
            with self.assertRaises(ValueError):build_plan(matches(),accounts(),count)
        with self.assertRaises(ValueError):build_plan(matches()+matches()[:1],accounts())
        with self.assertRaises(ValueError):build_plan([{**matches()[0],'id':i} for i in range(13)],accounts())


class BulkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.clients={a['name']:object() for a in accounts(8)}
        self.state={a['name']:{'balance':a['balance'],'balance_ts':time.time()} for a in accounts(8)}
        self.submit=AsyncMock(return_value=web.json_response({'ok':True}))
        self.fetch=AsyncMock(return_value=matches())
        self.bulk=BulkGame(self.temp.name,lambda:self.clients,self.state,self.fetch,self.submit)
    async def asyncTearDown(self):await self.bulk.close();self.temp.cleanup()
    async def plan(self):
        response=await self.bulk.preview(Request({'max_variants':20}))
        self.assertEqual(response.status,200,response.text)
        return json.loads(response.text)
    async def test_preview_is_read_only_then_one_shot_start(self):
        plan=await self.plan();self.submit.assert_not_awaited()
        await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        self.assertEqual(self.submit.await_count,8)
        response=await self.bulk.start(Request({'plan_id':plan['id']}))
        self.assertEqual(response.status,409)
        self.assertEqual(self.submit.await_count,8)
    async def test_ambiguous_send_stops_and_remains_blocked_after_restart(self):
        async def ambiguous(name,client,stake,picks,state):
            state['on_send']();state['sent']=True;raise TimeoutError()
        self.bulk.submit=ambiguous;plan=await self.plan()
        await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        self.assertEqual(self.bulk.job['items'][0]['state'],'unknown')
        self.assertTrue(all(i['state']=='not_run' for i in self.bulk.job['items'][1:]))
        restored=BulkGame(self.temp.name,lambda:self.clients,self.state,self.fetch,self.submit)
        self.assertIn(plan['items'][0]['account'],restored.unknown_accounts)
        response=await restored.preview(Request({}));new=json.loads(response.text)
        self.assertNotIn(plan['items'][0]['account'],[i['account'] for i in new['items']])
        await restored.close()
    async def test_preflight_failure_sends_nothing_further(self):
        self.bulk.submit=AsyncMock(side_effect=ValueError('odds changed'))
        plan=await self.plan();await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        self.bulk.submit.assert_awaited_once()
        self.assertEqual(self.bulk.job['items'][0]['state'],'error')
    async def test_stop_preserves_current_and_skips_remaining(self):
        async def submit(*args):
            self.bulk.job['stop_requested']=True
            return web.json_response({'ok':True})
        self.bulk.submit=submit;plan=await self.plan();await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        self.assertEqual(sum(i['state']=='ok' for i in self.bulk.job['items']),1)
        self.assertEqual(sum(i['state']=='not_run' for i in self.bulk.job['items']),7)
    async def test_expired_plan_cannot_start(self):
        plan=await self.plan();self.bulk.plans[plan['id']]['expires_at']=0
        response=await self.bulk.start(Request({'plan_id':plan['id']}))
        self.assertEqual(response.status,409);self.submit.assert_not_awaited()


class LiveLimitValidationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,stake,odds=2,balance=100000,seen=None):
        calls=[]
        async def api(name,client,method,path,payload=None):
            calls.append((method,path,payload))
            if path=='/api/profile':return 200,{'success':True,'data':{'user':{'balance':balance}}}
            if path=='/api/matches/active':return 200,{'success':True,'data':{'matches':[{'id':1,'status':'betting_open'},{'id':2,'status':'betting_open'}]}}
            if '/detail' in path:return 200,{'success':True,'data':{}}
            return 200,{'success':True}
        with patch('cs3bet.call',side_effect=api),patch('cs3bet.login',new=AsyncMock()),patch('cs3bet.set_balance'),patch('cs3bet.extract_coefficients',return_value=(Decimal(str(odds)),Decimal(str(odds)))):
            bet=cs3bet.make_handlers(lambda:{'test':object()})[3]
            try:
                response=await bet(Request({'stake':stake,'legs':[{'match_id':i,'side':'1','odds':seen} for i in [1,2]]}))
            except web.HTTPException as exc:response=exc
        return response,calls
    async def test_server_limits_even_with_forged_ui_request(self):
        for stake,odds,balance in [('50001',2,100000),('NaN',2,100000),('10000',2,500),('100',6,100000)]:
            response,calls=await self.exercise(stake,odds,balance)
            self.assertEqual(response.status,400)
            self.assertFalse(any(method=='POST' for method,_,_ in calls))
    async def test_all_capped_and_fresh_odds_change_stops(self):
        response,calls=await self.exercise('all')
        self.assertEqual(response.status,200)
        self.assertEqual(json.loads(response.text)['stake'],50000)
        self.assertEqual(sum(method=='POST' for method,_,_ in calls),1)
        response,calls=await self.exercise('1000',odds=3,seen=2)
        self.assertEqual(response.status,400)
        self.assertFalse(any(method=='POST' for method,_,_ in calls))

if __name__=='__main__':unittest.main()
