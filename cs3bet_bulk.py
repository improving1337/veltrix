"""Bounded, exhaustive scenario planner for non-monetary CS3BET game points."""
import asyncio
import itertools
import json
import math
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from aiohttp import web
from account_security import private_json, read_json

MAX_STAKE = Decimal('50000')
MAX_ODDS = Decimal('30')
MIN_STAKE = Decimal('100')
CENT = Decimal('.01')


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Некорректное число')
    if not result.is_finite():
        raise ValueError('Некорректное число')
    return result


def payout(value):
    return value.quantize(CENT, rounding=ROUND_DOWN)


def build_plan(matches, accounts, max_variants=20):
    if isinstance(max_variants, bool) or not isinstance(max_variants, int) or not 4 <= max_variants <= 64:
        raise ValueError('Число вариантов: целое от 4 до 64')
    pool, skipped, seen_accounts = [], [], set()
    for account in accounts:
        name = str(account['name'])
        identity = str(account.get('identity') or name)
        if identity in seen_accounts:
            skipped.append({'account': name, 'reason': 'Другая сессия того же Telegram-аккаунта'})
            continue
        seen_accounts.add(identity)
        try:
            balance = payout(amount(account['balance']))
        except (KeyError, ValueError):
            skipped.append({'account': name, 'reason': 'Баланс неизвестен'})
            continue
        if balance < MIN_STAKE:
            skipped.append({'account': name, 'reason': 'Меньше минимальной ставки 100'})
            continue
        pool.append({**account, 'name': name, 'balance': balance, 'stake': min(balance, MAX_STAKE)})
    pool.sort(key=lambda a: (-a['balance'], a['name']))
    if len(pool) < 4:
        raise ValueError('Для полного покрытия экспрессов нужны хотя бы 4 аккаунта с балансом от 100')
    if len(pool) > 1000:
        raise ValueError('За один план поддерживается до 1000 аккаунтов')
    eligible, ignored, seen_matches = [], [], set()
    for match in matches:
        mid = int(match['id'])
        if mid in seen_matches:
            raise ValueError('Матч повторяется')
        seen_matches.add(mid)
        try:
            a, b = amount(match['odds_a']), amount(match['odds_b'])
        except (KeyError, ValueError):
            ignored.append(mid);continue
        if match.get('status') != 'betting_open' or min(a,b) <= 1 or max(a,b) > MAX_ODDS:
            ignored.append(mid);continue
        eligible.append({**match, 'id': mid, 'odds': (a,b)})
    if len(eligible) > 12:
        raise ValueError('Выберите до 12 открытых матчей для точного перебора')
    if len(eligible) < 2:
        raise ValueError('Нужны хотя бы 2 открытых матча с двумя корректными коэффициентами')
    best = None
    limit = min(len(eligible), int(math.log2(min(len(pool), max_variants))))
    for size in range(2, limit+1):
        for subset in itertools.combinations(eligible, size):
            worst = math.prod(max(m['odds']) for m in subset)
            if worst > MAX_ODDS:
                continue  # All possible binary outcomes must meet the cap, not just favourites.
            variants = []
            for sides in itertools.product((0,1), repeat=size):
                coef = math.prod(m['odds'][side] for m,side in zip(subset,sides))
                variants.append((coef,sides))
            variants.sort()
            # Largest funds go to the lowest multiplier in the first full coverage cycle.
            floor = max(max(a['balance']-a['stake'] for a in pool),
                        min(a['balance']-a['stake']+payout(a['stake']*coef)
                        for a,(coef,_) in zip(pool,variants)))
            peak = max(a['balance']-a['stake']+payout(a['stake']*coef)
                       for a,(coef,_) in zip(pool,variants))
            score = (floor, peak, size, tuple(-m['id'] for m in subset))
            if best is None or score > best[0]:
                best = (score, subset, variants)
    if best is None:
        raise ValueError('Нельзя полностью покрыть даже два матча при лимите ×30: выберите другой набор')
    _, subset, variants = best
    items = []
    for index, account in enumerate(pool):
        variant = index % len(variants)
        coef,sides = variants[variant]
        legs=[{'match_id':m['id'],'side':str(side+1),'odds':str(m['odds'][side]),
               'team_a':m.get('team_a',''),'team_b':m.get('team_b','')} for m,side in zip(subset,sides)]
        win = payout(account['stake']*coef)
        items.append({'account':account['name'],'variant':variant+1,'round':index//len(variants)+1,
                      'balance':str(account['balance']),'balance_ts':account.get('balance_ts'),
                      'stake':str(account['stake']),'remaining':str(account['balance']-account['stake']),
                      'coefficient':str(coef),'potential':str(win),
                      'winner_balance':str(account['balance']-account['stake']+win),'legs':legs})
    reserve = sum((amount(i['remaining']) for i in items), Decimal(0))
    largest_reserve = max(amount(i['remaining']) for i in items)
    scenarios = []
    for index,(coef,_) in enumerate(variants,1):
        winners = [i for i in items if i['variant']==index]
        scenarios.append({'variant':index,'accounts':len(winners),'coefficient':str(coef),
                          'largest_balance':str(max(largest_reserve,max(amount(i['winner_balance']) for i in winners))),
                          'total_balance':str(reserve+sum((amount(i['potential']) for i in winners),Decimal(0)))})
    return {'items':items,'scenarios':scenarios,'variant_count':len(variants),
            'selected_matches':[m['id'] for m in subset],
            'unused_matches':[m['id'] for m in eligible if m not in subset], 'ignored_matches':ignored,
            'skipped_accounts':skipped,'account_count':len(items),
            'initial_total':str(sum((amount(i['balance']) for i in items),Decimal(0))),
            'total_stake':str(sum((amount(i['stake']) for i in items),Decimal(0))),
            'reserve':str(reserve),
            'worst_largest_balance':str(min(amount(i['largest_balance']) for i in scenarios)),
            'best_largest_balance':str(max(amount(i['largest_balance']) for i in scenarios)),
            'worst_total_balance':str(min(amount(i['total_balance']) for i in scenarios)),
            'best_total_balance':str(max(amount(i['total_balance']) for i in scenarios))}


