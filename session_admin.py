"""Session provenance, inventory and sequential replacement of authorized accounts."""
import asyncio
import hashlib
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from aiohttp import web
from telethon import TelegramClient, functions, errors
from account_security import private_json, read_json
from account_deploy import publish_session


def fingerprint(client):
    key = getattr(getattr(client.session, 'auth_key', None), 'key', None)
    return hashlib.sha256(key).hexdigest() if key else None


class SessionAdmin:
    def __init__(self, root, accounts, security, connect_new, retire, factory=TelegramClient):
        self.root, self.accounts, self.security = Path(root), accounts, security
        self.connect_new, self.retire, self.factory = connect_new, retire, factory
        self.path = self.root/'security'/'session_inventory.json'
        self.records = read_json(self.path)
        self.job = None
        self.task = None
        self.pending = {}
        self.date_tasks = {}
        self.date_semaphore = asyncio.Semaphore(4)
        self.lock = asyncio.Lock()

    def save(self):
        private_json(self.path, self.records)

    def owned(self, account):
        record = self.records.get(account.name, {})
        return bool(record.get('owned_key') and record['owned_key'] == fingerprint(account.client))

    def mark_owned(self, account):
        record = self.records.setdefault(account.name, {'received_at': time.time()})
        key = fingerprint(account.client)
        if not key:
            raise RuntimeError('MissingAuthorizationKey')
        record['owned_key'] = key
        self.save()

    async def call(self, operation):
        return await asyncio.wait_for(operation, 40)

    async def inventory(self, request):
        rows = []
        for account in list(self.accounts().values()):
            record = self.records.setdefault(account.name, {'received_at': time.time()})
            me = account.me
            working = bool(me and account.client.is_connected())
            if working:
                record['phone'], record['telegram_id'] = me.phone or '', str(me.id)
                if time.time()-record.get('checked_at', 0) > 60 and account.name not in self.date_tasks:
                    record['checked_at'] = time.time()
                    task = asyncio.create_task(self.refresh_date(account, record))
                    self.date_tasks[account.name] = task
                    task.add_done_callback(lambda task, name=account.name: self.date_tasks.pop(name, None))
            identity = str(me.id) if me else record.get('telegram_id')
            saved = self.security.passwords.get(identity, {})
            rows.append({'name': account.name, 'phone': getattr(me, 'phone', '') or record.get('phone', ''),
                         'owned': self.owned(account), 'working': working,
                         'password': saved.get('password'), 'session_date': record.get('session_date'),
                         'received_at': record['received_at'],
                         'status': record.get('status', ''), 'needs_password': account.name in self.pending})
        self.save()
        return web.json_response({'items': rows, 'job': self.job, 'protection_enabled': self.security.state.get('enabled', True)}, headers={'Cache-Control': 'no-store'})

    async def refresh_date(self, account, record):
        async with self.date_semaphore:
            try:
                auths = await self.call(account.client(functions.account.GetAuthorizationsRequest()))
                current = next(a for a in auths.authorizations if a.current)
                record['session_date'] = current.date_created.isoformat()
                self.save()
            except Exception:
                pass

    async def cleanup_client(self, item):
        try:
            await asyncio.wait_for(item['client'].disconnect(), 10)
        finally:
            item['client'].session.close()
            shutil.rmtree(item['directory'], ignore_errors=True)

    async def replace(self, source):
        if self.owned(source) or source.me is None:
            return
        record = self.records.setdefault(source.name, {'received_at': time.time()})
        if source.name in self.pending:
            return
        if record.get('retry_at', 0) > time.time():
            return
        self.security.suspended.add(source.name)
        directory = Path(tempfile.mkdtemp(prefix='reissue-', dir=self.root/'temp'))
        item = {'directory': directory, 'source': source, 'expires': time.time()+600}
        try:
            client = self.factory(str(directory/'new'), 2040, 'b18441a1ff607e10a989891a5462e627', flood_sleep_threshold=0)
            item['client'] = client
            me = await self.call(source.client.get_me())
            if not me or me.id != source.me.id or not me.phone:
                raise RuntimeError('PhoneUnavailable')
            # Only messages newer than this request and newer than the previous service message qualify.
            previous = await self.call(source.client.get_messages(777000, limit=1))
            last_id = previous[0].id if previous else 0
            requested_at = datetime.now(timezone.utc)
            await self.call(client.connect())
            sent = await self.call(client.send_code_request('+'+me.phone.lstrip('+')))
            record['status'] = 'Ожидание нового кода Telegram'
            self.save()
            code = None
            end = time.monotonic()+90
            while time.monotonic() < end:
                messages = await self.call(source.client.get_messages(777000, limit=5, min_id=last_id))
                for message in reversed(messages):
                    stamp = message.date
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    text = message.message or ''
                    if message.id <= last_id or stamp < requested_at.replace(microsecond=0) or getattr(message, 'out', False):
                        continue
                    match = re.search(r'(?:Login code|Код (?:для входа|подтверждения))\s*:\s*(\d{4,8})\b', text, re.I)
                    if match:
                        code = match.group(1)
                        break
                if code:
                    break
                await asyncio.sleep(2)
            if not code:
                raise RuntimeError('CodeNotReceivedUseDeploy')
            try:
                await self.call(client.sign_in(phone=me.phone, code=code, phone_code_hash=sent.phone_code_hash))
            except errors.SessionPasswordNeededError:
                password = self.security.passwords.get(str(me.id), {}).get('password')
                if password:
                    try:
                        await self.call(client.sign_in(password=password))
                    except errors.PasswordHashInvalidError:
                        password = None
                if not password:
                    self.pending[source.name] = item
                    record['status'] = 'Нужен текущий пароль 2FA'
                    self.save()
                    return
            await self.finish(item)
        finally:
            if source.name not in self.pending:
                self.security.suspended.discard(source.name)
            if source.name not in self.pending and 'client' in item:
                await self.cleanup_client(item)
            elif 'client' not in item:
                shutil.rmtree(directory, ignore_errors=True)

    async def finish(self, item):
        source, client = item['source'], item['client']
        if self.accounts().get(source.name) is not source:
            raise RuntimeError('SourceAccountRemoved')
        me = await self.call(client.get_me())
        if not me or me.id != source.me.id or fingerprint(client) == fingerprint(source.client):
            raise RuntimeError('IndependentAuthorizationNotConfirmed')
        await self.call(client.disconnect())
        client.session.close()
        name = 'own_'+str(me.id)+'_'+uuid.uuid4().hex[:8]
        config = self.root/'Account'/(name+'.json')
        private_json(config, {'app_id': 2040, 'app_hash': 'b18441a1ff607e10a989891a5462e627'}, exclusive=True)
        try:
            publish_session(item['directory']/'new.session', self.root/'Session'/(name+'.session'))
        except Exception:
            config.unlink(missing_ok=True)
            raise
        await self.connect_new()
        new = self.accounts().get(name)
        if not new or not new.me or new.me.id != me.id:
            self.records.setdefault(source.name, {})['status'] = 'Новая сессия сохранена, подключение не подтверждено; исходная сохранена'
            self.save()
            return
        self.mark_owned(new)
        await self.retire(source)
        self.records[new.name]['status'] = 'Новая сессия работает; отзыв старых контролируется фоновой защитой'
        self.save()
        # Reset attempts use the new client only, obeying Telegram's fresh-session restriction.
        self.security.state.setdefault('accounts', {}).pop(new.name, None)
        self.security.save()

    async def run(self):
        self.job = {'running': True, 'done': 0, 'errors': 0}
        try:
            for source in list(self.accounts().values()):
                if self.owned(source) or not source.me:
                    continue
                try:
                    async with self.security.lock:
                        await self.replace(source)
                    self.job['done'] += 1
                except asyncio.CancelledError:
                    raise
                except errors.FloodWaitError as exc:
                    self.records.setdefault(source.name, {})['retry_at'] = time.time()+exc.seconds
                    self.records[source.name]['status'] = f'Ограничение Telegram: повтор не раньше чем через {exc.seconds} сек.'
                    self.job['errors'] += 1
                except Exception as exc:
                    self.records.setdefault(source.name, {})['status'] = 'Не перевыпущена: '+type(exc).__name__
                    self.job['errors'] += 1
                self.save()
        finally:
            self.job['running'] = False

    async def start(self, request):
        async with self.lock:
            if self.task and not self.task.done():
                return web.json_response({'error': 'Перевыпуск уже выполняется'}, status=409)
            self.task = asyncio.create_task(self.run())
        return web.json_response({'started': True})

    async def password(self, request):
        data = await request.json()
        name = str(data.get('name', ''))
        async with self.lock, self.security.lock:
            item = self.pending.get(name)
            if not item or time.time() >= item['expires']:
                return web.json_response({'error': 'Запрос истёк или отсутствует'}, status=410)
            try:
                await self.call(item['client'].sign_in(password=str(data.get('password', ''))))
            except errors.PasswordHashInvalidError:
                return web.json_response({'error': 'Неверный пароль 2FA'}, status=400)
            await self.finish(item)
            self.pending.pop(name, None)
            self.security.suspended.discard(name)
            await self.cleanup_client(item)
        return web.json_response({'ok': True})

    async def cleanup(self):
        async with self.lock:
            for name, item in list(self.pending.items()):
                if item['expires'] <= time.time():
                    self.pending.pop(name)
                    self.security.suspended.discard(name)
                    await self.cleanup_client(item)

    async def close(self):
        for task in self.date_tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.date_tasks.values()), return_exceptions=True)
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for item in self.pending.values():
            await self.cleanup_client(item)
        self.pending.clear()

    def register(self, app):
        app.router.add_get('/api/2fa', self.inventory)
        app.router.add_post('/api/2fa/reissue', self.start)
        app.router.add_post('/api/2fa/password', self.password)
