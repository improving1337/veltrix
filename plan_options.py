"""Shared eligibility and absolute-deadline scheduling for all services."""
import asyncio
import math
import random
import time
from datetime import datetime, timedelta, timezone


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
