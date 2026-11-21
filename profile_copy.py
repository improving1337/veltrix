"""Preview and schedule selected profile fields from an accessible group."""
import asyncio
import base64
import hashlib
import json
import logging
import math
import random
import re
import secrets
import shutil
import time
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from aiohttp import web
from telethon import functions, types
from telethon.errors import (FloodPremiumWaitError, FloodWaitError, RPCError, UserIdInvalidError,
                             UsernameInvalidError, UsernameOccupiedError, UsernamePurchaseAvailableError)

from channels import parse_link
from template_library import atomic_json_save

DRAFT_TTL = 20 * 60
SEARCH_TIMEOUT = 300
SEARCH_REQUEST_INTERVAL = 0.2
SEARCH_STATES = {'queued', 'searching', 'waiting'}
MAX_SEARCH_MEMBERS = 300
DAY = 24 * 60 * 60
ACTIVE_STATES = {'waiting', 'running'}
FIELDS = {'username', 'birthday', 'bio', 'avatars', 'music'}
DEFAULT_GROUP = 'https://t.me/minecraft_beseda764'
STATIC_PHOTO_SIZES = (types.PhotoSize, types.PhotoSizeProgressive, types.PhotoCachedSize,
                      types.PhotoStrippedSize)
LOG = logging.getLogger(__name__)


class AvatarUnavailable(ValueError):
    pass


def search_workers(data):
    value = data.get('workers', 1)
    if type(value) is not int or value < 1:
        raise ValueError('Число аккаунтов для поиска должно быть целым и не меньше 1')
    return value


def copy_options(data):
    selected = data.get('fields') if isinstance(data, dict) else None
    if not isinstance(selected, list) or any(
            not isinstance(key, str) or key not in FIELDS for key in selected):
        raise ValueError('Выберите хотя бы одно поле для копирования')
    direct = data.get('mode') == 'profile'
    group = str(data.get('group') or '').strip()
    if not group and direct:
        raise ValueError('Укажите ссылку на профиль')
    group = group or DEFAULT_GROUP
    kind, key = parse_link(group)
    if direct and kind != 'public':
        raise ValueError('Для конкретного профиля нужна ссылка t.me/username')
    if direct:
        kind = 'profile'
    legacy = data.get('avatar_count')
    bounds = (data.get('avatar_min', legacy if legacy is not None else 3), data.get('avatar_max', legacy if legacy is not None else 5))
    normalized = [str(n).strip() for n in bounds]
    if any(not re.fullmatch(r'[0-9]{1,10}', n) for n in normalized):
        raise ValueError('Укажите целые границы количества аватарок')
    low, high = map(int, normalized)
    if not 1 <= low <= high < 2**31 - 1:
        raise ValueError('Количество: от 1, верхняя граница должна быть не меньше нижней')
    count = (low, high)
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


def username_parts(name):
    split = re.sub(r'([a-z])([A-Z])', r'\1_\2', name)
    return re.findall(r'[a-z]+', split.casefold(), flags=re.ASCII)


