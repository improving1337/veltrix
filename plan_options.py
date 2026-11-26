"""Shared eligibility and absolute-deadline scheduling for all services."""
import asyncio
import operator
import math
import random
import re
import time
from datetime import datetime, timedelta, timezone

MOSCOW_OFFSET = 3 * 3600
DAY = 24 * 3600


def quiet_hours(extra):
    value = (extra or {}).get('quiet_hours')
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('Укажите время пропуска в формате ЧЧ:ММ по МСК')
    limits = []
    for key in ('from', 'to'):
        match = re.fullmatch(r'([01][0-9]|2[0-3]):([0-5][0-9])', str(value.get(key, '')))
        if not match:
            raise ValueError('Укажите время пропуска в формате ЧЧ:ММ по МСК')
        limits.append(int(match[1]) * 3600 + int(match[2]) * 60)
    if limits[0] == limits[1]:
        raise ValueError('Начало и конец времени пропуска должны отличаться')
    return tuple(limits)


def quiet_end(timestamp, quiet):
    """End of the pause containing timestamp, or None outside a pause."""
    if quiet is None:
        return None
    start, end = quiet
    since_start = (timestamp + MOSCOW_OFFSET - start) % DAY
    length = (end - start) % DAY
    return timestamp + (length - since_start) if since_start < length else None


def after_quiet(timestamp, quiet):
    end = quiet_end(timestamp, quiet)
    if end is None:
        return timestamp
    available = (quiet[0] - quiet[1]) % DAY
    return end + random.uniform(1, min(600, available / 2))


def account_filter(extra, metadata):
    rules = (extra or {}).get('account_filters') or []
    mode = (extra or {}).get('filter_mode', 'and')
    ops = {'>': operator.gt, '>=': operator.ge, '=': operator.eq, '<': operator.lt, '<=': operator.le}
    if mode not in ('and', 'or') or not isinstance(rules, list) or len(rules) > 12:
        raise ValueError('Некорректные фильтры аккаунтов')
    tests = []
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get('op'), str) or rule['op'] not in ops:
            raise ValueError('Проверь оператор фильтра')
        field, value, compare = rule.get('field'), rule.get('value'), ops[rule['op']]
        if field == 'flag' and value in ('general', 'public') and rule['op'] == '=':
            tests.append((field, compare, value))
        elif field in ('avatar_count', 'music_count') and type(value) is int and value >= 0:
            tests.append((field, compare, value))
        else:
            raise ValueError('Проверь условие отбора аккаунтов')
    def matches(name):
        if not tests:
            return True
        values = metadata(name) or {}
        results = (values.get(field) is not None and compare(values[field], value)
                   for field, compare, value in tests)
        return all(results) if mode == 'and' else any(results)
    return matches


def realistic_profile(extra):
    profile = (extra or {}).get('realistic')
    if profile is None:
        return None
    if not isinstance(profile, dict):
        raise ValueError('Некорректный профиль «Реалистик»')
    minutes, boost = profile.get('minutes'), profile.get('boost')
    if type(minutes) is not int or not 1 <= minutes <= 2880 or type(boost) is not int or not 0 <= boost <= 100:
        raise ValueError('Реалистик: от 1 до 2880 минут, сила старта от 0 до 100')
    return minutes, boost


def realistic_schedule(items, duration, at, profile):
    from automatic import DEFAULT_VIEW_ACTIVITY
    _, boost = profile
    bins = 240
    step = duration / bins
    weights = []
    for i in range(bins):
        fraction = (i + .5) / bins
        local = ((at(duration * fraction) + MOSCOW_OFFSET) % DAY) / 3600
        hour = int(local)
        fraction_hour = local - hour
        activity = DEFAULT_VIEW_ACTIVITY[hour] * (1 - fraction_hour) + DEFAULT_VIEW_ACTIVITY[(hour + 1) % 24] * fraction_hour
        weights.append(activity * (1 + boost / 20 * math.exp(-fraction * 6)))
    total = sum(weights)
    index, before, previous = 0, 0., 0.
    for i, item in enumerate(items, 1):
        quantile = total * (i - .5) / len(items)
        while before + weights[index] < quantile:
            before += weights[index]
            index += 1
        due = (index + (quantile - before) / weights[index]) * step
        item.update(delay=due - previous, at=due)
        previous = due


