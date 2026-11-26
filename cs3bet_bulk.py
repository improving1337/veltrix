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
TIER_LIMITS = (MAX_STAKE, Decimal('32500'), Decimal('17500'))
LENGTH_OPTIONS = {2, 3, 4, 6, 8}


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


def stake_parts(balance, tiered):
    parts = []
    for limit in TIER_LIMITS if tiered else TIER_LIMITS[:1]:
        stake = min(balance, limit)
        if stake < MIN_STAKE:
            break
        parts.append(stake)
        balance -= stake
    return parts


def build_plan(matches, accounts, max_variants=20, *, lengths=None, single=False,
               tiered=False, allow_shorter=False, target_balance=1500000):
    modern = lengths is not None or single
    if any(type(value) is not bool for value in (single, tiered, allow_shorter)):
        raise ValueError('Некорректные параметры плана')
    if lengths is not None and (not isinstance(lengths, (list, tuple)) or not lengths or
                                any(type(n) is not int or n not in LENGTH_OPTIONS for n in lengths)):
        raise ValueError('Выберите длины экспресса: 2, 3, 4, 6 или 8')
    minimum, maximum = (2, 256) if modern else (4, 64)
    if type(max_variants) is not int or not minimum <= max_variants <= maximum:
        raise ValueError(f'Число вариантов: целое от {minimum} до {maximum}')
    target_balance = amount(target_balance)
    if target_balance <= 0:
        raise ValueError('Целевой баланс должен быть положительным')
    allowed = {1} if single else set(lengths if lengths is not None else range(2, 13))
    tiered = tiered and modern
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
        parts = stake_parts(balance, tiered)
        pool.append({**account, 'name': name, 'balance': balance, 'stake': sum(parts), 'parts': parts})
    pool.sort(key=lambda a: (-a['balance'], a['name']))
    if len(pool) < (2 if single else 4):
        raise ValueError('Для покрытия не хватает аккаунтов с балансом от 100')
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
    if single and len(eligible) != 1:
        raise ValueError('Выберите ровно один открытый матч с двумя корректными коэффициентами')
    if not single and len(eligible) < min(allowed):
        raise ValueError(f'Выберите не меньше {min(allowed)} открытых матчей')
    # Enumerate coupons with DIFFERENT match sets. Each coupon carries a bitmap
    # of complete outcomes in which it wins. This checks coverage despite overlap.
    n = len(eligible)
    outcome_count = 1 << n
    universe = (1 << outcome_count)-1
    masks = [[sum(1 << outcome for outcome in range(outcome_count)
                  if ((outcome >> index) & 1) == side) for side in (0,1)] for index in range(n)]
    candidates = []
    max_length = max(allowed)

    def enumerate_coupons(index, coefficient, legs, coverage):
        if len(legs) + n - index < min(allowed):
            return
        if index == n or len(legs) == max_length:
            if len(legs) in allowed:
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

    if modern:
        chosen = optimized_cover(candidates, capacity, universe, cover)
    else:
        chosen = legacy_cover(candidates, capacity, outcome_count, universe, cover)
    variants = [candidates[i] for i in chosen]
    items = []
    allocation = list(range(len(variants)))
    if single:
        allocation.sort(key=lambda variant: variants[variant][1][0][1])
    elif modern:
        # Give the lowest-paying covering coupons the strongest initial balances.
        allocation.sort(key=lambda variant: variants[variant][0])
    for index, account in enumerate(pool):
        variant = allocation[index % len(variants)] if index < len(variants) or single else index % len(variants)
        coef, picks, _ = variants[variant]
        legs = [{'match_id':eligible[i]['id'],'side':str(side+1),'odds':str(eligible[i]['odds'][side]),
                 'team_a':eligible[i].get('team_a',''),'team_b':eligible[i].get('team_b','')} for i,side in picks]
        win = sum(payout(part * coef) for part in account['parts'])
        item = {'account':account['name'],'variant':variant+1,'round':index//len(variants)+1,
                'balance':str(account['balance']),'balance_ts':account.get('balance_ts'),
                'stake':str(account['stake']),'remaining':str(account['balance']-account['stake']),
                'coefficient':str(coef),'potential':str(win),
                'winner_balance':str(account['balance']-account['stake']+win),'legs':legs}
        if modern:
            item.update(allow_shorter=allow_shorter and not single, tranches=[str(part) for part in account['parts']])
        items.append(item)
    reserve = sum((amount(i['remaining']) for i in items), Decimal(0))
    largest_reserve = max(amount(i['remaining']) for i in items)
    groups = []
    for index, (coef, picks, coverage) in enumerate(variants, 1):
        group = [i for i in items if i['variant']==index]
        groups.append((coverage,len(group),max(amount(i['winner_balance']) for i in group),
                       sum((amount(i['potential']) for i in group),Decimal(0)),coef,
                       sum(amount(i['winner_balance']) >= target_balance for i in group)))
    scenarios = []
    for outcome in range(outcome_count):
        winning = [g for g in groups if (g[0] >> outcome) & 1]
        if not winning:
            raise ValueError('Обнаружен непокрытый исход; план не создан')
        scenarios.append({'outcome':outcome,'accounts':sum(g[1] for g in winning),
                          'largest_balance':str(max(largest_reserve,max(g[2] for g in winning))),
                          'total_balance':str(reserve+sum((g[3] for g in winning),Decimal(0))),
                          'highest_coefficient':str(max(g[4] for g in winning)),
                          'accounts_at_target':sum(g[5] for g in winning)})
    selected_ids = {leg['match_id'] for item in items for leg in item['legs']}
    return {'items':items,'scenarios':scenarios,'variant_count':len(variants),
            'exact_target_candidates':sum(coefficient == MAX_ODDS for coefficient, _, _ in candidates),
            'exact_target_variants':sum(coefficient == MAX_ODDS for coefficient, _, _ in variants),
            'strategy':'single' if single else 'balance_floor' if modern else 'target_30',
            'target_coefficient':str(MAX_ODDS),'target_balance':str(target_balance),
            'lengths':sorted({len(picks) for _,picks,_ in variants}),
            'tiered':tiered,'allow_shorter':allow_shorter and not single,
            'highest_possible_coefficient':str(candidates[0][0]),
            'highest_planned_coefficient':str(variants[0][0]),
            'worst_highest_coefficient':str(min(amount(i['highest_coefficient']) for i in scenarios)),
            'minimum_accounts_at_target':min(i['accounts_at_target'] for i in scenarios),
            'maximum_accounts_at_target':max(i['accounts_at_target'] for i in scenarios),
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


def optimized_cover(candidates, capacity, universe, cover):
    # Sorted coefficients supply an exact upper bound from the least favorable
    # complete outcome. Searching for a small cover at that bound is bounded.
    covered = 0
    ceiling = None
    for coefficient, _, mask in candidates:
        covered |= mask
        if covered == universe:
            ceiling = coefficient
            break
    if ceiling is None:
        raise ValueError('С выбранными длинами и лимитом ×30 есть исход без выигрышного купона. Измените матчи или длины.')
    chosen = cover(ceiling, force_peak=False)
    if chosen is None:
        scores = sorted({coefficient for coefficient, _, _ in candidates if coefficient <= ceiling})
        low, high = 0, len(scores)-2
        while low <= high:
            middle = (low+high)//2
            proposal = cover(scores[middle], force_peak=False)
            if proposal is None:
                high = middle-1
            else:
                chosen = proposal
                low = middle+1
    if chosen is None:
        raise ValueError('Не найдено полное покрытие. Увеличьте число вариантов или выберите другие матчи.')
    used = set(chosen)
    for index in range(len(candidates)):
        if len(chosen) == capacity:
            break
        if index not in used:
            chosen.append(index)
    return sorted(chosen)


def legacy_cover(candidates, capacity, outcome_count, universe, cover):
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
    return sorted(chosen)

RESUMABLE = {'error', 'not_run', 'skipped', 'rejected'}

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
    strict = 'tranches' in item and not item.get('allow_shorter')
    minimum = len(item['legs']) if strict else 2
    for size in range(minimum,len(available)+1):
        for subset in itertools.combinations(available,size):
            coef = Decimal(1)
            for leg in subset:coef *= amount(leg['odds'])
            if coef <= MAX_ODDS and (best is None or coef > best[0]):best=(coef,list(subset))
    if best is None:
        raise ValueError('Исходный купон недоступен при текущих коэффициентах и выбранной длине')
    coef,legs=best
    parts = [amount(part) for part in item.get('tranches', [item['stake']])]
    if any(not MIN_STAKE <= part <= MAX_STAKE for part in parts):
        raise ValueError('Сумма одной ставки вне лимита 100–50000')
    win = sum(payout(part*coef) for part in parts)
    return {'legs':legs,'coefficient':str(coef),'potential':str(win),
            'winner_balance':str(amount(item['remaining'])+win)}


def settled_coverage(job):
    positions = {mid: index for index, mid in enumerate(job['coverage_matches'])}
    count = 1 << len(positions)
    target = amount(job['target_balance'])
    prepared = []
    for item in job['items']:
        bets = item.get('accepted_bets')
        if bets is None:
            bets = [item] if item['state'] == 'ok' and not item.get('tranches') else []
        accepted = []
        remaining = amount(item['balance'])
        pending = item.get('pending_bet')
        if item['state'] == 'unknown' and pending:
            remaining -= amount(pending['stake'])
        for bet in bets:
            stake = amount(bet['stake'])
            remaining -= stake
            mask, bits = 0, 0
            for leg in bet['legs']:
                bit = 1 << positions[leg['match_id']]
                mask |= bit
                if leg['side'] == '2':
                    bits |= bit
            accepted.append((mask, bits, payout(stake * amount(bet['coefficient']))))
        if accepted:
            prepared.append((remaining, accepted))
    covered, minimum_count, floor = 0, len(prepared), None
    for outcome in range(count):
        winners = []
        for remaining, bets in prepared:
            win = sum(value for mask,bits,value in bets if outcome & mask == bits)
            if win:
                winners.append(remaining + win)
        covered += bool(winners)
        minimum_count = min(minimum_count, sum(balance >= target for balance in winners))
        least = max(winners, default=Decimal(0))
        floor = least if floor is None else min(floor, least)
    return {'covered':covered,'total':count,'minimum_accounts_at_target':minimum_count,
            'worst_winner_balance':str(floor),'target_balance':str(target)}


class BulkGame:
    def __init__(self, root, clients, state, fetch_matches, submit, identities=None, parallelism=1, continue_on_error=False):
        self.parallelism = max(1,min(3,int(parallelism)))
        self.continue_on_error = continue_on_error
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
                elif item['state'] in ('checking','queue'):
                    item['state']='partial' if item.get('accepted_bets') else 'not_run'
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
            plan=await asyncio.to_thread(build_plan,matches,accounts,data.get('max_variants',20),
                                         lengths=data.get('lengths'),single=data.get('single',False),
                                         tiered=data.get('tiered',False),allow_shorter=data.get('allow_shorter',False),
                                         target_balance=data.get('target_balance',1500000))
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
                      'coverage_matches':plan['coverage_matches'],'target_balance':plan['target_balance'],
                      'items':[{**i,'state':'queue','note':''} for i in plan['items']]}
            self.save();self.task=asyncio.create_task(self.run())
        return web.json_response(self.job)

    async def run(self):
        queue = [item for item in self.job['items'] if item['state']=='queue']
        halt = asyncio.Event()
        async def worker():
            while queue and not halt.is_set() and not self.job['stop_requested']:
                item = queue.pop(0)
                client=self.clients().get(item['account'])
                if client is None or (hasattr(client,'is_connected') and not client.is_connected()):
                    item.update(state='skipped',note='Аккаунт отключён');self.save()
                    if not self.continue_on_error:halt.set()
                    continue
                await self.run_item(item, client)
                if item['state'] not in ('ok', 'partial') and not self.continue_on_error:
                    halt.set()
        tasks=[asyncio.create_task(worker()) for _ in range(self.parallelism)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            for item in self.job['items']:
                if item['state']=='queue':item['state']='not_run'
            self.job['running']=False;self.save()

    async def run_item(self, item, client):
        parts = item.get('tranches')
        accepted = item.get('accepted_bets', [])
        stakes = parts if parts is not None else [item['stake']]
        first_note = ''
        for part in stakes[len(accepted):]:
            if self.job['stop_requested']:
                item.update(state='partial' if accepted else 'not_run', note='Остановка очереди')
                break
            item['state'] = 'checking'
            self.save()
            state, ticket = {'sent':False, 'allow_shorter':item.get('allow_shorter', False) and not accepted}, None

            def on_send():
                nonlocal ticket
                coefficient = state.get('coefficient', item['coefficient'])
                quoted = state.get('quoted_odds', {})
                chosen = state.get('selected_matches')
                legs = [{**leg, 'odds':quoted.get(leg['match_id'],leg['odds'])} for leg in item['legs']
                        if chosen is None or leg['match_id'] in chosen]
                ticket = {'stake':part, 'coefficient':coefficient, 'legs':legs}
                item['state'] = 'sending'
                item['pending_bet'] = ticket
                self.save()

            state['on_send'] = on_send
            for attempt in range(2):
                try:
                    picks = [(leg['match_id'], leg['side'], amount(leg['odds'])) for leg in item['legs']]
                    result = await self.submit(item['account'], client, amount(part), picks, state)
                    body = json.loads(result.text)
                    raw = body.get('response') or {}
                    if not isinstance(raw, dict):
                        raw = {}
                    if body.get('ok'):
                        if ticket is None:
                            ticket = {'stake':part, 'coefficient':str(body.get('coefficient') or item['coefficient']),
                                      'legs':item['legs']}
                        if parts is not None:
                            accepted.append(ticket)
                            item['accepted_bets'] = accepted
                        if len(ticket['legs']) < len(item['legs']):
                            first_note = 'Купон укорочен: ' + str(len(ticket['legs'])) + ' исхода'
                            item.setdefault('planned_legs', item['legs'])
                        if ticket['coefficient'] != item['coefficient']:
                            item.setdefault('planned_coefficient', item['coefficient'])
                        item.update(coefficient=ticket['coefficient'], legs=ticket['legs'], state='ok',
                                    note=first_note or 'Принято')
                        if parts is not None:
                            spent = sum(amount(bet['stake']) for bet in accepted)
                            win = sum(payout(amount(bet['stake']) * amount(bet['coefficient'])) for bet in accepted)
                            item.update(accepted_stake=str(spent), potential=str(win),
                                        winner_balance=str(amount(item['balance'])-spent+win))
                        break
                    status = body.get('status')
                    definite = ((status in (200,201) and raw.get('success') is False) or
                                (status in (400,401,403,404,405,409,422,429) and
                                 (raw.get('success') is False or raw.get('error') or raw.get('message'))))
                    item.update(state=('partial' if accepted else 'rejected') if definite else 'unknown',
                                note=str(body.get('error') or raw.get('message') or raw.get('error') or
                                         'Результат требует ручной проверки')[:600])
                    break
                except asyncio.CancelledError:
                    item['state'] = 'unknown' if state['sent'] else 'partial' if accepted else 'not_run'
                    raise
                except Exception as exc:
                    transient = not isinstance(exc, (web.HTTPException, ValueError))
                    if not state['sent'] and transient and attempt == 0 and self.continue_on_error:
                        item['note'] = 'Повтор проверки до отправки: ' + error_message(exc)
                        self.save()
                        await asyncio.sleep(max(1,int(getattr(exc,'retry_after',0) or 1)))
                        continue
                    item.update(state='unknown' if state['sent'] else 'partial' if accepted else 'error',
                                note=error_message(exc))
                    break
            if item['state'] != 'unknown':
                item.pop('pending_bet', None)
            if item['state'] == 'ok' and parts is not None and len(accepted) < len(stakes):
                item['state'] = 'checking'
                self.save()
                continue
            break
        self.save()

    async def confirm_manual(self, request):
        data=await request.json()
        async with self.lock:
            if not self.job or data.get('job_id')!=self.job['id']:
                return web.json_response({'error':'Очередь не найдена'},status=404)
            if self.job['running']:return web.json_response({'error':'Сначала остановите очередь'},status=409)
            item=next((i for i in self.job['items'] if i['account']==data.get('account')),None)
            if not item or item['state']!='unknown' or data.get('confirmed') is not True:
                return web.json_response({'error':'Нужно явное подтверждение неизвестной отправки'},status=409)
            pending = item.pop('pending_bet', None)
            if pending:
                item.update(legs=pending['legs'], coefficient=pending['coefficient'])
                if item.get('tranches'):
                    accepted = item.setdefault('accepted_bets', [])
                    accepted.append(pending)
                    item['accepted_stake'] = str(sum(amount(bet['stake']) for bet in accepted))
            item.update(state='partial' if len(item.get('accepted_bets', [])) < len(item.get('tranches', [])) else 'ok',
                        manual_verified=True,manual_verified_at=time.time(),
                        note='Ставка подтверждена пользователем в истории игры, не API')
            if not any(i['state']=='unknown' and i['account']==item['account'] for i in self.job['items']):
                self.unknown_accounts.discard(item['account'])
            self.save()
            return web.json_response({'ok':True})

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

    async def status(self, request):
        metrics = None
        if self.job and self.job.get('coverage_matches') and not self.job['running']:
            metrics = settled_coverage(self.job)
        return web.json_response({'job':self.job,'coverage':metrics})

    async def close(self, app=None):
        if self.task and not self.task.done():
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)

    def register(self, app):
        app.router.add_post('/api/cs3bet/bulk/plan',self.preview)
        app.router.add_post('/api/cs3bet/bulk/start',self.start)
        app.router.add_post('/api/cs3bet/bulk/resume',self.resume)
        app.router.add_post('/api/cs3bet/bulk/confirm-manual',self.confirm_manual)
        app.router.add_post('/api/cs3bet/bulk/requote',self.requote)
        app.router.add_post('/api/cs3bet/bulk/stop',self.stop)
        app.router.add_get('/api/cs3bet/bulk',self.status)
        app.on_cleanup.append(self.close)
