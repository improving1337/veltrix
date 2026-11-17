"""Bounded, exhaustive scenario planner for non-monetary CS3BET game points."""
import asyncio
import itertools
import json
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
    # Enumerate coupons with DIFFERENT match sets. Each coupon carries a bitmap
    # of complete outcomes in which it wins. This checks coverage despite overlap.
    n = len(eligible)
    outcome_count = 1 << n
    universe = (1 << outcome_count)-1
    masks = [[sum(1 << outcome for outcome in range(outcome_count)
                  if ((outcome >> index) & 1) == side) for side in (0,1)] for index in range(n)]
    candidates = []
    def enumerate_coupons(index, coefficient, legs, coverage):
        if index == n:
            if len(legs) >= 2:
                candidates.append((coefficient, tuple(legs), coverage))
                if len(candidates) > 100000:
                    raise ValueError('Более 100000 допустимых купонов: выберите меньше матчей')
            return
        enumerate_coupons(index+1, coefficient, legs, coverage)
        for side in (0,1):
            value = coefficient * eligible[index]['odds'][side]
            if value <= MAX_ODDS:
                legs.append((index,side))
                enumerate_coupons(index+1, value, legs, coverage & masks[index][side])
                legs.pop()
    enumerate_coupons(0, Decimal(1), [], universe)
    if not candidates:
        raise ValueError('Нет допустимых экспрессов при лимите ×30')
    candidates.sort(key=lambda c:(c[0], c[2].bit_count()), reverse=True)
    capacity = min(len(pool), max_variants, len(candidates))

    def cover(threshold, force_peak=True):
        choices = [i for i,c in enumerate(candidates) if c[0] >= threshold]
        selected = [0] if force_peak else []
        covered = candidates[0][2] if force_peak else 0
        used = set(selected)
        while covered != universe and len(selected) < capacity:
            remaining = universe ^ covered
            best_index = None
            best_score = None
            for i in choices:
                if i in used:
                    continue
                coupon = candidates[i]
                new = (coupon[2] & remaining).bit_count()
                if new and (best_score is None or (new,coupon[0]) > best_score):
                    best_score = (new,coupon[0]);best_index = i
            if best_index is None:
                return None
            selected.append(best_index);used.add(best_index)
            covered |= candidates[best_index][2]
        return selected if covered == universe else None

    chosen = None
    # Peak payout is the primary goal. Prefer high-coefficient coverage too;
    # greedy coverage is bounded and does not claim a globally optimal portfolio.
    for ratio in ('1','.98','.95','.90','.85','.80','.75','.70','.60','.50','.35','0'):
        chosen = cover(MAX_ODDS * Decimal(ratio))
        if chosen is not None:
            break
    if chosen is None:
        # With very few accounts the global peak coupon may make full coverage
        # impossible. Prefer an explicitly disclosed coverage plan to a silent gap.
        chosen = cover(Decimal(0), force_peak=False)
    if chosen is None:
        raise ValueError('Не удалось полностью покрыть исходы в этом числе вариантов; увеличьте предел или сократите матчи')
    # Keep a full fixed-set coverage candidate as well. Adding the peak coupon
    # must not needlessly destroy the previous floor when the budget can fit both.
    groups_by_matches = {}
    for i,coupon in enumerate(candidates):
        key = tuple(index for index,side in coupon[1])
        if (1 << len(key)) <= capacity:
            groups_by_matches.setdefault(key, []).append(i)
    fixed = None
    fixed_score = None
    for key, indices in groups_by_matches.items():
        proposal = sorted(set(indices+[0]))
        if len(indices) != 1 << len(key) or len(proposal) > capacity:
            continue
        score = min(candidates[i][0] for i in indices)
        if fixed_score is None or score > fixed_score:
            fixed_score, fixed = score, proposal
    def coefficient_floor(indices):
        return min(max(candidates[i][0] for i in indices if (candidates[i][2] >> outcome) & 1)
                   for outcome in range(outcome_count))
    if fixed is not None and (0 not in chosen or coefficient_floor(fixed) > coefficient_floor(chosen)):
        chosen = fixed
    if 0 not in chosen:
        raise ValueError('Не удалось совместить пиковый купон и полное покрытие: увеличьте число вариантов/аккаунтов или выберите меньше матчей')
    used = set(chosen)
    for i in range(len(candidates)):
        if len(chosen) >= capacity:
            break
        if i not in used:
            chosen.append(i);used.add(i)
    chosen.sort()  # Candidates are ordered by descending coefficient.
    variants = [candidates[i] for i in chosen]
    items = []
    for index, account in enumerate(pool):
        variant = index % len(variants)
        coef, picks, _ = variants[variant]
        legs = [{'match_id':eligible[i]['id'],'side':str(side+1),'odds':str(eligible[i]['odds'][side]),
                 'team_a':eligible[i].get('team_a',''),'team_b':eligible[i].get('team_b','')} for i,side in picks]
        win = payout(account['stake']*coef)
        items.append({'account':account['name'],'variant':variant+1,'round':index//len(variants)+1,
                      'balance':str(account['balance']),'balance_ts':account.get('balance_ts'),
                      'stake':str(account['stake']),'remaining':str(account['balance']-account['stake']),
                      'coefficient':str(coef),'potential':str(win),
                      'winner_balance':str(account['balance']-account['stake']+win),'legs':legs})
    reserve = sum((amount(i['remaining']) for i in items), Decimal(0))
    largest_reserve = max(amount(i['remaining']) for i in items)
    groups = []
    for index, (coef, picks, coverage) in enumerate(variants, 1):
        group = [i for i in items if i['variant']==index]
        groups.append((coverage,len(group),max(amount(i['winner_balance']) for i in group),
                       sum((amount(i['potential']) for i in group),Decimal(0))))
    scenarios = []
    for outcome in range(outcome_count):
        winning = [g for g in groups if (g[0] >> outcome) & 1]
        if not winning:
            raise ValueError('Обнаружен непокрытый исход; план не создан')
        scenarios.append({'outcome':outcome,'accounts':sum(g[1] for g in winning),
                          'largest_balance':str(max(largest_reserve,max(g[2] for g in winning))),
                          'total_balance':str(reserve+sum((g[3] for g in winning),Decimal(0)))})
    selected_ids = {leg['match_id'] for item in items for leg in item['legs']}
    return {'items':items,'scenarios':scenarios,'variant_count':len(variants),
            'strategy':'target_30','target_coefficient':'30',
            'highest_possible_coefficient':str(candidates[0][0]),
            'highest_planned_coefficient':str(variants[0][0]),
            'peak_preserved':0 in chosen,'complete_coverage':True,'outcome_count':outcome_count,
            'coverage_matches':[m['id'] for m in eligible],
            'selected_matches':[m['id'] for m in eligible if m['id'] in selected_ids],
            'unused_matches':[m['id'] for m in eligible if m['id'] not in selected_ids], 'ignored_matches':ignored,
            'skipped_accounts':skipped,'account_count':len(items),
            'initial_total':str(sum((amount(i['balance']) for i in items),Decimal(0))),
            'total_stake':str(sum((amount(i['stake']) for i in items),Decimal(0))),
            'reserve':str(reserve),
            'worst_largest_balance':str(min(amount(i['largest_balance']) for i in scenarios)),
            'best_largest_balance':str(max(amount(i['largest_balance']) for i in scenarios)),
            'worst_total_balance':str(min(amount(i['total_balance']) for i in scenarios)),
            'best_total_balance':str(max(amount(i['total_balance']) for i in scenarios))}

