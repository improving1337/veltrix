"""Preview and schedule selected profile fields from an accessible group."""
import asyncio
import json
import logging
import math
import random
import re
import secrets
import shutil
import time
from pathlib import Path

from aiohttp import web
from telethon import functions, types
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameOccupiedError

from channels import parse_link
from template_library import atomic_json_save

DRAFT_TTL = 20 * 60
DAY = 24 * 60 * 60
ACTIVE_STATES = {'waiting', 'running'}
FIELDS = {'username', 'birthday', 'bio', 'avatars'}
STATIC_PHOTO_SIZES = (types.PhotoSize, types.PhotoSizeProgressive, types.PhotoCachedSize,
                      types.PhotoStrippedSize)
LOG = logging.getLogger(__name__)


class AvatarUnavailable(ValueError):
    pass


def copy_options(data):
    selected = data.get('fields') if isinstance(data, dict) else None
    if not isinstance(selected, list) or not selected or any(
            not isinstance(key, str) or key not in FIELDS for key in selected):
        raise ValueError('Выберите хотя бы одно поле для копирования')
    group = data.get('group')
    if not isinstance(group, str) or not group.strip():
        raise ValueError('Укажите ссылку на группу Telegram')
    kind, key = parse_link(group)
    if 'avatars' not in selected:
        return kind, key, set(selected), None, None
    count = str(data.get('avatar_count', '')).strip()
    if not re.fullmatch(r'[0-9]{1,10}', count) or not 1 <= int(count) < 2**31 - 1:
        raise ValueError('Укажите целое количество аватарок больше нуля')
    count = int(count)
    if count == 1:
        return kind, key, set(selected), count, None
    try:
        delay = tuple(float(str(data.get(k, d)).replace(',', '.'))
                      for k, d in [('delay_from_days', 1), ('delay_to_days', 2)])
    except ValueError:
        raise ValueError('Укажите интервал между аватарками в днях') from None
    if not all(math.isfinite(n * DAY) and n > 0 for n in delay) or delay[0] > delay[1]:
        raise ValueError('Задержка должна быть больше нуля, а «от» — не больше «до»')
    return kind, key, set(selected), count, delay


async def group_entity(client, kind, key):
    if kind == 'invite':
        invite = await client(functions.messages.CheckChatInviteRequest(key))
        if not isinstance(invite, types.ChatInviteAlready):
            raise ValueError('Этот аккаунт должен уже состоять в группе по приглашению')
        entity = invite.chat
    else:
        entity = await client.get_entity(key)
    if not (isinstance(entity, types.Chat) or isinstance(entity, types.Channel)
            and (entity.megagroup or entity.gigagroup)):
        raise ValueError('Укажите ссылку на группу Telegram')
    return entity


async def available_username(client, original):
    base = original[:26].rstrip('_')
    for _ in range(5):
        candidate = f'{base}_{secrets.randbelow(90000) + 10000}'
        try:
            if await client(functions.account.CheckUsernameRequest(candidate)):
                return candidate
        except (UsernameInvalidError, UsernameOccupiedError):
            continue
    return None


def birthday_text(birthday):
    if birthday is None:
        return ''
    value = f'{birthday.day:02d}.{birthday.month:02d}'
    return f'{value}.{birthday.year}' if birthday.year else value


