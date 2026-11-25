"""Cached channel memberships and scheduled, one-account departures."""
import asyncio
import logging
import random
import time
from collections import defaultdict
from pathlib import Path

from aiohttp import web
from telethon.errors import FloodWaitError, RPCError, UserNotParticipantError
from telethon.tl import functions, types

from template_library import atomic_json_save, json_load

LOG = logging.getLogger(__name__)
CACHE_SECONDS = 900
SCAN_GAP = 4
RETRY_SECONDS = 900


class Subscriptions:
    def __init__(self, directory, accounts, settings, clock=time.time, *, eligible=None):
        self.path = Path(directory) / "security" / "subscriptions.json"
        self.accounts, self.settings, self.clock = accounts, settings, clock
        self.eligible = eligible or (lambda account: True)
        state = json_load(self.path)
        self.memberships = state.get("memberships", {})
        self.rules = state.get("rules", {})
        self.cooldown = state.get("cooldown", {})
        self.avatars = {}
        self.lock = asyncio.Lock()
        self.image_limit = asyncio.Semaphore(3)
        self.scan = None
        self.scan_errors = 0
        legacy = json_load(Path(directory) / "channels_count.json")
        for name, acc in accounts().items():
            count = legacy.get(name, {}).get("n")
            if type(count) is int:
                acc.channels = count
        for entry in self.known().values():
            accounts()[entry["name"]].channels = len(entry["channels"])

    def save(self):
        atomic_json_save(self.path, {"memberships": self.memberships,
                                     "rules": self.rules, "cooldown": self.cooldown})

    def connected(self):
        return {str(acc.me.id): acc for acc in self.accounts().values()
                if acc.me is not None and acc.client.is_connected()}

    def known(self):
        names = self.accounts()
        return {key: entry for key, entry in self.memberships.items() if entry["name"] in names
                and (names[entry["name"]].me is None or str(names[entry["name"]].me.id) == key)}

    def channel_members(self):
        result = defaultdict(dict)
        for account, entry in self.known().items():
            for channel, info in entry["channels"].items():
                result[channel][account] = info
        return result

    def snapshot(self):
        members = self.channel_members()
        channels = []
        for channel in members.keys() | self.rules.keys():
            entries = members.get(channel, {})
            rule = self.rules.get(channel, {})
            info = max(entries.values(), key=lambda row: row["seen"],
                       default=rule.get("info", {"title": channel, "username": "", "photo": ""}))
            channels.append({"id": channel, "title": info["title"], "username": info["username"],
                             "photo": info["photo"], "count": len(entries),
                             "enabled": bool(rule), "due": rule.get("due")})
        channels.sort(key=lambda row: (-row["count"], row["title"].casefold(), row["id"]))
        known = self.known()
        return {"channels": channels, "cached": len(known), "total": len(self.accounts()),
                "updated": min((entry["seen"] for entry in known.values()), default=None),
                "scanning": self.scan is not None and not self.scan.done(), "errors": self.scan_errors}

    def request_scan(self):
        if self.scan is None or self.scan.done():
            self.scan = asyncio.create_task(self.refresh())
            self.scan.add_done_callback(self.scan_finished)

    @staticmethod
    def scan_finished(task):
        if not task.cancelled() and task.exception() is not None:
            LOG.error("Subscription refresh failed", exc_info=task.exception())

    def scan_due(self, account, now):
        previous = self.memberships.get(account)
        return self.cooldown.get(account, 0) <= now and (
            previous is None or previous["seen"] + CACHE_SECONDS <= now)

    async def refresh(self):
        self.scan_errors = 0
        for account, acc in self.connected().items():
            async with self.lock:
                now = self.clock()
                if not self.scan_due(account, now) or self.connected().get(account) is not acc:
                    continue
                try:
                    channels = await asyncio.wait_for(self.read_dialogs(acc.client, now), timeout=50)
                except FloodWaitError as exc:
                    self.cooldown[account] = now + max(CACHE_SECONDS, exc.seconds)
                    self.scan_errors += 1
                except (RPCError, OSError, ValueError):
                    self.cooldown[account] = now + CACHE_SECONDS
                    self.scan_errors += 1
                    LOG.warning("Could not refresh subscriptions for Telegram ID %s", account)
                else:
                    self.memberships[account] = {"name": acc.name, "seen": now, "channels": channels}
                    acc.channels = len(channels)
                    self.cooldown.pop(account, None)
                self.save()
            await asyncio.sleep(SCAN_GAP)

    @staticmethod
    async def read_dialogs(client, now):
        channels = {}
        async for dialog in client.iter_dialogs(ignore_migrated=True):
            channel = dialog.entity
            if isinstance(channel, types.Channel) and channel.broadcast and not channel.left:
                channels[str(channel.id)] = {"title": channel.title, "username": channel.username or "",
                                             "photo": str(getattr(channel.photo, "photo_id", "")), "seen": now,
                                             "access_hash": channel.access_hash}
        return channels

    def next_due(self):
        prefs = self.settings()
        spread = prefs["subscription_spread"] / 100
        multiplier = random.uniform(1 - spread, 1 + spread)
        return self.clock() + max(60, prefs["subscription_hours"] * 3600 * multiplier)

    def reschedule(self):
        if self.rules:
            for rule in self.rules.values():
                rule["due"] = self.next_due()
            self.save()

    def configure(self, channel, enabled):
        if type(enabled) is not bool:
            raise ValueError("Укажи, включён ли автовыход")
        if enabled:
            entries = self.channel_members().get(channel)
            if not entries:
                raise ValueError("Канал отсутствует в списке подписок")
            if channel not in self.rules:
                self.rules[channel] = {"due": self.next_due(), "info": next(iter(entries.values()))}
        else:
            self.rules.pop(channel, None)
        self.save()

    def remove_membership(self, account, channel, acc):
        channels = self.memberships[account]["channels"]
        channels.pop(channel, None)
        acc.channels = len(channels)
        self.save()

    async def leave_one(self, channel, automatic=False):
        async with self.lock:
            now = self.clock()
            rule = self.rules.get(channel)
            if automatic and (rule is None or rule["due"] > now):
                return
            connected = self.connected()
            entries = self.channel_members().get(channel, {})
            candidates = [(account, info) for account, info in entries.items()
                          if account in connected and self.eligible(connected[account]) and self.cooldown.get(account, 0) <= now
                          and info["access_hash"] is not None]
            if not candidates:
                if automatic:
                    rule["due"] = now + RETRY_SECONDS
                    self.save()
                    return
                raise ValueError("Нет подключённого подписчика, который сейчас может выйти")
            account, info = random.choice(candidates)
            acc = connected[account]
            if automatic:
                # Record the next due time before sending so a restart cannot leave twice in one interval.
                rule["due"] = self.next_due()
                self.save()
            try:
                await asyncio.wait_for(acc.client(functions.channels.LeaveChannelRequest(
                    types.InputChannel(int(channel), info["access_hash"]))), timeout=30)
            except UserNotParticipantError:
                self.remove_membership(account, channel, acc)
                if not automatic:
                    raise ValueError("Аккаунт уже вышел. Список обновлён, можно повторить быстрый выход.") from None
                return
            except FloodWaitError as exc:
                self.cooldown[account] = now + max(1, exc.seconds)
                if automatic and self.rules.get(channel) is rule:
                    rule["due"] = now + max(RETRY_SECONDS, exc.seconds)
                self.save()
                raise ValueError(f"Telegram просит подождать {exc.seconds} сек.") from None
            except (RPCError, OSError, ValueError) as exc:
                LOG.warning("Leave channel %s failed for Telegram ID %s: %s", channel, account, type(exc).__name__)
                raise ValueError("Выход не подтверждён Telegram. Обнови список перед повтором.") from None
            self.remove_membership(account, channel, acc)
            return " ".join(filter(None, (acc.me.first_name, acc.me.last_name))) or acc.me.username or acc.name

    async def avatar(self, request):
        async with self.image_limit:
            return await self.channel_avatar(request.match_info["channel"])

    async def channel_avatar(self, channel):
        members = self.channel_members().get(channel, {})
        if not members:
            raise web.HTTPNotFound()
        photo = max(members.values(), key=lambda row: row["seen"])["photo"]
        if not photo:
            raise web.HTTPNotFound()
        key = (channel, photo)
        if key not in self.avatars:
            connected = self.connected()
            entry = next(((connected[account], info) for account, info in members.items()
                          if account in connected and info["photo"] == photo and info["access_hash"] is not None), None)
            if entry is None:
                raise web.HTTPNotFound()
            try:
                image = await asyncio.wait_for(entry[0].client.download_profile_photo(
                    types.InputPeerChannel(int(channel), entry[1]["access_hash"]), file=bytes), timeout=15)
            except (RPCError, OSError, ValueError):
                raise web.HTTPNotFound() from None
            if len(self.avatars) >= 256:
                self.avatars.pop(next(iter(self.avatars)))
            self.avatars[key] = image
        if not self.avatars[key]:
            raise web.HTTPNotFound()
        return web.Response(body=self.avatars[key], content_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

    async def run(self):
        while True:
            await asyncio.sleep(15)
            try:
                now = self.clock()
                if any(self.scan_due(account, now) for account in self.connected()):
                    self.request_scan()
                due = [channel for channel, rule in self.rules.items() if rule["due"] <= now]
                for channel in due:
                    try:
                        await self.leave_one(channel, automatic=True)
                    except ValueError as exc:
                        LOG.info("Automatic leave %s: %s", channel, exc)
            except Exception:
                LOG.exception("Subscription scheduler failed")

    async def lifecycle(self, app):
        task = asyncio.create_task(self.run())
        yield
        tasks = [task] + ([self.scan] if self.scan else [])
        for active in tasks:
            active.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, request):
        account = None
        try:
            command = request.match_info.get("command")
            if request.method == "POST":
                if command == "refresh":
                    self.request_scan()
                else:
                    channel = request.match_info["channel"]
                    if command == "leave":
                        account = await self.leave_one(channel)
                    elif command == "auto":
                        data = await request.json()
                        self.configure(channel, data.get("enabled") if isinstance(data, dict) else None)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400, headers={"Cache-Control": "no-store"})
        return web.json_response(self.snapshot() | {"left": account}, headers={"Cache-Control": "no-store"})

    def register(self, app):
        app.cleanup_ctx.append(self.lifecycle)
        app.router.add_get("/api/subscriptions", self.handle)
        app.router.add_post("/api/subscriptions/{command:refresh}", self.handle)
        app.router.add_post(r"/api/subscriptions/{channel:\d+}/{command:auto|leave}", self.handle)
        app.router.add_get(r"/api/subscriptions/{channel:\d+}/avatar", self.avatar)
