"""
Присутствие аккаунтов «в сети».

1. Фоновая работа: в течение суток КАЖДЫЙ аккаунт хотя бы раз выходит в сеть на 10-15 минут и выходит обратно.
   Время у каждого свое (случайное), одновременно в сети не больше MAX_SHARE от всех аккаунтов (минимум 1).
2. Когда аккаунт работает в услуге «Мульти», он тоже выходит в сеть на время работы (hold / release).
   Счётчик удержаний не даёт фоновому режиму и «Мульти» выключить друг друга: из сети аккаунт выходит,
   только когда его больше никто не держит.
3. Время последнего выхода в сеть каждого аккаунта хранится в presence.json (переживает перезапуск).

Эндпоинт: GET /api/presence
"""
import asyncio
import json
import math
import os
import random
import time
from pathlib import Path

from aiohttp import web

SESSION_MIN, SESSION_MAX = 10 * 60, 15 * 60   # сколько аккаунт сидит в сети за один заход, сек
HEARTBEAT = (60, 100)                         # как часто подтверждаем «в сети» (Telegram гасит статус через ~5 мин)
MAX_SHARE = 0.10                              # одновременно в сети не больше 10% аккаунтов (минимум 1)
FIRST_SPREAD = 20 * 3600                      # новые аккаунты распределяются по ближайшим 20 часам
REPEAT = (8 * 3600, 20 * 3600)                # следующий заход через 8-20 часов после предыдущего (< 24 ч)
RETRY = (5 * 60, 15 * 60)                     # если выйти в сеть не вышло — повтор через 5-15 минут
TICK = (20, 40)                               # как часто планировщик проверяет, кому пора


_conn_locks = {}


async def ensure_connected(client, tries=3):
    """Если Telethon-клиент отвалился (Cannot send requests while disconnected) — переподключаем.
    Бросает ConnectionError, если за несколько попыток связи так и нет."""
    if client.is_connected():
        return
    lock = _conn_locks.setdefault(id(client), asyncio.Lock())
    async with lock:
        if client.is_connected():
            return
        last = None
        for i in range(tries):
            try:
                await asyncio.wait_for(client.connect(), 25)
                if client.is_connected():
                    return
            except Exception as e:
                last = e
            await asyncio.sleep(1 + i * 2)
        raise ConnectionError("нет связи с Telegram" + (f" ({type(last).__name__})" if last else ""))


async def _send(client, online):
    """Статус «в сети» / «не в сети». -> None при успехе, иначе текст ошибки."""
    from telethon.tl.functions.account import UpdateStatusRequest
    try:
        if online:
            await ensure_connected(client)
        await client(UpdateStatusRequest(offline=not online))
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:120]


class PresenceKeeper:
    def __init__(self):
        self.get_clients = lambda: {}
        self.path = None
        self.state = {}      # имя -> {"last": ts|None, "next": ts, "count": n, "src": "auto"|"multi"}
        self.holds = {}      # имя -> сколько задач сейчас держат аккаунт в сети
        self.sessions = {}   # имя -> asyncio.Task фонового захода
        self.task = None

    # ---------- настройка / хранение ----------
    def configure(self, get_clients, path):
        self.get_clients, self.path = get_clients, Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = {k: v for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            self.state = {}

    def _save(self):
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[PRESENCE] не удалось сохранить: {e}", flush=True)

    def _mark(self, name, src):
        """Аккаунт только что вышел в сеть: запоминаем время и планируем следующий заход."""
        now = time.time()
        st = self.state.setdefault(name, {})
        st.update(last=now, next=now + random.uniform(*REPEAT), count=int(st.get("count", 0)) + 1, src=src)
        self._save()

    # ---------- удержание (используют и «Мульти», и фоновые заходы) ----------
    async def hold(self, name, client, src="multi"):
        """Выйти в сеть. Каждый hold обязательно парный к release. -> None или текст ошибки."""
        self.holds[name] = self.holds.get(name, 0) + 1
        err = await _send(client, True)
        if not err:
            self._mark(name, src)
        return err

    async def release(self, name, client):
        """Отпустить аккаунт; если больше никто не держит — выйти из сети. -> None или текст ошибки."""
        left = self.holds.get(name, 0) - 1
        if left > 0:
            self.holds[name] = left
            return None
        self.holds.pop(name, None)
        return await _send(client, False)

    # ---------- фоновый планировщик ----------
    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self._loop())

    async def stop(self):
        tasks = [t for t in [self.task, *self.sessions.values()] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.task = None

    async def _loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[PRESENCE] ошибка планировщика: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(random.uniform(*TICK))

    def _tick(self):
        clients = {n: c for n, c in self.get_clients().items() if c}
        now = time.time()
        for n in clients:   # новые аккаунты раскидываем по суткам, чтобы не зашли все сразу
            if n not in self.state:
                self.state[n] = {"last": None, "next": now + random.uniform(0, FIRST_SPREAD), "count": 0}
        cap = max(1, math.ceil(len(clients) * MAX_SHARE))
        online = sum(1 for n in clients if self.holds.get(n, 0) > 0)
        due = sorted((n for n in clients
                      if self.state[n].get("next", 0) <= now and self.holds.get(n, 0) == 0 and n not in self.sessions),
                     key=lambda n: self.state[n]["next"])
        for n in due[:max(0, cap - online)]:
            self.sessions[n] = asyncio.create_task(self._session(n, clients[n]))

    async def _session(self, name, client):
        try:
            minutes = random.uniform(SESSION_MIN, SESSION_MAX)
            err = await self.hold(name, client, "auto")
            if err:
                self.state[name]["next"] = time.time() + random.uniform(*RETRY)
                print(f"[PRESENCE] {name}: не удалось выйти в сеть: {err}", flush=True)
                return
            print(f"[PRESENCE] {name}: в сети на {minutes / 60:.0f} мин", flush=True)
            end = time.time() + minutes
            while time.time() < end:
                await asyncio.sleep(min(random.uniform(*HEARTBEAT), max(1.0, end - time.time())))
                if time.time() < end:
                    await _send(client, True)   # подтверждаем статус, иначе Telegram через ~5 минут покажет «был(а) недавно»
        finally:
            await self.release(name, client)   # выйти из сети всегда, даже при остановке программы
            self.sessions.pop(name, None)
            print(f"[PRESENCE] {name}: вышел из сети", flush=True)

    # ---------- для интерфейса ----------
    def info(self, name):
        st = self.state.get(name) or {}
        return {"online": self.holds.get(name, 0) > 0, "last": st.get("last"),
                "next": st.get("next"), "count": st.get("count", 0), "src": st.get("src")}


keeper = PresenceKeeper()


async def api_presence(request):
    names = sorted(set(keeper.state) | set(keeper.holds))
    items = [{"name": n, **keeper.info(n)} for n in names]
    return web.json_response({"items": items, "online": sum(1 for i in items if i["online"])})


def register(app):
    app.add_routes([web.get("/api/presence", api_presence)])
