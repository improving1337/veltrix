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
        self.assertEqual(plan['variant_count'],20)
        self.assertEqual(plan['strategy'],'target_30')
        self.assertTrue(plan['complete_coverage'])
        self.assertTrue(plan['peak_preserved'])
        self.assertGreater(Decimal(plan['highest_planned_coefficient']),Decimal('29.9'))
        self.assertGreater(len({len(i['legs']) for i in plan['items']}),1)
        self.assertEqual(len({i['account'] for i in plan['items']}),25)
        for i,item in enumerate(plan['items']):
            self.assertLessEqual(Decimal(item['coefficient']),30)
            self.assertLessEqual(Decimal(item['stake']),50000)
            self.assertEqual(item['variant'],i%plan['variant_count']+1)
            self.assertEqual(item['round'],i//plan['variant_count']+1)
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

    def test_peak_equals_exhaustive_best_coupon(self):
        plan=build_plan(matches(),accounts(),20)
        best=Decimal(0)
        for choices in itertools.product((-1,0,1),repeat=8):
            if sum(side>=0 for side in choices)<2:continue
            coefficient=Decimal(1)
            for match,side in zip(matches(),choices):
                if side>=0:coefficient*=Decimal(str(match['odds_a'] if side==0 else match['odds_b']))
            if coefficient<=30:best=max(best,coefficient)
        self.assertEqual(Decimal(plan['highest_possible_coefficient']),best)
        self.assertEqual(Decimal(plan['items'][0]['coefficient']),best)
        self.assertEqual(Decimal(plan['highest_planned_coefficient']),Decimal('29.9819520'))

    def test_42_accounts_target_peak_and_verified_scenario_floor(self):
        plan=build_plan(matches(),[{'name':str(i),'balance':50000} for i in range(42)],20)
        self.assertEqual(plan['best_largest_balance'],'1499097.60')
        self.assertGreaterEqual(Decimal(plan['worst_largest_balance']),Decimal('362406'))
        self.assertEqual(plan['outcome_count'],256)
        self.assertTrue(all(s['accounts']>=1 for s in plan['scenarios']))
        self.assertEqual(Decimal(plan['items'][0]['stake']),50000)

    def test_insufficient_budget_does_not_silently_drop_peak(self):
        with self.assertRaises(ValueError):
            build_plan(matches(),accounts(4),4)

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

    async def test_resume_after_restart_never_repeats_accepted_rows(self):
        sent=[]
        async def first(name,client,stake,picks,state):
            sent.append(name)
            if len(sent)==3:
                raise web.HTTPBadRequest(text=json.dumps({'error':'Коэффициент изменился: 2.0 → 2.1'}),content_type='application/json')
            return web.json_response({'ok':True})
        self.bulk.submit=first;plan=await self.plan()
        await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        self.assertIn('Коэффициент изменился',self.bulk.job['items'][2]['note'])
        accepted=[i['account'] for i in self.bulk.job['items'] if i['state']=='ok']
        submit=AsyncMock(return_value=web.json_response({'ok':True}))
        restored=BulkGame(self.temp.name,lambda:self.clients,self.state,self.fetch,submit)
        response=await restored.resume(Request({'job_id':plan['id']}))
        self.assertEqual(response.status,200)
        await restored.task
        self.assertTrue(all(c.args[0] not in accepted for c in submit.await_args_list))
        self.assertEqual(submit.await_count,6)
        self.assertTrue(all(i['state']=='ok' for i in restored.job['items']))
        await restored.close()

    async def test_resume_skips_unknown_results(self):
        async def ambiguous(name,client,stake,picks,state):
            state['on_send']();state['sent']=True;raise TimeoutError()
        self.bulk.submit=ambiguous;plan=await self.plan()
        await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        unknown=self.bulk.job['items'][0]['account']
        submit=AsyncMock(return_value=web.json_response({'ok':True}));self.bulk.submit=submit
        await self.bulk.resume(Request({'job_id':plan['id']}));await self.bulk.task
        self.assertEqual(self.bulk.job['items'][0]['state'],'unknown')
        self.assertFalse(any(c.args[0]==unknown for c in submit.await_args_list))
        self.assertEqual(submit.await_count,7)

    async def test_requote_requires_confirmation_and_preserves_accepted_and_stakes(self):
        self.bulk.submit=AsyncMock(side_effect=ValueError('before send'))
        plan=await self.plan();await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        # Simulate a previously accepted row from the saved job.
        self.bulk.job['items'][0]['state']='ok'
        accepted=json.loads(json.dumps(self.bulk.job['items'][0]))
        original=json.loads(json.dumps(self.bulk.job['items']))
        self.fetch.return_value=[{**m,'odds_a':2,'odds_b':2} for m in matches()]
        self.bulk.submit=AsyncMock(return_value=web.json_response({'ok':True}))
        response=await self.bulk.requote(Request({'job_id':plan['id']}));quote=json.loads(response.text)
        self.bulk.submit.assert_not_awaited()
        self.assertEqual(self.bulk.job['items'],original)
        for change in quote['changes']:
            old={(l['match_id'],l['side']) for l in change['before']}
            new={(l['match_id'],l['side']) for l in change['legs']}
            self.assertTrue(new.issubset(old))
            self.assertLessEqual(Decimal(change['coefficient']),30)
        response=await self.bulk.resume(Request({'job_id':plan['id'],'quote_id':quote['id']}))
        self.assertEqual(response.status,200)
        await self.bulk.task
        self.assertEqual(self.bulk.job['items'][0],accepted)
        for old,new in zip(original,self.bulk.job['items']):self.assertEqual(old['stake'],new['stake'])
        self.assertEqual(self.bulk.submit.await_count,7)

    async def test_invalid_or_expired_quote_does_not_resume(self):
        self.bulk.submit=AsyncMock(side_effect=ValueError('before send'))
        plan=await self.plan();await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        self.fetch.return_value=[{**m,'odds_a':10,'odds_b':10} for m in matches()]
        quote=json.loads((await self.bulk.requote(Request({'job_id':plan['id']}))).text)
        self.assertTrue(all(c.get('error') for c in quote['changes']))
        self.bulk.submit=AsyncMock()
        response=await self.bulk.resume(Request({'job_id':plan['id'],'quote_id':quote['id']}))
        self.assertEqual(response.status,409)
        self.bulk.submit.assert_not_awaited()
        self.bulk.quotes[quote['id']]['expires_at']=0
        response=await self.bulk.resume(Request({'job_id':plan['id'],'quote_id':quote['id']}))
        self.assertEqual(response.status,409)


    async def test_parallel_queue_continues_after_explicit_rejection(self):
        self.bulk.parallelism=3;self.bulk.continue_on_error=True
        active=0;maximum=0;sent=[]
        async def submit(name,client,stake,picks,state):
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active);sent.append(name)
            await asyncio.sleep(.005);active-=1
            if len(sent)==1:return web.json_response({'ok':False,'status':400,'error':'Отклонено: лимит','response':{'success':False}})
            return web.json_response({'ok':True})
        # Use account identity rather than completion order for deterministic rejection.
        async def guarded(name,client,stake,picks,state):
            result=await submit(name,client,stake,picks,state)
            if name=='acc00':return web.json_response({'ok':False,'status':400,'error':'Отклонено: лимит','response':{'success':False}})
            return result
        self.bulk.submit=guarded;plan=await self.plan();await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        self.assertEqual(maximum,3)
        self.assertEqual(len(sent),len(set(sent)))
        self.assertEqual(len(sent),8)
        self.assertEqual(self.bulk.job['items'][0]['state'],'rejected')
        self.assertEqual(sum(i['state']=='ok' for i in self.bulk.job['items']),7)
        self.assertNotIn('acc00',self.bulk.unknown_accounts)

    async def test_server_error_after_send_remains_unknown_without_retry(self):
        self.bulk.parallelism=3;self.bulk.continue_on_error=True
        async def failed(name,client,stake,picks,state):
            state['sent']=True;state['on_send']()
            return web.json_response({'ok':False,'status':500,'response':{'success':False,'error':'Internal error'}})
        self.bulk.submit=AsyncMock(side_effect=failed)
        plan=await self.plan();await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        self.assertEqual(self.bulk.submit.await_count,8)
        self.assertTrue(all(i['state']=='unknown' for i in self.bulk.job['items']))

    async def test_manual_confirmation_does_not_submit_again(self):
        async def ambiguous(name,client,stake,picks,state):
            state['sent']=True;state['on_send']();raise TimeoutError()
        self.bulk.submit=AsyncMock(side_effect=ambiguous);plan=await self.plan()
        await self.bulk.start(Request({'plan_id':plan['id']}));await self.bulk.task
        account=self.bulk.job['items'][0]['account'];calls=self.bulk.submit.await_count
        response=await self.bulk.confirm_manual(Request({'job_id':plan['id'],'account':account,'confirmed':False}))
        self.assertEqual(response.status,409)
        response=await self.bulk.confirm_manual(Request({'job_id':plan['id'],'account':account,'confirmed':True}))
        self.assertEqual(response.status,200)
        self.assertEqual(self.bulk.submit.await_count,calls)
        self.assertEqual(self.bulk.job['items'][0]['state'],'ok')
        self.assertTrue(self.bulk.job['items'][0]['manual_verified'])
        self.assertNotIn(account,self.bulk.unknown_accounts)



class LiveLimitValidationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,stake,odds=2,balance=100000,seen=None,bulk=False):
        calls=[]
        async def api(name,client,method,path,payload=None):
            calls.append((method,path,payload))
            if path=='/api/profile':return 200,{'success':True,'data':{'user':{'balance':balance}}}
            if path=='/api/matches/active':return 200,{'success':True,'data':{'matches':[{'id':1,'status':'betting_open'},{'id':2,'status':'betting_open'}]}}
            if '/detail' in path:return 200,{'success':True,'data':{}}
            return 200,{'success':True}
        with patch('cs3bet.call',side_effect=api),patch('cs3bet.login',new=AsyncMock()),patch('cs3bet.set_balance'),patch('cs3bet.extract_coefficients',return_value=(Decimal(str(odds)),Decimal(str(odds)))):
            if bulk:
                with patch('cs3bet.BulkGame') as factory:
                    cs3bet.make_handlers(lambda:{'test':object()},web.Application())
                submit=factory.call_args.args[4]
                state={'sent':False}
                try:
                    response=await submit('test',object(),Decimal(stake),[(i,'1',Decimal(str(seen))) for i in (1,2)],state)
                    await submit('another',object(),Decimal(stake),[(i,'1',Decimal(str(seen))) for i in (1,2)],{'sent':False})
                except web.HTTPException as exc:response=exc
                return response,calls
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

    async def test_bulk_accepts_current_prices_and_coalesces_market_reads(self):
        response,calls=await self.exercise('1000',odds=2,seen=2.05,bulk=True)
        self.assertEqual(response.status,200)
        self.assertEqual(json.loads(response.text)['coefficient'],4)
        self.assertEqual(sum(path=='/api/matches/active' for _,path,_ in calls),1)
        response,calls=await self.exercise('1000',odds=6,seen=2.05,bulk=True)
        self.assertEqual(response.status,400)
        self.assertFalse(any(method=='POST' for method,_,_ in calls))

if __name__=='__main__':unittest.main()