class BulkGame:
    def __init__(self, root, clients, state, fetch_matches, submit, identities=None):
        self.clients, self.state = clients, state
        self.fetch_matches, self.submit = fetch_matches, submit
        self.identities = identities or (lambda:{})
        self.path = Path(root)/'cs3bet_bulk_job.json'
        self.unknown_path = Path(root)/'cs3bet_bulk_unknown.json'
        self.unknown_accounts = set(read_json(self.unknown_path).get('accounts', []))
        self.job = read_json(self.path) or None
        if self.job:
            self.unknown_accounts.update(i['account'] for i in self.job.get('items', []) if i.get('state')=='unknown')
        if self.job and self.job.get('running'):
            self.job['running']=False;self.job['interrupted']=True
            for item in self.job.get('items',[]):
                if item['state']=='sending':item['state']='unknown'
                elif item['state'] in ('checking','queue'):item['state']='not_run'
            self.save()
        self.plans={};self.task=None;self.lock=asyncio.Lock()

    def save(self):
        if self.job:
            self.unknown_accounts.update(i['account'] for i in self.job.get('items',[]) if i.get('state')=='unknown')
            private_json(self.unknown_path, {'accounts': sorted(self.unknown_accounts)})
            private_json(self.path,self.job)

    async def preview(self, request):
        if self.task and not self.task.done():
            return web.json_response({'error':'Дождитесь завершения текущего плана'},status=409)
        data=await request.json()
        if not isinstance(data,dict):raise web.HTTPBadRequest(text='Expected object')
        names=data.get('accounts')
        clients={n:c for n,c in self.clients().items() if c and (not hasattr(c,'is_connected') or c.is_connected())}
        if names is not None:
            if not isinstance(names,list) or not all(isinstance(n,str) and n in clients for n in names):
                raise web.HTTPBadRequest(text='Unknown account selection')
            clients={n:clients[n] for n in dict.fromkeys(names)}
        if not clients:return web.json_response({'error':'Нет подключённых аккаунтов'},status=400)
        identities=self.identities()
        blocked=set(self.unknown_accounts)
        accounts=[{'name':n,'identity':identities.get(n,n),'balance':self.state.get(n,{}).get('balance'),
                   'balance_ts':self.state.get(n,{}).get('balance_ts')} for n in clients if n not in blocked]
        source=max(clients,key=lambda n: float(self.state.get(n,{}).get('balance') or 0))
        try:
            matches=await self.fetch_matches(source,clients[source])
            selected=data.get('match_ids')
            if selected is not None:
                if not isinstance(selected,list) or not selected:
                    raise ValueError('Выберите матчи')
                ids={int(mid) for mid in selected}
                if not ids.issubset({m['id'] for m in matches}):raise ValueError('Матчи изменились; обновите список')
                matches=[m for m in matches if m['id'] in ids]
            plan=await asyncio.to_thread(build_plan,matches,accounts,data.get('max_variants',20))
        except (ValueError,RuntimeError) as exc:
            return web.json_response({'error':str(exc)},status=400)
        self.plans={k:p for k,p in self.plans.items() if time.time()<p['expires_at']}
        plan.update(id=uuid.uuid4().hex,expires_at=time.time()+600,used=False)
        self.plans[plan['id']]=plan
        return web.json_response({**plan,'blocked_unknown_accounts':sorted(blocked)})

    async def start(self, request):
        data=await request.json()
        async with self.lock:
            if self.task and not self.task.done():return web.json_response({'error':'План уже выполняется'},status=409)
            plan=self.plans.get(data.get('plan_id'))
            if not plan or plan['used'] or time.time()>=plan['expires_at']:
                return web.json_response({'error':'План устарел или уже запускался'},status=409)
            plan['used']=True
            self.job={'id':plan['id'],'running':True,'stop_requested':False,'created_at':time.time(),
                      'items':[{**i,'state':'queue','note':''} for i in plan['items']]}
            self.save();self.task=asyncio.create_task(self.run())
        return web.json_response(self.job)

    async def run(self):
        try:
            for item in self.job['items']:
                if self.job['stop_requested']:break
                client=self.clients().get(item['account'])
                if client is None or (hasattr(client,'is_connected') and not client.is_connected()):
                    item.update(state='skipped',note='Аккаунт отключён');self.save();break
                item['state']='checking';self.save()
                def on_send():item.update(state='sending');self.save()
                state={'sent':False,'on_send':on_send}
                try:
                    result=await self.submit(item['account'],client,amount(item['stake']),
                                             [(l['match_id'],l['side'],amount(l['odds'])) for l in item['legs']],state)
                    body=json.loads(result.text)
                    item.update(state='ok' if body.get('ok') else 'unknown',note='Принято' if body.get('ok') else 'Результат требует ручной проверки')
                except asyncio.CancelledError:
                    item['state']='unknown' if state['sent'] else 'not_run';raise
                except Exception as exc:
                    item.update(state='unknown' if state['sent'] else 'error',note=type(exc).__name__)
                self.save()
                if item['state'] in ('unknown','error'):break  # Odds/balance changed or result ambiguous: stop the batch.
        finally:
            for item in self.job['items']:
                if item['state']=='queue':item['state']='not_run'
            self.job['running']=False;self.save()

    async def stop(self, request):
        if self.job:self.job['stop_requested']=True;self.save()
        return web.json_response({'ok':True})

    async def status(self, request):return web.json_response({'job':self.job})

    async def close(self, app=None):
        if self.task and not self.task.done():
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)

    def register(self, app):
        app.router.add_post('/api/cs3bet/bulk/plan',self.preview)
        app.router.add_post('/api/cs3bet/bulk/start',self.start)
        app.router.add_post('/api/cs3bet/bulk/stop',self.stop)
        app.router.add_get('/api/cs3bet/bulk',self.status)
        app.on_cleanup.append(self.close)