class UsernameDiversity:
    """Index names for reuse and obvious lexical similarity checks."""

    def __init__(self, names):
        self.names = set()
        self.edges = set()
        self.words = set()
        self.grams = defaultdict(set)
        for name in names:
            self.add(name)

    @staticmethod
    def features(name):
        parts = username_parts(name)
        compact = ''.join(parts)
        words = set(word for word in parts if len(parts) > 1 and len(word) >= 4)
        grams = {compact[i:i+3] for i in range(len(compact) - 2)}
        return compact, words, grams

    def add(self, name):
        compact, words, grams = self.features(name)
        if not compact:
            return
        self.names.add(compact)
        self.edges.update((('start', compact[:4]), ('end', compact[-4:])))
        self.words.update(words)
        for gram in grams:
            self.grams[gram].add(compact)

    def allows(self, name):
        compact, words, grams = self.features(name)
        if (compact in self.names or words & self.words
                or ('start', compact[:4]) in self.edges or ('end', compact[-4:]) in self.edges):
            return False
        shared_grams = Counter(other for gram in grams for other in self.grams.get(gram, ()))
        for other, overlap in shared_grams.items():
            shorter = min(len(compact), len(other))
            if overlap >= 4 and overlap * 3 >= 2 * min(len(grams), len(other) - 2):
                return False
            match = SequenceMatcher(None, compact, other, autojunk=False)
            shared = match.find_longest_match().size
            if shared >= max(5, (shorter * 2 + 2) // 3) or match.ratio() >= .78:
                return False
        return True


def username_candidates(original, target_id):
    """Mix source-based variants with independent names when stems collide."""
    words = username_parts(original)
    if not words:
        return []
    compact = ''.join(words)
    seed = hashlib.blake2b(f'{original.casefold()}:{target_id}'.encode(), digest_size=16).digest()
    rng = random.Random(seed)

    def coined():
        syllables = []
        onsets = ('', 'b', 'c', 'd', 'f', 'g', 'h', 'k', 'l', 'm', 'n', 'p', 'r', 's',
                  't', 'v', 'w', 'z', 'br', 'cl', 'dr', 'fr', 'gr', 'sh', 'st', 'tr')
        vowels = ('a', 'e', 'i', 'o', 'u', 'ai', 'ea', 'io', 'ou')
        for _ in range(rng.choice((2, 2, 3, 3, 4))):
            syllables.append(rng.choice(onsets) + rng.choice(vowels) + rng.choice(('', '', '', 'n', 'r', 's', 'l')))
        return ''.join(syllables)

    root_forms = [compact]
    if len(words) > 1:
        root_forms.append(''.join(reversed(words)))
    rng.shuffle(root_forms)
    alternatives = []
    for index in range(90):
        fresh = coined()
        if index < len(root_forms):
            alternatives.append(root_forms[index])
        if index < 8:
            root = rng.choice(words) if len(words) > 1 else compact
            stem_space = 32 - len(fresh)
            if index % 2:
                alternatives.append(f'{fresh}{root[:stem_space]}')
            else:
                alternatives.append(f'{root[:stem_space]}{fresh}')
        alternatives.append(fresh)
    return [name for name in dict.fromkeys(alternatives) if name != original.casefold()
            and re.fullmatch(r'[a-z]{5,32}', name)]


async def available_username(client, original, target_id, read, reserved=()):
    diversity = reserved if isinstance(reserved, UsernameDiversity) else UsernameDiversity(reserved)
    checked = 0
    for candidate in username_candidates(original, target_id):
        if not diversity.allows(candidate):
            continue
        checked += 1
        try:
            if await read(client, functions.account.CheckUsernameRequest(candidate)):
                return candidate
        except (UsernameInvalidError, UsernameOccupiedError, UsernamePurchaseAvailableError):
            pass
        if checked == 5:
            break
    return None


def birthday_text(birthday):
    if birthday is None:
        return ''
    value = f'{birthday.day:02d}.{birthday.month:02d}'
    return f'{value}.{birthday.year}' if birthday.year else value


def music_reference(doc):
    return {'id': doc.id, 'access_hash': doc.access_hash,
            'file_reference': base64.b64encode(doc.file_reference).decode()}


async def profile_music(client, user):
    songs = []
    offset = 0
    while True:
        page = await client(functions.users.GetSavedMusicRequest(user, offset=offset, limit=100, hash=0))
        documents = getattr(page, 'documents', ())
        songs.extend(music_reference(doc) for doc in documents if isinstance(doc, types.Document))
        offset += len(documents)
        if len(documents) < 100 or offset >= getattr(page, 'count', offset):
            return songs


def profile_signature(username, bio, birthday):
    return {'username': re.sub(r'_[0-9]{5}$', '', (username or '').casefold()),
            'bio': ' '.join((bio or '').casefold().split()), 'birthday': birthday or ''}


def similar_profile(user, bio, birthday, accounts, signatures):
    target = profile_signature(user.username, bio, birthday)
    known = list(signatures)
    for acc in accounts:
        meta = getattr(acc, 'copy_profile_meta', None) or {}
        known.append(profile_signature(acc.me.username, meta.get('bio'), meta.get('birthday')))
    return signature_matches(target, known)


def signature_matches(target, signatures):
    return any(sum(bool(value) and value == other.get(key) for key, value in target.items()) >= 2
               or bool(target['username'] and target['username'] == other.get('username'))
               for other in signatures)


class ProfileCopies:
    def __init__(self, directory, accounts, prepare_avatar, upload_avatar, apply_fields, clock=time.time, clear_photos=None):
        self.root = Path(directory)
        self.photos = self.root / 'photos'
        self.path = self.root / 'state.json'
        self.accounts = accounts
        self.prepare_avatar = prepare_avatar
        self.upload_avatar = upload_avatar
        self.apply_fields = apply_fields
        self.clear_photos = clear_photos
        self.clock = clock
        self.drafts = {}
        self.searches = {}
        self.blocked_until = {}
        self.preview_lock = asyncio.Lock()
        try:
            state = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            state = {'used_sources': [], 'jobs': {}}
        self.used_sources = set(state['used_sources'])
        self.jobs = state['jobs']
        self.used_usernames = set(state.get('used_usernames', []))
        self.used_usernames.update(job['values']['username'] for job in self.jobs.values()
                                   if job.get('values', {}).get('username'))
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

    def save(self, jobs, used, usernames=None):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        usernames = self.used_usernames if usernames is None else usernames
        atomic_json_save(self.path, {'used_sources': sorted(used), 'jobs': jobs,
                                    'used_usernames': sorted(usernames)})
        self.jobs, self.used_sources, self.used_usernames = jobs, used, usernames

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
        for target, search in list(self.searches.items()):
            if search['state'] not in SEARCH_STATES and search['expires'] <= now:
                self.searches.pop(target)

    @staticmethod
    def target(acc):
        return str(acc.me.id)

    def username_diversity(self, except_token=None):
        names = list(self.used_usernames)
        names.extend(a.me.username or '' for a in self.accounts().values() if a.me is not None)
        names.extend(draft['values'].get('username', '') for token, draft in self.drafts.items()
                     if token != except_token)
        return UsernameDiversity(names)

    def signatures(self):
        return [job['signature'] for job in self.jobs.values() if 'signature' in job]

    def check_idle(self, target):
        job = self.jobs.get(target)
        if job and job['state'] in ACTIVE_STATES:
            raise ValueError('У аккаунта уже есть очередь изменений профиля')

    async def download_avatar_still(self, client, photo, read):
        sizes = [size for size in (getattr(photo, 'sizes', None) or ())
                 if isinstance(size, STATIC_PHOTO_SIZES)]
        sizes.sort(key=lambda size: getattr(size, 'w', 0) * getattr(size, 'h', 0), reverse=True)
        # A Photo can also contain MP4 variants. An explicit still thumbnail
        # type keeps Telethon from choosing a video when downloading it.
        for size in sizes:
            data = await read(client.download_media, photo, file=bytes, thumb=size.type)
            if data:
                try:
                    return await asyncio.to_thread(self.prepare_avatar, data)
                except ValueError:
                    continue  # Try the next still size if Telegram returned unusable bytes.
        raise AvatarUnavailable('У одной из аватарок нет доступного изображения')

    async def group_users(self, client, entity, read):
        if not isinstance(entity, types.Channel):
            async def members():
                return [user async for user in client.iter_participants(entity, limit=MAX_SEARCH_MEMBERS)]
            return await read(members)
        peer = await read(client.get_input_entity, entity)
        users = []
        offset = 0
        while offset < MAX_SEARCH_MEMBERS:
            page = await read(client, functions.channels.GetParticipantsRequest(
                channel=peer, filter=types.ChannelParticipantsSearch(''), offset=offset,
                limit=min(100, MAX_SEARCH_MEMBERS - offset), hash=0))
            found = {user.id: user for user in page.users}
            for member in page.participants:
                user = found.get(getattr(member, 'user_id', None))
                if user is not None:
                    users.append(user)
            offset += len(page.participants)
            if not page.participants or (page.count and offset >= page.count):
                break
        return users

    async def select_source(self, acc, kind, key, selected, count, token, search):
        target = self.target(acc)
        next_request = {}
        read_locks = {}

        async def read(operation, *args, reader=acc, **kwargs):
            reader_id = self.target(reader)
            loop = asyncio.get_running_loop()
            lock = read_locks.setdefault(reader_id, asyncio.Lock())
            async with lock:
                while True:
                    until = self.blocked_until.get(reader_id, 0)
                    if (seconds := until - loop.time()) > 0:
                        search.update(state='waiting', retry_at=self.clock() + seconds)
                        await asyncio.sleep(seconds)
                    search.update(state='searching', retry_at=None)
                    if (seconds := next_request.get(reader_id, 0) - loop.time()) > 0:
                        await asyncio.sleep(seconds)
                    next_request[reader_id] = loop.time() + SEARCH_REQUEST_INTERVAL
                    try:
                        return await operation(*args, **kwargs)
                    except (FloodWaitError, FloodPremiumWaitError) as exc:
                        self.blocked_until[reader_id] = loop.time() + max(1, exc.seconds) + 1

        excluded = self.used_sources | {draft['source_id'] for draft in self.drafts.values()}
        local = [a for a in self.accounts().values() if a.me is not None]
        excluded = excluded | {self.target(a) for a in local}
        taken_usernames = self.username_diversity()
        if kind == 'profile':
            members = [await read(acc.client.get_entity, key)]
        else:
            entity = await read(group_entity, acc.client, kind, key)
            members = await self.group_users(acc.client, entity, read)
        users = list({user.id: user for user in members if isinstance(user, types.User) and
                      str(user.id) not in excluded and not user.bot and not user.deleted
                      and ('username' not in selected or user.username)}.values())
        random.shuffle(users)
        workers = [acc]
        limit = min(search['workers'], len(users))
        if kind != 'profile' and limit > 1:
            seen = {target}
            for reader in local:
                ident = self.target(reader)
                if ident not in seen and reader.client is not acc.client and reader.client.is_connected():
                    seen.add(ident)
                    workers.append(reader)
                if len(workers) >= limit:
                    break

        async def details(user, reader):
            if reader is not acc and user.username:
                try:
                    source = await read(reader.client.get_input_entity, '@' + user.username, reader=reader)
                    if getattr(source, 'user_id', None) == user.id:
                        full = await read(reader.client, functions.users.GetFullUserRequest(source), reader=reader)
                        return full.full_user
                except (ValueError, RPCError):
                    pass
            try:
                return (await read(acc.client, functions.users.GetFullUserRequest(user))).full_user
            except UserIdInvalidError:
                return None

        async def candidate_details():
            for offset in range(0, len(users), len(workers)):
                batch = users[offset:offset+len(workers)]
                records = await asyncio.gather(*(details(user, reader) for user, reader in zip(batch, workers)))
                for user, full in zip(batch, records):
                    yield user, full

        unusable_photos = False
        async for user, full in candidate_details():
            search['checked'] += 1
            if full is None:
                continue
            values = {'first_name': user.first_name or '', 'last_name': user.last_name or ''}
            bio = getattr(full, 'about', '') or ''
            birthday = birthday_text(getattr(full, 'birthday', None))
            if ('bio' in selected and not bio) or ('birthday' in selected and not birthday):
                continue
            if bio:
                values['bio'] = bio
            if birthday:
                values['birthday'] = birthday
            if similar_profile(user, bio, birthday, local, self.signatures()):
                continue
            lo, hi = count
            photos = await read(acc.client.get_profile_photos, user, limit=hi + 1 if kind != 'profile' else None)
            photo_count = getattr(photos, 'total', None) or len(photos)
            if kind != 'profile' and not (lo <= photo_count <= hi and len(photos) == photo_count):
                if 'avatars' in selected:
                    continue
                photos = []
            music = []
            if getattr(full, 'saved_music', None) or 'music' in selected or kind == 'profile':
                try:
                    music = await read(profile_music, acc.client, user)
                except (RPCError, ValueError):
                    pass
                if not music and isinstance(getattr(full, 'saved_music', None), types.Document):
                    music = [music_reference(full.saved_music)]
            if 'music' in selected and not music:
                continue
            if music:
                values['music'] = music
            if user.username:
                username = await available_username(acc.client, user.username, target, read, taken_usernames)
                if username:
                    values['username'] = username
                elif 'username' in selected:
                    continue
            values['source_username'] = user.username or ''
            if photos:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                folder = self.photos / token
                folder.mkdir(parents=True, mode=0o700)
                try:
                    for index, photo in enumerate(reversed(photos)):
                        image = await self.download_avatar_still(acc.client, photo, read)
                        (folder / f'{index}.jpg').write_bytes(image)
                except AvatarUnavailable:
                    self.remove_photos(token)
                    unusable_photos = True
                    if 'avatars' in selected:
                        continue
                    photos = []
            return user, values, photos
        if unusable_photos:
            raise ValueError('Подходящие участники найдены, но скачать все их аватарки не удалось')
        if count is not None:
            raise ValueError(f'Проверено участников: {search["checked"]}. '
                             f'Совпадений по обязательным полям и диапазону аватарок ({count[0]}–{count[1]}) нет.')
        raise ValueError('В доступной выборке нет нового участника со всеми выбранными полями')

    async def preview(self, acc, data, search=None):
        kind, key, selected, count, delay = copy_options(data)
        search = {'checked': 0} if search is None else search
        search['workers'] = search_workers(data)
        target = self.target(acc)
        async with self.preview_lock:
            search['state'] = 'searching'
            self.expire()
            self.check_idle(target)
            for token, draft in list(self.drafts.items()):
                if draft['target'] == target:
                    self.drafts.pop(token)
                    self.remove_photos(token)
            token = secrets.token_hex(16)
            try:
                source, values, photos = await asyncio.wait_for(
                    self.select_source(acc, kind, key, selected, count, token, search), SEARCH_TIMEOUT)
                delays = [random.uniform(*delay) * DAY for _ in photos]
                draft = {'token': token, 'target': target, 'source_id': str(source.id), 'values': values,
                         'delays': delays, 'expires': self.clock() + DRAFT_TTL}
                self.drafts[token] = draft
            except asyncio.TimeoutError:
                if search.get('state') == 'waiting':
                    seconds = max(1, math.ceil(search['retry_at'] - self.clock()))
                    raise ValueError(f'Telegram ограничил поиск. Повторите через {seconds} сек.') from None
                raise ValueError(f'Поиск занял слишком много времени. Проверено участников: {search["checked"]}.') from None
            finally:
                if token not in self.drafts:
                    self.remove_photos(token)
        return draft

    @staticmethod
    def public_draft(draft):
        public = {key: draft[key] for key in ('token', 'source_id', 'values', 'delays', 'expires')}
        public['values'] = {key: value for key, value in draft['values'].items() if key not in ('music', 'source_username')}
        if 'music' in draft['values']:
            public['values']['music'] = len(draft['values']['music'])
        return public

    def search_status(self, acc, token=None):
        search = self.searches.get(self.target(acc))
        if search is None or (token is not None and token != search['token']):
            return None
        draft = self.drafts.get(search.get('draft'))
        if search['state'] == 'ready' and (not draft or draft['expires'] <= self.clock()):
            return None
        result = {key: search[key] for key in ('token', 'state', 'checked')}
        if search['state'] == 'waiting':
            result['retry_in'] = max(0, math.ceil(search['retry_at'] - self.clock()))
        if search['state'] == 'error':
            result['error'] = search['error']
        if draft:
            result['draft'] = self.public_draft(draft)
        return result

    def start_search(self, acc, data):
        copy_options(data)
        search_workers(data)
        target = self.target(acc)
        self.check_idle(target)
        search = self.searches.get(target)
        if search and search['state'] in SEARCH_STATES:
            return self.search_status(acc)
        search = {'token': secrets.token_hex(16), 'state': 'queued', 'checked': 0}
        self.searches[target] = search

        async def run():
            try:
                draft = await self.preview(acc, data, search)
                search.update(state='ready', draft=draft['token'])
            except ValueError as exc:
                search.update(state='error', error=str(exc))
            except Exception as exc:
                LOG.warning('Profile source search failed: %s', type(exc).__name__)
                search.update(state='error', error=f'Не удалось прочитать группу или профиль в Telegram ({type(exc).__name__}).')
            finally:
                search['expires'] = self.clock() + DRAFT_TTL

        search['task'] = asyncio.create_task(run())
        return self.search_status(acc)

    async def cancel_search(self, acc, token):
        target = self.target(acc)
        search = self.searches.get(target)
        if search is not None and search['token'] == token:
            self.searches.pop(target)
            search['task'].cancel()
            await asyncio.gather(search['task'], return_exceptions=True)
            if draft := self.drafts.pop(search.get('draft'), None):
                self.remove_photos(draft['token'])

    async def close(self, app):
        tasks = [search['task'] for search in self.searches.values() if not search['task'].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
        job['signature'] = profile_signature(draft['values'].get('source_username'),
                                             draft['values'].get('bio'), draft['values'].get('birthday'))
        if signature_matches(job['signature'], self.signatures()):
            raise ValueError('Похожий профиль уже перенесён. Создайте новый предпросмотр.')
        job.update(state='waiting', due=self.clock(), uploaded=0, errors=[],
                   apply_profile_first=True, replace_photos=bool(job['delays']))
        username = job['values'].get('username')
        if username and not self.username_diversity(except_token=token).allows(username):
            raise ValueError('Юзернейм стал слишком похож на другой профиль. Создайте новый предпросмотр.')
        used_usernames = self.used_usernames | ({username} if username else set())
        self.save(self.jobs | {target: job}, self.used_sources | {draft['source_id']}, used_usernames)
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
        apply_profile = job.get('apply_profile_first', False)
        replace_photos = job.get('replace_photos', False)
        try:
            if apply_profile:
                errors.extend(await self.apply_fields(acc, job['values']))
            else:
                if replace_photos and self.clear_photos is not None:
                    await self.clear_photos(acc)
                if uploaded < len(delays):
                    data = (self.photos / job['token'] / f'{uploaded}.jpg').read_bytes()
                    await asyncio.wait_for(self.upload_avatar(acc, data), 120)
                    uploaded += 1
                if job['values']:
                    errors.extend(await self.apply_fields(acc, job['values']))
        except Exception as exc:
            errors.append(f'Последовательность остановлена: {exc or type(exc).__name__}. Проверьте профиль в Telegram.')
            state, due = 'failed', None
        else:
            state = 'waiting' if uploaded < len(delays) else 'done'
            base = job['due'] if apply_profile else self.clock()
            due = base + delays[uploaded] if state == 'waiting' else None
        self.put({**job, 'values': {}, 'apply_profile_first': False,
                  'replace_photos': replace_photos and apply_profile and state == 'waiting',
                  'uploaded': uploaded, 'due': due, 'state': state, 'errors': errors})
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
            return json_response(self.public_draft(draft))

        async def start_search(request):
            try:
                return json_response(self.start_search(get_account(request), await request.json()))
            except ValueError as exc:
                raise bad_request(exc) from None

        async def search_status(request):
            return json_response(self.search_status(get_account(request), request.match_info.get('search')))

        async def cancel_search(request):
            await self.cancel_search(get_account(request), request.match_info['search'])
            return json_response({'ok': True})

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
        lookup = base + '/search'
        individual = lookup + r'/{search:[a-f0-9]{32}}'
        app.add_routes([web.get(base, status), web.post(base, preview), web.post(plan, confirm),
                        web.delete(plan, discard), web.get(plan + r'/photos/{index:\d+}', image),
                        web.post(lookup, start_search), web.get(lookup, search_status),
                        web.get(individual, search_status), web.delete(individual, cancel_search)])
        app.on_cleanup.append(self.close)