RESUMABLE = {'error', 'not_run', 'skipped'}

def error_message(exc):
    if isinstance(exc, web.HTTPException):
        try:
            body = json.loads(exc.text)
            text = body.get('error') or body.get('message')
        except (ValueError, AttributeError):
            text = exc.text
        return str(text or exc.reason)[:600]
    return (str(exc) or type(exc).__name__)[:600]


def requote_item(item, matches):
    current = {m['id']:m for m in matches}
    available = []
    for original in item['legs']:
        match = current.get(original['match_id'])
        if not match or match.get('status') != 'betting_open':continue
        try:odds=amount(match['odds_a'] if original['side']=='1' else match['odds_b'])
        except (KeyError,ValueError):continue
        if odds <= 1 or odds > MAX_ODDS:continue
        available.append({**original,'odds':str(odds)})
    best = None
    for size in range(2,len(available)+1):
        for subset in itertools.combinations(available,size):
            coef = Decimal(1)
            for leg in subset:coef *= amount(leg['odds'])
            if coef <= MAX_ODDS and (best is None or coef > best[0]):best=(coef,list(subset))
    if best is None:raise ValueError('Не осталось допустимого экспресса из хотя бы двух исходных событий')
    coef,legs=best
    stake=amount(item['stake'])
    if stake < MIN_STAKE or stake > MAX_STAKE:raise ValueError('Исходная сумма вне лимита 100–50000')
    return {'legs':legs,'coefficient':str(coef),'potential':str(payout(stake*coef)),
            'winner_balance':str(amount(item['remaining'])+payout(stake*coef))}


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
        self.plans={};self.quotes={};self.task=None;self.lock=asyncio.Lock()

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
                if item['state'] != 'queue':continue
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
                    item.update(state='unknown' if state['sent'] else 'error',note=error_message(exc))
                self.save()
                if item['state'] in ('unknown','error'):break  # Odds/balance changed or result ambiguous: stop the batch.
        finally:
            for item in self.job['items']:
                if item['state']=='queue':item['state']='not_run'
            self.job['running']=False;self.save()

    async def requote(self, request):
        data=await request.json()
        async with self.lock:
            if not self.job or data.get('job_id') != self.job['id']:
                return web.json_response({'error':'Сохранённая очередь не найдена'},status=404)
            if self.job['running']:return web.json_response({'error':'Сначала остановите очередь'},status=409)
            clients=self.clients()
            source=next(((n,c) for n,c in clients.items() if c and (not hasattr(c,'is_connected') or c.is_connected())),None)
            if source is None:return web.json_response({'error':'Нет подключённого аккаунта для чтения матчей'},status=409)
            try:matches=await self.fetch_matches(*source)
            except Exception as exc:return web.json_response({'error':error_message(exc)},status=400)
            changes=[]
            for index,item in enumerate(self.job['items']):
                if item['state'] not in RESUMABLE:continue
                try:
                    new=requote_item(item,matches)
                    changes.append({'index':index,'account':item['account'],'before':item['legs'],
                                    'old_coefficient':item['coefficient'],'stake':item['stake'],**new})
                except ValueError as exc:
                    changes.append({'index':index,'account':item['account'],'error':str(exc)})
            token=uuid.uuid4().hex
            quote={'id':token,'job_id':self.job['id'],'expires_at':time.time()+180,'changes':changes}
            self.quotes={token:quote}
            return web.json_response(quote)

    async def resume(self, request):
        data=await request.json()
        async with self.lock:
            if not self.job or data.get('job_id') != self.job['id']:
                return web.json_response({'error':'Сохранённая очередь не найдена'},status=404)
            if self.job['running'] or (self.task and not self.task.done()):
                return web.json_response({'error':'Очередь уже выполняется'},status=409)
            todo=[i for i in self.job['items'] if i['state'] in RESUMABLE]
            if not todo:return web.json_response({'error':'Нет безопасно неотправленных строк для продолжения'},status=409)
            if any(i['account'] in self.unknown_accounts for i in todo):
                return web.json_response({'error':'Есть аккаунт с неизвестной прошлой отправкой; нужна ручная проверка истории игры'},status=409)
            quote_id=data.get('quote_id')
            if quote_id is not None:
                quote=self.quotes.get(quote_id)
                if not quote or quote['job_id']!=self.job['id'] or quote['expires_at']<=time.time():
                    return web.json_response({'error':'Пересчёт устарел. Запросите новый'},status=409)
                if any(c.get('error') for c in quote['changes']):
                    return web.json_response({'error':'Не все купоны удалось пересчитать; исходная очередь сохранена'},status=409)
                for change in quote['changes']:
                    item=self.job['items'][change['index']]
                    if item['state'] not in RESUMABLE or item['legs']!=change['before'] or item['stake']!=change['stake']:
                        return web.json_response({'error':'Очередь изменилась; пересчитайте снова'},status=409)
                for change in quote['changes']:
                    item=self.job['items'][change['index']]
                    item.update({key:change[key] for key in ('legs','coefficient','potential','winner_balance')})
                self.quotes.clear()
                self.job['requoted_at']=time.time()
            for item in todo:
                item.update(state='queue',note='')
            self.job.update(running=True,stop_requested=False,interrupted=False,resumed_at=time.time())
            self.save();self.task=asyncio.create_task(self.run())
            return web.json_response(self.job)

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
        app.router.add_post('/api/cs3bet/bulk/resume',self.resume)
        app.router.add_post('/api/cs3bet/bulk/requote',self.requote)
        app.router.add_post('/api/cs3bet/bulk/stop',self.stop)
        app.router.add_get('/api/cs3bet/bulk',self.status)
        app.on_cleanup.append(self.close)