class ProfileCopies:
    def __init__(self, directory, accounts, prepare_avatar, upload_avatar, apply_fields, clock=time.time):
        self.root = Path(directory)
        self.photos = self.root / 'photos'
        self.path = self.root / 'state.json'
        self.accounts = accounts
        self.prepare_avatar = prepare_avatar
        self.upload_avatar = upload_avatar
        self.apply_fields = apply_fields
        self.clock = clock
        self.drafts = {}
        self.preview_lock = asyncio.Lock()
        try:
            state = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            state = {'used_sources': [], 'jobs': {}}
        self.used_sources = set(state['used_sources'])
        self.jobs = state['jobs']
        interrupted = {target: {**job, 'state': 'interrupted', 'values': {},
                                'errors': [*job['errors'], 'Операция прервана. Проверьте профиль в Telegram.']}
                       for target, job in self.jobs.items() if job['state'] == 'running'}
        if interrupted:
            self.save(self.jobs | interrupted, self.used_sources)
        if self.photos.is_dir():
            keep = {job['token'] for job in self.jobs.values() if job['state'] == 'waiting'}
            for path in self.photos.iterdir():
                if path.is_dir() and re.fullmatch(r'[0-9a-f]{32}', path.name) and path.name not in keep:
                    self.remove_photos(path.name)

    def save(self, jobs, used):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json_save(self.path, {'used_sources': sorted(used), 'jobs': jobs})
        self.jobs, self.used_sources = jobs, used

    def put(self, job):
        self.save(self.jobs | {job['target']: job}, self.used_sources)

    def remove_photos(self, token):
        try:
            shutil.rmtree(self.photos / token)
        except FileNotFoundError:
            pass
        except OSError:
            LOG.warning('Unable to remove cached profile photos for job %s', token)

    def expire(self):
        now = self.clock()
        for token, draft in list(self.drafts.items()):
            if draft['expires'] <= now:
                self.drafts.pop(token)
                self.remove_photos(token)

    @staticmethod
    def target(acc):
        return str(acc.me.id)

    def check_idle(self, target):
        job = self.jobs.get(target)
        if job and job['state'] in ACTIVE_STATES:
            raise ValueError('У аккаунта уже есть очередь изменений профиля')

    async def download_avatar_still(self, client, photo):
        sizes = [size for size in (getattr(photo, 'sizes', None) or ())
                 if isinstance(size, STATIC_PHOTO_SIZES)]
        sizes.sort(key=lambda size: getattr(size, 'w', 0) * getattr(size, 'h', 0), reverse=True)
        # A Photo can also contain MP4 variants. An explicit still thumbnail
        # type keeps Telethon from choosing a video when downloading it.
        for size in sizes:
            data = await client.download_media(photo, file=bytes, thumb=size.type)
            if data:
                try:
                    return await asyncio.to_thread(self.prepare_avatar, data)
                except ValueError:
                    continue  # Try the next still size if Telegram returned unusable bytes.
        raise AvatarUnavailable('У одной из аватарок нет доступного изображения')

    async def select_source(self, acc, kind, key, selected, count, token):
        entity = await group_entity(acc.client, kind, key)
        excluded = self.used_sources | {draft['source_id'] for draft in self.drafts.values()}
        excluded = excluded | {self.target(a) for a in self.accounts().values() if a.me is not None}
        users = [user async for user in acc.client.iter_participants(entity, limit=300)
                 if str(user.id) not in excluded and not user.bot and not user.deleted
                 and ('username' not in selected or user.username)]
        random.shuffle(users)
        unusable_photos = False
        for user in users[:60]:
            values = {}
            if selected & {'bio', 'birthday'}:
                full = (await acc.client(functions.users.GetFullUserRequest(user))).full_user
                if 'bio' in selected:
                    bio = getattr(full, 'about', '') or ''
                    if not bio:
                        continue
                    values['bio'] = bio
                if 'birthday' in selected:
                    birthday = birthday_text(getattr(full, 'birthday', None))
                    if not birthday:
                        continue
                    values['birthday'] = birthday
            photos = []
            if count is not None:
                photos = await acc.client.get_profile_photos(user, limit=count + 1)
                if len(photos) != count or (getattr(photos, 'total', None) or count) > count:
                    continue
            if 'username' in selected:
                username = await available_username(acc.client, user.username)
                if not username:
                    continue
                values['username'] = username
            if photos:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                folder = self.photos / token
                folder.mkdir(parents=True, mode=0o700)
                try:
                    for index, photo in enumerate(reversed(photos)):
                        image = await self.download_avatar_still(acc.client, photo)
                        (folder / f'{index}.jpg').write_bytes(image)
                except AvatarUnavailable:
                    self.remove_photos(token)
                    unusable_photos = True
                    continue
            return user, values, photos
        if unusable_photos:
            raise ValueError('Подходящие участники найдены, но скачать все их аватарки не удалось')
        if count is not None:
            raise ValueError(f'В доступной выборке нет нового участника с нужным количеством '
                             f'аватарок ({count}) и выбранными полями')
        raise ValueError('В доступной выборке нет нового участника со всеми выбранными полями')

    async def preview(self, acc, data):
        kind, key, selected, count, delay = copy_options(data)
        target = self.target(acc)
        async with self.preview_lock:
            self.expire()
            self.check_idle(target)
            for token, draft in list(self.drafts.items()):
                if draft['target'] == target:
                    self.drafts.pop(token)
                    self.remove_photos(token)
            token = secrets.token_hex(16)
            try:
                source, values, photos = await asyncio.wait_for(
                    self.select_source(acc, kind, key, selected, count, token), 120)
                delays = [0] + [random.uniform(*delay) * DAY for _ in photos[1:]] if photos else []
                draft = {'token': token, 'target': target, 'source_id': str(source.id), 'values': values,
                         'delays': delays, 'expires': self.clock() + DRAFT_TTL}
                self.drafts[token] = draft
            except FloodWaitError as exc:
                raise ValueError(f'Повторите поиск через {exc.seconds} сек.') from None
            except asyncio.TimeoutError:
                raise ValueError('Telegram не успел вернуть данные для предпросмотра') from None
            finally:
                if token not in self.drafts:
                    self.remove_photos(token)
        return draft

    def draft_for(self, acc, token):
        self.expire()
        draft = self.drafts.get(token)
        if draft is None or draft['target'] != self.target(acc):
            raise ValueError('Предпросмотр истёк. Создайте новый.')
        return draft

    def discard(self, acc, token):
        self.draft_for(acc, token)
        self.drafts.pop(token)
        self.remove_photos(token)

    async def confirm(self, acc, token):
        draft = self.draft_for(acc, token)
        target = draft['target']
        self.check_idle(target)
        if draft['source_id'] in self.used_sources:
            raise ValueError('Этот участник уже использован. Создайте новый предпросмотр.')
        job = {key: draft[key] for key in ('token', 'target', 'source_id', 'values', 'delays')}
        job.update(state='waiting', due=self.clock(), uploaded=0, errors=[])
        self.save(self.jobs | {target: job}, self.used_sources | {draft['source_id']})
        self.drafts.pop(token)
        await self.advance(target, acc)
        return self.status(acc)

    async def advance(self, target, acc):
        job = self.jobs.get(target)
        if not job or job['state'] != 'waiting' or job['due'] > self.clock():
            return
        job = {**job, 'state': 'running'}
        self.put(job)
        uploaded = job['uploaded']
        errors = list(job['errors'])
        delays = job['delays']
        try:
            if job['values']:
                errors.extend(await self.apply_fields(acc, job['values']))
            if uploaded < len(delays):
                data = (self.photos / job['token'] / f'{uploaded}.jpg').read_bytes()
                await asyncio.wait_for(self.upload_avatar(acc, data), 120)
                uploaded += 1
        except Exception as exc:
            errors.append(f'Последовательность остановлена: {exc or type(exc).__name__}. Проверьте профиль в Telegram.')
            state, due = 'failed', None
        else:
            state = 'waiting' if uploaded < len(delays) else 'done'
            due = self.clock() + delays[uploaded] if state == 'waiting' else None
        self.put({**job, 'values': {}, 'uploaded': uploaded, 'due': due, 'state': state, 'errors': errors})
        if state != 'waiting':
            self.remove_photos(job['token'])

    def status(self, acc):
        job = self.jobs.get(self.target(acc))
        if not job:
            return None
        return {**{key: job[key] for key in ('source_id', 'state', 'due', 'uploaded', 'errors')},
                'total': len(job['delays'])}

    async def run(self):
        while True:
            self.expire()
            connected = {self.target(acc): acc for acc in self.accounts().values() if acc.me is not None}
            for target, acc in connected.items():
                await self.advance(target, acc)
            await asyncio.sleep(15)

    def register(self, app, get_account):
        def json_response(payload):
            return web.json_response(payload, headers={'Cache-Control': 'no-store'})

        def bad_request(exc):
            return web.HTTPBadRequest(text=json.dumps({'error': str(exc)}, ensure_ascii=False),
                                      content_type='application/json')

        async def preview(request):
            acc = get_account(request)
            try:
                draft = await self.preview(acc, await request.json())
            except ValueError as exc:
                raise bad_request(exc) from None
            return json_response({key: draft[key] for key in ('token', 'source_id', 'values', 'delays', 'expires')})

        async def image(request):
            token = request.match_info['token']
            try:
                draft = self.draft_for(get_account(request), token)
            except ValueError:
                raise web.HTTPNotFound() from None
            index = int(request.match_info['index'])
            if index >= len(draft['delays']):
                raise web.HTTPNotFound()
            return web.FileResponse(self.photos / token / f'{index}.jpg',
                                    headers={'Cache-Control': 'no-store'})

        async def confirm(request):
            try:
                job = await self.confirm(get_account(request), request.match_info['token'])
            except ValueError as exc:
                raise bad_request(exc) from None
            return json_response(job)

        async def discard(request):
            try:
                self.discard(get_account(request), request.match_info['token'])
            except ValueError as exc:
                raise bad_request(exc) from None
            return json_response({'ok': True})

        async def status(request):
            return json_response(self.status(get_account(request)))

        base = '/api/accounts/{acc}/profile-copy'
        plan = base + r'/{token:[a-f0-9]{32}}'
        app.add_routes([web.get(base, status), web.post(base, preview), web.post(plan, confirm),
                        web.delete(plan, discard), web.get(plan + r'/photos/{index:\d+}', image)])