def schedule_order(items, start, finish, quiet, profile=None):
    if profile and finish is None:
        finish = after_quiet(start + 60 * profile[0], quiet)
    if finish is None:
        previous = start
        for item in items:
            due = after_quiet(previous + item['base_delay'], quiet)
            item.update(delay=due - previous, at=due - start)
            previous = due
        return items[-1]['at']
    if finish <= start:
        raise ValueError('Время окончания должно быть позже начала')
    if quiet is None:
        if profile:
            realistic_schedule(items, finish - start, lambda offset: start + offset, profile)
        else:
            schedule(items, finish - start)
        return items[-1]['at']
    if quiet_end(finish, quiet) is not None:
        raise ValueError('Время «Закончить к» попадает в интервал пропуска (МСК)')
    open_seconds = (quiet[0] - quiet[1]) % DAY
    origin = quiet[1] - MOSCOW_OFFSET

    def open_clock(timestamp):
        day, seconds = divmod(timestamp - origin, DAY)
        return day * open_seconds + min(seconds, open_seconds)

    start_open = open_clock(start)
    available = open_clock(finish) - start_open
    if available <= 0:
        raise ValueError('До срока нет доступного времени вне интервала пропуска')
    def to_timestamp(offset):
        day, seconds = divmod(start_open + offset, open_seconds)
        return origin + day * DAY + seconds

    if profile:
        realistic_schedule(items, available, to_timestamp, profile)
    else:
        schedule(items, available)
    previous = start
    for item in (items if profile else items[:-1]):
        day, seconds = divmod(start_open + item['at'], open_seconds)
        due = origin + day * DAY + seconds
        item.update(delay=due - previous, at=due - start)
        previous = due
    if not profile:
        items[-1].update(delay=finish - previous, at=finish - start)
    return items[-1]['at']


async def wait_before_action(extra):
    wait = (extra or {}).get('wait_before_action')
    return await wait() if wait else False


def deadline(value):
    if value in (None, ''):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError('Некорректное время последнего действия')
    if not math.isfinite(result) or result <= time.time():
        raise ValueError('Время последнего действия должно быть в будущем')
    return result


def schedule(items, duration):
    if not items or duration <= 0:
        raise ValueError('Недостаточно времени или нет подходящих аккаунтов')
    # Zero-sum deviations guarantee exact duration and ±10–40% per interval.
    deviations = []
    pairs = len(items) // 2
    if len(items) % 2 and len(items) > 1:
        a, b = random.uniform(.1, .2), random.uniform(.1, .2)
        sign = random.choice([-1, 1])
        deviations.extend([sign*a, sign*b, -sign*(a+b)])
        pairs -= 1
    for _ in range(pairs):
        d = random.uniform(.1, .4)
        deviations.extend([d, -d])
    if len(items) == 1:
        deviations = [0]
    random.shuffle(deviations)
    at = 0.
    for item, deviation in zip(items, deviations):
        delay = duration / len(items) * (1 + deviation)
        at += delay
        item.update(delay=delay, at=at)
    items[-1]['delay'] += duration - at
    items[-1]['at'] = duration


async def eligible(alive, extra):
    raw_days = (extra or {}).get('freshness_days') or 0
    try:
        days = int(raw_days)
        if isinstance(raw_days, bool) or float(raw_days) != days:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('Свежесть аккаунтов должна быть целым числом дней')
    if not 0 <= days <= 36500:
        raise ValueError('Свежесть аккаунтов: от 0 до 36500 дней')
    if not days:
        return alive
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sem = asyncio.Semaphore(8)

    async def check(name, client):
        async with sem:
            try:
                photos = await asyncio.wait_for(client.get_profile_photos('me', limit=1), 25)
                photo = next(iter(photos), None)
                if photo is None:
                    return name
                stamp = getattr(photo, 'date', None)
                if not isinstance(stamp, datetime):
                    return None
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                return name if stamp <= cutoff else None
            except Exception:
                return None  # Unknown history must not bypass an enabled filter.
    names = await asyncio.gather(*(check(n, c) for n, c in alive.items()))
    return {name: alive[name] for name in names if name is not None}
