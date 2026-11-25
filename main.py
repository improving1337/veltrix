import asyncio
import base64
import hashlib
import io
import json
import importlib.util
import logging
import mimetypes
import os
import random
import re
import secrets
import shutil
import socket
import tarfile
import tempfile
import threading
import time
import zipfile
from urllib.parse import quote
from datetime import datetime
from pathlib import Path

from aiohttp import web
from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import (BotResponseTimeoutError, ChannelPrivateError, FloodWaitError, RPCError,
                             UserNotParticipantError)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.users import GetFullUserRequest

import channels
import automatic_multi
import cs3bet
import multi
import presence
import unsub
import template_library
import account_profile_manager
import account_music_manager
import account_deploy
import account_flags
import session_admin
import account_security
import profile_copy
import panel_settings
import vpn
import scenarios
import subscriptions

# ============== НАСТРОЙКИ ==============
BASE_DIR = Path(__file__).resolve().parent
ACCOUNT_DIR = BASE_DIR / "Account"    # <id>.json
SESSION_DIR = BASE_DIR / "Session"    # <id>.session
SORTING_DIR = BASE_DIR / "Sorting"    # сюда кидаешь файлы, они раскладываются сами
WEB_DIR = BASE_DIR / "web"            # index.html
TEMP_DIR = BASE_DIR / "temp"
TEMPLATE_AVATARS = BASE_DIR / "template_avatars"  # <user_id>/01.jpg ... 05.jpg
SPAM_STATUS_FILE = BASE_DIR / 'security' / 'spam_status.json'
SPAM_STATUS = template_library.json_load(SPAM_STATUS_FILE)
TEMPLATE_ROSTER_FILE = BASE_DIR / "template_roster.json"   # просмотренный и отобранный набор
# Pterodactyl обычно передаёт назначенный внешний порт через SERVER_PORT.
# PORT оставлен как запасной вариант для обычного запуска вне хостинга.
HOST = "0.0.0.0"
PORT = int(os.getenv("SERVER_PORT") or os.getenv("PORT") or os.getenv("P_SERVER_PORT") or "8080")
PANEL_USER = "admin"
PASSWORD_FILE = BASE_DIR / "panel_password.txt"  # создаётся сам при первом запуске
TEMPLATE_PAGE = 60  # сколько профилей тянем за раз
# ======================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", force=True)


class Account:
    def __init__(self, name, client):
        self.name, self.client = name, client
        self.me = None
        self.status = "не подключён"
        self.avatar_ver = 0
        self.channels = None     # на сколько каналов подписан (None — ещё не считали)
        self.connecting = False
        self.connect_attempts = 0
        self.error_kind = None  # temporary / session / other
        self.last_error = ""
        self.next_retry_at = None

    @property
    def label(self):
        return self.name


ACCOUNTS: dict[str, Account] = {}
ACCOUNT_FLAGS = None
PANEL_PREFERENCES = None
ACCOUNT_LOAD_ERRORS: dict[str, str] = {}  # malformed JSON, absent/corrupted session files
INDEX: dict[str, Account] = {}      # имя / user id / телефон -> Account
INBOX: dict[str, dict] = {}         # "аккаунт:чат" -> плашка входящих
SUBSCRIBERS: set[asyncio.Queue] = set()
CACHE: dict = {}
PANEL_PASSWORD = ""

# Кэш загруженных участников из источника (для шаблона).
# username группы -> {"users": [Telethon User, ...], "full": {id: (bio, birthday)}}
TEMPLATE_CACHE: dict[str, dict] = {}

# Read-only summary cache: loaded asynchronously so the web panel never waits
# for 58 individual Telegram connections and their profile queries.
ACCOUNT_PROFILE_CACHE: dict[str, dict] = {}
ACCOUNT_PROFILE_TASKS: dict[str, asyncio.Task] = {}
ACCOUNT_PHOTOS: dict[str, tuple[float, list]] = {}
ACCOUNT_PROFILE_SEMAPHORE = asyncio.Semaphore(3)
ACCOUNT_PROFILE_TTL = 15 * 60
ACCOUNT_MUSIC_LOCKS: dict[str, asyncio.Lock] = {}
ACCOUNT_PHOTO_TTL = 4 * 60
# Shared bound for startup, automatic retries, and newly imported accounts.
ACCOUNT_CONNECT_SEMAPHORE = asyncio.Semaphore(4)
ACCOUNT_RETRY_DELAYS = (10, 25, 60, 120, 300)
SESSION_ADMIN = None
SECURITY = account_security.AccountSecurity(BASE_DIR, lambda: ACCOUNTS)


def norm(key):
    return str(key).strip().lstrip("+").lstrip("@").lower()


def find_account(key):
    return INDEX.get(norm(key))


def display_name(e):
    if e is None:
        return ""
    t = getattr(e, "title", None)
    if t:
        return t
    n = f"{getattr(e, 'first_name', '') or ''} {getattr(e, 'last_name', '') or ''}".strip()
    return n or getattr(e, "username", None) or ""


def media_type(m):
    media = m.media
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return ""
    if isinstance(media, types.MessageMediaPhoto):
        return "[ФОТО]"
    for attr, label in (("sticker", "[СТИКЕР]"), ("gif", "[GIF]"), ("video_note", "[КРУЖОК]"),
                        ("video", "[ВИДЕО]"), ("voice", "[ГОЛОСОВОЕ]"), ("audio", "[АУДИО]"),
                        ("document", "[ФАЙЛ]")):
        if getattr(m, attr, None):
            return label
    return "[МЕДИА]"


MEDIA_MIME_FALLBACK = {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/ogg"}


def media_mime(m):
    """Настоящий mime-тип медиа сообщения (фото/документ/стикер/голосовое — что угодно), если его знает Telegram."""
    if isinstance(m.media, types.MessageMediaPhoto):
        return "image/jpeg"
    f = getattr(m, "file", None)
    return (getattr(f, "mime_type", None) or "").lower()


def media_kind(m):
    """Упрощённый тип медиа для показа в сообщении: image / video / audio / "" (остальное — текстовая плашка).
    Определяем по настоящему mime-типу, так что сюда попадают и webp/png стикеры, и видео-кружки, и voice/audio —
    любой формат, который умеет показать браузер."""
    media = m.media
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return ""
    mime = media_mime(m)
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return ""




def media_file_info(m):
    """Metadata for a real Telegram attachment; link previews are not files.

    Never fetch media contents when listing messages. Telegram does not always
    disclose photo sizes; in that case the UI deliberately shows unknown size.
    """
    media = getattr(m, "media", None)
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return None
    f = getattr(m, "file", None)
    if f is None and not isinstance(media, types.MessageMediaPhoto):
        # Locations, contacts, polls and other service media are not files.
        return None
    mime = media_mime(m) or "application/octet-stream"
    ext = (getattr(f, "ext", None) or "").lower()
    if not isinstance(ext, str) or not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif", "video/mp4": ".mp4", "audio/ogg": ".ogg",
               "audio/mpeg": ".mp3", "application/pdf": ".pdf"}.get(mime, "")
        if not ext:
            ext = mimetypes.guess_extension(mime) or ".bin"
    name = getattr(f, "name", None) or ""
    # Telegram filenames are user-controlled: make them safe for headers/UI.
    name = re.split(r"[/\\]", str(name))[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    if not name:
        label = {"[ФОТО]": "photo", "[ВИДЕО]": "video", "[ГОЛОСОВОЕ]": "voice",
                 "[АУДИО]": "audio", "[СТИКЕР]": "sticker", "[КРУЖОК]": "video_note",
                 "[GIF]": "animation"}.get(media_type(m), "file")
        name = f"{label}_{m.id}{ext}"
    elif "." not in name and ext:
        name += ext
    name = name[:180]
    size = getattr(f, "size", None)
    if size is None:
        size = getattr(getattr(m, "document", None), "size", None)
    if not isinstance(size, int) or size < 0:
        size = None
    return {"file_name": name, "file_size": size, "file_mime": mime}


def tl_name(x):
    return type(x).__name__


def btn_data(b):
    """Данные callback-кнопки (в разных версиях поле называется по-разному)."""
    for k in ("data", "callback_data", "payload"):
        v = getattr(b, k, None)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        if isinstance(v, str) and v:
            return v.encode()
    return None


def btn_kind(b):
    """Тип inline-кнопки: cb (callback), url, web (мини-приложение), copy, other. Определяем по имени/полям — без types.*"""
    n = tl_name(b)
    if getattr(b, "requires_password", False):
        return "other"
    if "WebView" in n:
        return "web"
    has_url = isinstance(getattr(b, "url", None), str) and bool(getattr(b, "url"))
    if "Url" in n or (has_url and btn_data(b) is None):
        return "url"
    if "Copy" in n:
        return "copy"
    if "Callback" in n or "InlineButton" in n or btn_data(b) is not None:
        return "cb"
    return "other"


def is_inline_markup(rm):
    return rm is not None and "InlineMarkup" in tl_name(rm)


def ser_buttons(m):
    """Inline-кнопки сообщения (от ботов): строки кнопок или None."""
    try:
        rm = getattr(m, "reply_markup", None)
        if not is_inline_markup(rm):
            return None
        rows = []
        for r, row in enumerate(rm.rows):
            out = []
            for c, b in enumerate(row.buttons):
                kind = btn_kind(b)
                d = {"text": getattr(b, "text", "") or "", "row": r, "col": c, "kind": kind, "cls": tl_name(b)}
                if kind == "other":   # для диагностики: какие поля у кнопки
                    d["attrs"] = sorted(k for k in dir(b) if not k.startswith("_") and not callable(getattr(b, k, None)))[:14]
                if kind == "url":
                    d["url"] = getattr(b, "url", "") or ""
                elif kind == "copy":
                    d["copy"] = getattr(b, "copy_text", "") or ""
                out.append(d)
            rows.append(out)
        return rows or None
    except Exception as exc:
        print(f"[BUTTONS] не удалось разобрать клавиатуру: {type(exc).__name__}: {exc}", flush=True)
        return None


def ser_message(m):
    sender = ""
    if not m.out:
        try:
            sender = display_name(m.sender)
        except Exception:
            pass
    attachment = media_file_info(m)
    return {"id": m.id, "out": bool(m.out), "text": m.message or "", "media": media_type(m),
            "media_kind": media_kind(m), "has_media": attachment is not None,
            "file_name": attachment["file_name"] if attachment else None,
            "file_size": attachment["file_size"] if attachment else None,
            "file_mime": attachment["file_mime"] if attachment else None,
            "date": m.date.timestamp() if m.date else 0, "sender": sender, "buttons": ser_buttons(m)}


def flag_for(name):
    return ACCOUNT_FLAGS.get(name) if ACCOUNT_FLAGS else account_flags.GENERAL


def general_account(name):
    return flag_for(name) == account_flags.GENERAL


def service_clients(general_only=False):
    return {name: acc.client for name, acc in ACCOUNTS.items()
            if acc.me is not None and (not general_only or general_account(name))}


def ser_account(a):
    me = a.me
    return {"name": a.name, "flag": flag_for(a.name), "connected": bool(me is not None and a.client.is_connected()), "spam_status": SPAM_STATUS.get(str(me.id)) if me else None, "status": a.status,
            "id": me.id if me else None, "phone": (me.phone or "") if me else "",
            "first_name": (me.first_name or "") if me else "", "last_name": (me.last_name or "") if me else "",
            "username": (me.username or "") if me else "", "avatar_ver": int(a.avatar_ver), "presence": presence.keeper.info(a.name),
            "channels": a.channels if me else None,
            "profile_meta": _cached_profile_meta(a) if me else None,
            "connecting": bool(a.connecting),
            "connect_attempts": int(a.connect_attempts),
            "error_kind": a.error_kind,
            "last_error": a.last_error,
            "next_retry_at": a.next_retry_at,
            "security": SECURITY.info(a.name),
            "session_owned": SESSION_ADMIN.owned(a) if SESSION_ADMIN else False}


def _cached_profile_meta(acc):
    cached = ACCOUNT_PROFILE_CACHE.get(acc.name)
    if not isinstance(cached, dict) or cached.get('telegram_id') != getattr(acc.me, 'id', None):
        return None
    return {k: cached.get(k) for k in ('bio', 'birthday', 'avatar_count',
                                        'last_avatar_change', 'music_count', 'checked_at')}


def invalidate_account_profile(acc):
    ACCOUNT_PROFILE_CACHE.pop(acc.name, None)
    ACCOUNT_PHOTOS.pop(acc.name, None)
    for key in tuple(CACHE):
        if isinstance(key, tuple) and len(key) > 1 and key[1] == acc.name and key[0] in ('me', 'profile_photo'):
            CACHE.pop(key, None)


async def _refresh_account_profile(acc):
    """Only an explicitly connected account is fetched; failures never block HTTP startup."""
    try:
        async with ACCOUNT_PROFILE_SEMAPHORE:
            if acc.me is None:
                return
            # Separate calls help report partial information if only one API is unavailable.
            meta = {'telegram_id': acc.me.id, 'bio': '', 'birthday': '',
                    'avatar_count': None, 'last_avatar_change': '', 'music_count': None, 'checked_at': time.time()}
            try:
                full = await asyncio.wait_for(get_full(acc), timeout=25)
                meta['bio'] = full['bio']
                meta['birthday'] = full['birthday']
            except Exception as exc:
                logging.warning('[PROFILE] %s full: %s: %s', acc.name, type(exc).__name__, exc)
                meta['bio'] = None
                meta['birthday'] = None
            try:
                recent = await asyncio.wait_for(acc.client.get_profile_photos('me', limit=1), timeout=25)
                meta.update(account_profile_manager.summary_from_photos(recent))
            except Exception as exc:
                logging.warning('[PROFILE] %s photo summary: %s: %s', acc.name, type(exc).__name__, exc)
            try:
                meta['music_count'] = await asyncio.wait_for(
                    account_music_manager.get_profile_music_count(acc.client, functions), timeout=20)
            except Exception as exc:
                logging.warning('[PROFILE] %s music count: %s: %s', acc.name, type(exc).__name__, exc)
            if acc.me is not None and acc.me.id == meta['telegram_id']:
                ACCOUNT_PROFILE_CACHE[acc.name] = meta
                acc.copy_profile_meta = meta
    finally:
        task = ACCOUNT_PROFILE_TASKS.get(acc.name)
        if task is asyncio.current_task():
            ACCOUNT_PROFILE_TASKS.pop(acc.name, None)


def schedule_account_profile_refresh(acc, *, force=False):
    if acc.me is None:
        return
    prev = ACCOUNT_PROFILE_TASKS.get(acc.name)
    if prev is not None and not prev.done():
        return
    cached = ACCOUNT_PROFILE_CACHE.get(acc.name)
    if not force and isinstance(cached, dict) and cached.get('telegram_id') == acc.me.id:
        if time.time() - float(cached.get('checked_at') or 0) < ACCOUNT_PROFILE_TTL:
            return
    ACCOUNT_PROFILE_TASKS[acc.name] = asyncio.create_task(_refresh_account_profile(acc))


async def fetch_account_photos(acc, *, force=False):
    cache = ACCOUNT_PHOTOS.get(acc.name)
    if not force and cache and time.time() - cache[0] < ACCOUNT_PHOTO_TTL:
        return cache[1]
    # The first 100 most recent photos are enough for the manager; the API
    # also returns the real total, so we never imply this is the entire history.
    photos = await asyncio.wait_for(acc.client.get_profile_photos('me', limit=100), timeout=45)
    ACCOUNT_PHOTOS[acc.name] = (time.time(), photos)
    meta = ACCOUNT_PROFILE_CACHE.get(acc.name, {})
    if meta.get('telegram_id') != acc.me.id:
        meta = {'telegram_id': acc.me.id, 'bio': None, 'birthday': None}
    meta.update(account_profile_manager.summary_from_photos(photos))
    meta['checked_at'] = time.time()
    ACCOUNT_PROFILE_CACHE[acc.name] = meta
    return photos


async def api_account_photos(request):
    acc = get_acc(request)
    photos = await fetch_account_photos(acc, force=request.query.get('refresh') == '1')
    summary = account_profile_manager.summary_from_photos(photos)
    return web.json_response({'photos': [account_profile_manager.serialize_photo(photo) for photo in photos],
                              'total': summary['avatar_count'],
                              'shown': len(photos), 'has_more': summary['avatar_count'] is not None and summary['avatar_count'] > len(photos)})


async def _account_photo_by_id(acc, raw_photo_id):
    try:
        photo_id = int(raw_photo_id)
    except (ValueError, TypeError):
        raise web.HTTPNotFound()
    photos = await fetch_account_photos(acc)
    photo = next((p for p in photos if getattr(p, 'id', None) == photo_id), None)
    if photo is None:
        photos = await fetch_account_photos(acc, force=True)
        photo = next((p for p in photos if getattr(p, 'id', None) == photo_id), None)
    if photo is None:
        raise web.HTTPNotFound(text='Фотография отсутствует в истории аккаунта')
    return photo


async def api_account_photo_image(request):
    acc = get_acc(request)
    photo = await _account_photo_by_id(acc, request.match_info['pid'])
    async def load():
        return await acc.client.download_media(photo, file=bytes, thumb=0)
    return await cached_image(('profile_photo', acc.name, photo.id), load)


async def api_account_photo_delete(request):
    """Delete ONE photo from this account's own Telegram history, with UI confirmation."""
    acc = get_acc(request)
    photo = await _account_photo_by_id(acc, request.match_info['pid'])
    try:
        deleted = await acc.client(functions.photos.DeletePhotosRequest(id=[utils.get_input_photo(photo)]))
        if photo.id not in deleted:
            fail('Telegram не подтвердил удаление этой фотографии')
    except RPCError as exc:
        fail('Не удалось удалить фотографию: ' + err_text(exc))
    invalidate_account_profile(acc)
    acc.avatar_ver = time.time()
    try:
        acc.me = await acc.client.get_me()
    except Exception:
        pass
    schedule_account_profile_refresh(acc, force=True)
    return web.json_response({'ok': True})


async def api_account_music(request):
    """Refresh the count from Telegram for the selected connected user."""
    acc = get_acc(request)
    try:
        count = await asyncio.wait_for(
            account_music_manager.get_profile_music_count(acc.client, functions), timeout=30)
    except (account_music_manager.MusicProfileError, RPCError) as exc:
        fail(str(exc) if isinstance(exc, account_music_manager.MusicProfileError) else err_text(exc))
    meta = ACCOUNT_PROFILE_CACHE.get(acc.name) or {}
    if meta.get('telegram_id') != acc.me.id:
        meta = {'telegram_id': acc.me.id}
    meta['music_count'] = count
    # Don't refresh full profile solely to update song count.
    ACCOUNT_PROFILE_CACHE[acc.name] = meta
    return web.json_response({'ok': True, 'count': count})


async def api_account_music_add(request):
    """Add one audio message to this account's own profile, on explicit click."""
    acc = get_acc(request)
    try:
        payload = await request.json()
        link = payload.get('url', '') if isinstance(payload, dict) else ''
        # Validate before trying the Telegram connection.
        account_music_manager.parse_music_post_url(link)
    except (ValueError, account_music_manager.MusicProfileError) as exc:
        fail(str(exc))
    lock = ACCOUNT_MUSIC_LOCKS.setdefault(acc.name, asyncio.Lock())
    if lock.locked():
        fail('Для этого аккаунта уже выполняется добавление песни')
    async with lock:
        try:
            added = await asyncio.wait_for(
                account_music_manager.add_profile_music(acc.client, functions, utils, link), timeout=45)
        except account_music_manager.MusicProfileError as exc:
            fail(str(exc))
        except FloodWaitError as exc:
            fail(f'Лимит Telegram: повтори попытку через {int(exc.seconds)} сек.')
        except RPCError as exc:
            fail('Telegram отклонил добавление песни: ' + err_text(exc))
        except asyncio.TimeoutError:
            fail('Telegram не ответил. Проверь список песен перед повторной попыткой.')
        # Addition succeeded; count failure must not be misreported as addition failure.
        count = None
        warning = ''
        try:
            count = await asyncio.wait_for(
                account_music_manager.get_profile_music_count(acc.client, functions), timeout=25)
        except Exception as exc:
            warning = f'Песня добавлена, но счётчик не обновлён: {type(exc).__name__}'
            logging.warning('[MUSIC] %s: %s', acc.name, warning)
        meta = ACCOUNT_PROFILE_CACHE.get(acc.name) or {}
        if meta.get('telegram_id') != acc.me.id:
            meta = {'telegram_id': acc.me.id}
        meta['music_count'] = count
        ACCOUNT_PROFILE_CACHE[acc.name] = meta
        return web.json_response({'ok': True, 'count': count, 'warning': warning, **added})


# ---------- сортировка и загрузка ----------
SORT_LOCK = threading.Lock()
NESTED_EXT = (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz")  # вложенные архивы
MAX_FILE = 50 * 1024 * 1024   # .json/.session больше этого размера пропускаем
MAX_DEPTH = 4                 # насколько глубоко открывать архивы внутри архивов


def archive_kind(path):
    """Определяет тип архива по содержимому (расширение не важно): zip / 7z / rar / tar(.gz/.bz2/.xz) или None."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        if head.startswith(b"PK") and zipfile.is_zipfile(path):
            return "zip"
        if head.startswith(b"7z\xbc\xaf\x27\x1c"):
            return "7z"
        if head.startswith(b"Rar!"):
            return "rar"
        if tarfile.is_tarfile(path):
            return "tar"
    except OSError:
        pass
    return None


def _iter_dir(root):
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            yield str(p.relative_to(root)), p.stat().st_size, (lambda p=p: open(p, "rb"))


def iter_archive(path, kind):
    """Отдаёт (имя внутри архива, размер, функция-открывалка) для каждого файла, во всех вложенных папках."""
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if not info.is_dir():
                    yield info.filename, info.file_size, (lambda i=info: z.open(i))
    elif kind == "tar":
        with tarfile.open(path) as t:
            for m in t:
                if m.isfile():
                    yield m.name, m.size, (lambda m=m: t.extractfile(m))
    else:
        tmp = Path(tempfile.mkdtemp(dir=TEMP_DIR))
        try:
            if kind == "7z":
                try:
                    import py7zr
                except ImportError:
                    raise RuntimeError("для .7z нужен пакет py7zr (pip install py7zr)")
                with py7zr.SevenZipFile(path) as z:
                    z.extractall(tmp)
            else:
                try:
                    import rarfile
                except ImportError:
                    raise RuntimeError("для .rar нужен пакет rarfile и unrar (pip install rarfile)")
                with rarfile.RarFile(path) as z:
                    z.extractall(tmp)
            yield from _iter_dir(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def new_stats():
    return {"json": 0, "session": 0, "archives": 0, "dup": 0, "bad": 0, "junk": 0, "errors": [],
            "json_names": set(), "session_names": set()}


def place_file(base, data, stats):
    """Кладёт .json -> Account, .session -> Session. Уже подключённые аккаунты не перезаписываем."""
    suffix, stem = Path(base).suffix.lower(), Path(base).stem
    if stem in ACCOUNTS:
        stats["dup"] += 1
        return
    if suffix == ".json":
        try:
            cfg = json.loads(data.decode("utf-8-sig"))
            cfg["app_id"], cfg["app_hash"]
        except Exception:
            stats["bad"] += 1          # json без app_id/app_hash — не от аккаунта
            return
        (ACCOUNT_DIR / base).write_bytes(data)
        stats["json"] += 1
        stats["json_names"].add(stem)
    else:
        (SESSION_DIR / base).write_bytes(data)
        stats["session"] += 1
        stats["session_names"].add(stem)


def process_archive(path, stats, depth=0):
    """Открывает архив (любая вложенность папок: папка/tdata/сессия и .json, архивы в архивах)
    и раскладывает найденные циферки.session и циферки.json."""
    kind = archive_kind(path)
    if kind is None:
        raise ValueError("не архив или файл повреждён")
    for name, size, opener in iter_archive(path, kind):
        base = Path(name.replace("\\", "/")).name
        if not base or base.startswith("."):
            continue
        suffix = Path(base).suffix.lower()
        if suffix in (".json", ".session"):
            if size > MAX_FILE:
                print(f"[SORT] {Path(path).name}: {base} слишком большой, пропускаю", flush=True)
                continue
            with opener() as src:
                place_file(base, src.read(), stats)
        elif suffix in NESTED_EXT and depth < MAX_DEPTH:
            tmp = Path(tempfile.mkdtemp(dir=TEMP_DIR)) / base
            try:
                with opener() as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                process_archive(tmp, stats, depth + 1)
            except Exception as exc:
                stats["errors"].append(f"{base}: {type(exc).__name__}: {exc}")
                print(f"[SORT] вложенный {base}: {type(exc).__name__}: {exc}", flush=True)
            finally:
                shutil.rmtree(tmp.parent, ignore_errors=True)


def sort_incoming():
    """Разбирает папку Sorting. Возвращает статистику. Безопасно вызывать из потока."""
    with SORT_LOCK:
        return _sort_incoming()


def _sort_incoming():
    for d in (SORTING_DIR, ACCOUNT_DIR, SESSION_DIR, TEMP_DIR):
        d.mkdir(exist_ok=True)
    st = new_stats()
    for p in [p for p in SORTING_DIR.rglob("*") if p.is_file()]:
        try:
            if p.suffix.lower() in (".json", ".session"):
                place_file(p.name, p.read_bytes(), st)
                p.unlink()
            elif archive_kind(p):
                process_archive(p, st)
                st["archives"] += 1
                p.unlink()
            else:
                p.unlink()
                st["junk"] += 1
        except Exception as exc:
            st["errors"].append(f"{p.name}: {type(exc).__name__}: {exc}")
            print(f"[SORT] {p.name}: {type(exc).__name__}: {exc}", flush=True)
            if not isinstance(exc, RuntimeError):   # RuntimeError = не хватает библиотеки, файл оставляем
                p.unlink(missing_ok=True)
    for sub in SORTING_DIR.iterdir():
        if sub.is_dir():
            shutil.rmtree(sub, ignore_errors=True)
    st["no_session"] = sorted(n for n in st["json_names"] if not (SESSION_DIR / f"{n}.session").is_file())
    st["no_json"] = sorted(n for n in st["session_names"] if not (ACCOUNT_DIR / f"{n}.json").is_file())
    print(f"[SORT] json: {st['json']}, session: {st['session']}, архивов: {st['archives']}, "
          f"уже подключены: {st['dup']}, не аккаунт-json: {st['bad']}, удалено лишнего: {st['junk']}, "
          f"без пары: {len(st['no_session']) + len(st['no_json'])}", flush=True)
    return st


def load_accounts():
    """Читает Account/*.json. Уже загруженные пропускает. Возвращает список НОВЫХ аккаунтов."""
    new = []
    # Rebuild file errors on each scan, so a replaced/corrected session clears its warning.
    ACCOUNT_LOAD_ERRORS.clear()
    # A standalone .session uses the public Telegram Desktop API credentials.
    for session_file in SESSION_DIR.glob("*.session"):
        config = ACCOUNT_DIR / f"{session_file.stem}.json"
        if not config.exists():
            try:
                account_security.private_json(config, {"app_id": 2040, "app_hash": "b18441a1ff607e10a989891a5462e627"}, exclusive=True)
            except FileExistsError:
                pass
    for file in sorted(ACCOUNT_DIR.glob("*.json")):
        name = file.stem
        if name in ACCOUNTS:
            continue
        try:
            cfg = json.loads(file.read_text(encoding="utf-8-sig"))
            app_id, app_hash = int(cfg["app_id"]), str(cfg["app_hash"])
        except Exception as exc:
            ACCOUNT_LOAD_ERRORS[name] = f"неверный файл настроек: {type(exc).__name__}: {exc}"[:240]
            print(f"[LOAD] {file.name}: {ACCOUNT_LOAD_ERRORS[name]}", flush=True)
            continue
        if not (SESSION_DIR / f"{name}.session").is_file():
            ACCOUNT_LOAD_ERRORS[name] = "отсутствует файл Session/" + name + ".session"
            print(f"[LOAD] {name}: {ACCOUNT_LOAD_ERRORS[name]}", flush=True)
            continue
        extra = {}
        for jk, tk in (("device", "device_model"), ("device_model", "device_model"), ("sdk", "system_version"),
                       ("system_version", "system_version"), ("app_version", "app_version"),
                       ("lang_code", "lang_code"), ("system_lang_code", "system_lang_code")):
            if cfg.get(jk):
                extra[tk] = str(cfg[jk])
        try:
            acc = Account(name, TelegramClient(str(SESSION_DIR / name), app_id, app_hash, flood_sleep_threshold=0, **extra))
        except Exception as exc:
            ACCOUNT_LOAD_ERRORS[name] = f"ошибка открытия сессии [{type(exc).__name__}]: {exc}"[:240]
            print(f"[LOAD] {name}: {ACCOUNT_LOAD_ERRORS[name]}", flush=True)
            continue
        new.append(acc)
    if ACCOUNT_FLAGS and new:
        default = (account_flags.AUTOPILOT if PANEL_PREFERENCES.get()['autopilot_for_new_accounts']
                   else account_flags.GENERAL)
        ACCOUNT_FLAGS.register((acc.name for acc in new), default)
    ACCOUNTS.update((acc.name, acc) for acc in new)
    for session_file in sorted(SESSION_DIR.glob("*.session")):
        name = session_file.stem
        if name not in ACCOUNTS and not (ACCOUNT_DIR / f"{name}.json").is_file():
            ACCOUNT_LOAD_ERRORS[name] = f"нет файла настроек Account/{name}.json"
    return new


DEAD_REASONS = {
    "UserDeactivatedBanError": "аккаунт заблокирован Telegram",
    "UserDeactivatedError": "аккаунт удалён или деактивирован",
    "PhoneNumberBannedError": "номер телефона заблокирован",
    "AuthKeyUnregisteredError": "сессия завершена, нужна новая авторизация",
    "SessionRevokedError": "сессия отозвана владельцем",
    "SessionExpiredError": "сессия истекла",
    "AuthKeyDuplicatedError": "одна сессия используется с разных IP; нужна новая авторизация",
    "AuthKeyInvalidError": "недействительный ключ сессии",
    "AuthKeyNotFound": "ключ сессии не найден",
    "SessionPasswordNeededError": "для входа требуется пароль 2FA",
    "ApiIdInvalidError": "неверные Telegram API ID / API HASH",
    "PhoneCodeInvalidError": "неверный код входа",
}
TRANSIENT_CONNECTION_ERRORS = {
    "TimedOutError", "ServerError", "RpcCallFailError", "InterdcCallError",
    "InterdcCallRichError", "FloodWaitError", "TimeoutError",
}


def classify_connect_error(exc):
    """Network/server outages are retried; invalid credentials are never retried blindly."""
    name = type(exc).__name__
    if name in DEAD_REASONS or isinstance(exc, (PermissionError, FileNotFoundError)):
        return "session"
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)) or name in TRANSIENT_CONNECTION_ERRORS:
        return "temporary"
    return "other"


def explain_connect_error(exc):
    name = type(exc).__name__
    if name in DEAD_REASONS:
        return f"{DEAD_REASONS[name]} [{name}]"
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)) or name in TRANSIENT_CONNECTION_ERRORS:
        return f"нет связи с Telegram [{name}]" + (f": {exc}" if str(exc) else "")
    detail = str(exc).strip().replace("\n", " ")
    return (f"ошибка подключения [{name}]: {detail}" if detail else f"ошибка подключения [{name}]")[:240]


def _failed_connect(acc, text, kind, retry_after=None):
    acc.me = None
    acc.last_error = text
    acc.error_kind = kind
    acc.status = text
    if kind == "temporary":
        delay = retry_after if retry_after is not None else ACCOUNT_RETRY_DELAYS[
            min(max(acc.connect_attempts - 1, 0), len(ACCOUNT_RETRY_DELAYS) - 1)]
        acc.next_retry_at = time.time() + max(1, float(delay))
        logging.warning("[TELEGRAM] %s: попытка %s: %s; повтор через %s сек.",
                        acc.name, acc.connect_attempts, text, int(delay))
    else:
        acc.next_retry_at = None
        logging.error("[TELEGRAM] %s: %s (автоповтор отключён)", acc.name, text)


async def connect_account(acc):
    """One connection attempt with explicit failure cause and bounded wall-clock time."""
    if acc.me is not None:
        return True
    if acc.connecting:
        return False
    acc.connecting = True
    acc.connect_attempts += 1
    acc.next_retry_at = None
    acc.status = f"подключение · попытка {acc.connect_attempts}"
    try:
        async with ACCOUNT_CONNECT_SEMAPHORE:
            await asyncio.wait_for(acc.client.connect(), timeout=45)
            # get_me() raises real session errors, unlike is_user_authorized().
            me = await asyncio.wait_for(acc.client.get_me(), timeout=30)
        if me is None:
            _failed_connect(acc, "сессия не авторизована — требуется повторный вход", "session")
            try:
                await asyncio.wait_for(acc.client.disconnect(), timeout=10)
            except Exception:
                pass
            return False
        if ACCOUNT_FLAGS:
            ACCOUNT_FLAGS.bind(acc.name, me.id)
        acc.me = me
        acc.status = "онлайн"
        acc.error_kind = None
        acc.last_error = ""
        acc.next_retry_at = None
        if not getattr(acc, "handler_registered", False):
            register_handler(acc)
            acc.handler_registered = True
        for key in (acc.name, me.id, me.phone):
            if key:
                INDEX[norm(key)] = acc
        print(f"[{acc.label}] Подключено (попытка {acc.connect_attempts}): ID {me.id}", flush=True)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        kind = classify_connect_error(exc)
        retry_after = None
        if type(exc).__name__ == "FloodWaitError":
            try:
                retry_after = max(1, int(exc.seconds))
            except (ValueError, TypeError, AttributeError):
                pass
        _failed_connect(acc, explain_connect_error(exc), kind, retry_after)
        try:
            await asyncio.wait_for(acc.client.disconnect(), timeout=10)
        except Exception:
            pass
        return False
    finally:
        acc.connecting = False


async def connect_initial_accounts(max_parallel=4):
    """First pass starts after the site, without blocking the web server."""
    accounts = list(ACCOUNTS.values())
    if not accounts:
        return
    sem = asyncio.Semaphore(max(1, int(max_parallel)))

    async def one(acc):
        async with sem:
            return await connect_account(acc)

    results = await asyncio.gather(*(one(acc) for acc in accounts), return_exceptions=True)
    online = sum(result is True for result in results)
    for acc, result in zip(accounts, results):
        if isinstance(result, BaseException):
            _failed_connect(acc, explain_connect_error(result), classify_connect_error(result))
    print(f"[TELEGRAM] Первичная проверка: онлайн {online}/{len(accounts)}. Панель доступна.", flush=True)


async def reconnect_failed_accounts(poll_seconds=3):
    """Keep retrying *temporary* failures without repeating invalid-session logins.

    New imports are included because ACCOUNTS is scanned on every cycle.
    Calls from initial import and retries share ACCOUNT_CONNECT_SEMAPHORE.
    """
    inflight = {}
    try:
        while True:
            now = time.time()
            for acc in list(ACCOUNTS.values()):
                if acc.me is not None or acc.connecting or acc.error_kind != "temporary":
                    continue
                if acc.next_retry_at is None or acc.next_retry_at > now:
                    continue
                if acc.name in inflight:
                    continue
                async def retry(target):
                    if ACCOUNTS.get(target.name) is target:
                        return await connect_account(target)
                    return False
                task = asyncio.create_task(retry(acc), name=f"telegram-retry-{acc.name}")
                inflight[acc.name] = task
                task.add_done_callback(lambda done, key=acc.name: inflight.pop(key, None))
            await asyncio.sleep(poll_seconds)
    finally:
        tasks = list(inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------- события ----------
def publish(ev):
    for q in list(SUBSCRIBERS):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass


def register_handler(acc):
    @acc.client.on(events.NewMessage())
    async def on_message(event):
        try:
            m, chat_id = event.message, event.chat_id
            # во «Входящие» попадают только личные сообщения — группы и каналы игнорируем
            if not event.is_private:
                return
            chat = await event.get_chat()
            if getattr(chat, "bot", False):
                # сообщения от ботов не попадают во «Входящие»: без карточки, счётчика, уведомления и звука.
                # Событие шлём только для того, чтобы ответы бота появлялись в уже открытом чате (+ Написать)
                publish({"type": "message", "account": acc.name, "chat_id": chat_id, "out": bool(m.out),
                         "bot": True, "message": ser_message(m)})
                return
            title = display_name(chat) or "Неизвестный чат"
            kind = "user"
            payload = {"type": "message", "account": acc.name, "chat_id": chat_id, "out": bool(m.out)}
            if not m.out:
                sender = await event.get_sender()
                text, media = m.message or "", media_type(m)
                stamp = datetime.now().astimezone().strftime("%H:%M:%S")
                print(f'\n[{stamp}] На аккаунт {acc.label} пришло сообщение от [{event.sender_id or "?"}]: '
                      f'"{f"{text} {media}".strip()}"', flush=True)
                card = INBOX.setdefault(f"{acc.name}:{chat_id}", {"account": acc.name, "chat_id": chat_id, "unread": 0})
                card.update(title=title, kind=kind, last_text=text or media or "Сообщение",
                            last_sender=display_name(sender), date=m.date.timestamp())
                card["unread"] += 1
                payload["card"] = dict(card)
            payload["message"] = ser_message(m)
            publish(payload)
        except Exception as exc:
            print(f"[{acc.label}] [MESSAGE ERROR] {type(exc).__name__}: {exc}", flush=True)

    @acc.client.on(events.MessageEdited())
    async def on_edit(event):
        try:
            if event.is_private:
                publish({"type": "edit", "account": acc.name, "chat_id": event.chat_id,
                         "message": ser_message(event.message)})
        except Exception as exc:
            print(f"[{acc.label}] [EDIT ERROR] {type(exc).__name__}: {exc}", flush=True)


# ---------- web ----------
@web.middleware
async def auth_mw(request, handler):
    ok = False
    h = request.headers.get("Authorization", "")
    if h.startswith("Basic "):
        try:
            u, _, p = base64.b64decode(h[6:]).decode().partition(":")
            ok = (secrets.compare_digest(u.encode(), PANEL_USER.encode())
                  and secrets.compare_digest(p.encode(), PANEL_PASSWORD.encode()))
        except Exception:
            pass
    if not ok:
        return web.Response(status=401, text="Нужен вход", headers={"WWW-Authenticate": 'Basic realm="Telegram Panel"'})
    if request.method != "GET" and request.headers.get("X-Panel") != "1":
        print(f"[AUTH] 403 {request.method} {request.path}: нет заголовка X-Panel: 1", flush=True)
        return web.Response(status=403, text="Forbidden")
    return await handler(request)


@web.middleware
async def error_mw(request, handler):
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if exc.status >= 400:
            print(f"[HTTP {exc.status}] {request.method} {request.path}: {exc.text}", flush=True)
        raise
    except Exception as exc:
        if isinstance(exc, RPCError):
            print(f"[RPC ERROR] {request.method} {request.path}: {type(exc).__name__}: {exc}", flush=True)
        else:
            logging.exception("API error")
        return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=400)


def fail(msg, cls=web.HTTPBadRequest):
    raise cls(text=json.dumps({"error": msg}, ensure_ascii=False), content_type="application/json")


def get_acc(request):
    acc = find_account(request.match_info["acc"])
    if not acc or acc.me is None:
        fail("Аккаунт не найден или не подключён", web.HTTPNotFound)
    return acc


async def resolve(acc, chat_id):
    try:
        return await acc.client.get_input_entity(chat_id)
    except ValueError:
        await acc.client.get_dialogs(limit=200)
        return await acc.client.get_input_entity(chat_id)


async def cached_image(key, loader):
    hit = CACHE.get(key)
    if not hit or time.time() - hit[0] > 600:
        try:
            data = await loader()
        except Exception as exc:
            print(f"[IMAGE] {key}: {type(exc).__name__}: {exc}", flush=True)
            data = None
        if len(CACHE) > 300:
            CACHE.clear()
        hit = CACHE[key] = (time.time(), data)
    if not hit[1]:
        raise web.HTTPNotFound()
    return web.Response(body=hit[1], content_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


async def index(request):
    page = WEB_DIR / "index.html"
    if not page.is_file():
        return web.Response(status=500, text=f"Не найден файл: {page}\nПоложи index.html именно сюда.")
    return web.FileResponse(page)


async def api_accounts(request):
    for acc in ACCOUNTS.values():
        if acc.me is not None:
            schedule_account_profile_refresh(acc)
    return web.json_response({"items": [ser_account(a) for a in ACCOUNTS.values()],
                              "load_errors": [{"name": name, "error": error}
                                              for name, error in sorted(ACCOUNT_LOAD_ERRORS.items())]})


async def api_account_flag(request):
    name = request.match_info["acc"]
    if name not in ACCOUNTS:
        fail("Аккаунт не найден", web.HTTPNotFound)
    payload = await request.json()
    try:
        ACCOUNT_FLAGS.select(name, payload.get("flag") if isinstance(payload, dict) else None)
    except ValueError as exc:
        fail(str(exc))
    return web.json_response({"flags": {key: flag_for(key) for key in ACCOUNTS}})


async def api_delete_account(request):
    async with SECURITY.lock:
        return await _api_delete_account(request)


async def _api_delete_account(request):
    """Удаляет аккаунт из панели: отключает клиент, стирает Account/<имя>.json и Session/<имя>.session.
    Сессия в самом Telegram не завершается (log_out не вызываем) — удаляются только файлы."""
    name = request.match_info["acc"]
    acc = ACCOUNTS.get(name)
    if acc is None:
        fail("Аккаунт не найден", web.HTTPNotFound)
    try:
        await asyncio.wait_for(acc.client.disconnect(), timeout=10)
    except Exception as exc:
        print(f"[DELETE] {name}: disconnect: {type(exc).__name__}: {exc}", flush=True)
    try:
        acc.client.session.close()
    except Exception:
        pass
    ACCOUNTS.pop(name, None)
    invalidate_account_profile(acc)
    task = ACCOUNT_PROFILE_TASKS.pop(name, None)
    if task and not task.done():
        task.cancel()
    for k in [k for k, v in INDEX.items() if v is acc]:
        INDEX.pop(k, None)
    for k in [k for k, v in INBOX.items() if v["account"] == name]:
        INBOX.pop(k, None)
    for k in [k for k in CACHE if isinstance(k, tuple) and len(k) > 1 and k[1] == name]:
        CACHE.pop(k, None)
    removed, errors = [], []
    for path in (ACCOUNT_DIR / f"{name}.json", SESSION_DIR / f"{name}.session",
                 SESSION_DIR / f"{name}.session-journal"):
        try:
            if path.is_file():
                path.unlink()
                removed.append(path.name)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    print(f"[DELETE] {name}: удалено файлов {len(removed)} ({', '.join(removed)}), ошибок {len(errors)}", flush=True)
    publish({"type": "read"})   # обновить список «Входящие» у открытых вкладок
    if errors:
        fail("Аккаунт убран из панели, но файлы не удалились: " + "; ".join(errors))
    if ACCOUNT_FLAGS:
        ACCOUNT_FLAGS.forget(name)
    return web.json_response({"ok": True, "removed": removed})


async def api_my_avatar(request):
    acc = get_acc(request)
    resp = await cached_image(("me", acc.name), lambda: acc.client.download_profile_photo("me", file=bytes))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def api_chat_avatar(request):
    acc, cid = get_acc(request), int(request.match_info["chat_id"])

    async def load():
        return await acc.client.download_profile_photo(await resolve(acc, cid), file=bytes)
    return await cached_image(("chat", acc.name, cid), load)


async def api_chat_media(request):
    """Inline legacy preview or explicit attachment download from Telegram."""
    acc, cid = get_acc(request), int(request.match_info["chat_id"])
    mid = int(request.match_info["msg_id"])
    if request.query.get("download") == "1":
        # Download to a temporary file and stream in chunks; large videos must
        # never be fully buffered in memory. Session authentication is required.
        msg = await acc.client.get_messages(await resolve(acc, cid), ids=mid)
        meta = media_file_info(msg) if msg else None
        if not meta:
            raise web.HTTPNotFound(text="Вложение не найдено")
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="message_", dir=TEMP_DIR) as directory:
            target = Path(directory) / meta["file_name"]
            saved = await acc.client.download_media(msg, file=str(target))
            path = Path(saved) if saved else target
            if not path.is_file() or not path.resolve().is_relative_to(Path(directory).resolve()):
                raise web.HTTPNotFound(text="Не удалось скачать вложение из Telegram")
            header = "attachment; filename*=UTF-8''" + quote(meta["file_name"], safe="")
            resp = web.StreamResponse(headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": header,
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            })
            resp.content_length = path.stat().st_size
            await resp.prepare(request)
            try:
                with path.open("rb") as file:
                    while chunk := file.read(256 * 1024):
                        await resp.write(chunk)
                await resp.write_eof()
            except (ConnectionError, asyncio.CancelledError):
                # Browser closed the download; the temporary file is cleaned up.
                raise
            return resp
    key = ("media", acc.name, cid, mid)
    hit = CACHE.get(key)
    if not hit or time.time() - hit[0] > 3600:
        data, ctype = None, ""
        try:
            entity = await resolve(acc, cid)
            msg = await acc.client.get_messages(entity, ids=mid)
            kind = media_kind(msg) if msg else ""
            if kind:
                data = await acc.client.download_media(msg, file=bytes)
                ctype = media_mime(msg) or MEDIA_MIME_FALLBACK.get(kind, "application/octet-stream")
        except Exception as exc:
            print(f"[MEDIA] {key}: {type(exc).__name__}: {exc}", flush=True)
        if len(CACHE) > 300:
            CACHE.clear()
        hit = CACHE[key] = (time.time(), data, ctype)
    if not hit[1]:
        raise web.HTTPNotFound()
    return web.Response(body=hit[1], content_type=hit[2], headers={"Cache-Control": "private, max-age=3600"})


ERR_TEXT = {"UsernameOccupiedError": "username уже занят", "UsernameInvalidError": "недопустимый username",
            "UsernamePurchaseAvailableError": "этот username выставлен на продажу и недоступен для обычной установки",
            "FirstNameInvalidError": "недопустимое имя", "UsernameNotModifiedError": "username не изменился",
            "AboutTooLongError": "слишком длинное био", "BirthdayInvalidError": "недопустимая дата рождения"}
PRIV = {"bio": ("био", "InputPrivacyKeyAbout"), "birthday": ("день рождения", "InputPrivacyKeyBirthday"),
        "photo": ("фото профиля", "InputPrivacyKeyProfilePhoto")}


def err_text(exc):
    return ERR_TEXT.get(type(exc).__name__, f"{type(exc).__name__}: {exc}")


def parse_birthday(raw):
    if not raw:
        return None
    if getattr(types, "Birthday", None) is None or not hasattr(functions.account, "UpdateBirthdayRequest"):
        raise ValueError("эта версия Telethon не поддерживает день рождения: python -m pip install -U telethon")
    parts = raw.split(".")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError("формат ДД.ММ.ГГГГ или ДД.ММ")
    day, month = int(parts[0]), int(parts[1])
    year = int(parts[2]) if len(parts) == 3 else None
    try:
        when = datetime(year or 2000, month, day)
    except ValueError:
        raise ValueError("такой даты не существует")
    if year is not None and (year < 1900 or when > datetime.now()):
        raise ValueError("год вне допустимого диапазона или дата в будущем")
    return types.Birthday(day=day, month=month, year=year)


async def get_full(acc):
    full = (await acc.client(GetFullUserRequest("me"))).full_user
    b = getattr(full, "birthday", None)
    birthday = f"{b.day:02d}.{b.month:02d}" + (f".{b.year}" if getattr(b, "year", None) else "") if b else ""
    return {"bio": getattr(full, "about", "") or "", "birthday": birthday}


async def is_public(acc, key_cls):
    p = await acc.client(functions.account.GetPrivacyRequest(key=key_cls()))
    return any(type(r).__name__ == "PrivacyValueAllowAll" for r in getattr(p, "rules", []))


async def set_public(acc, which):
    allow, errors = getattr(types, "InputPrivacyValueAllowAll", None), []
    for w in which:
        label, cls_name = PRIV[w]
        key = getattr(types, cls_name, None)
        if key is None or allow is None:
            errors.append("Видимость: обнови Telethon (python -m pip install -U telethon)")
            break
        try:
            await acc.client(functions.account.SetPrivacyRequest(key=key(), rules=[allow()]))
            if not await is_public(acc, key):
                errors.append(f"Видимость ({label}): Telegram не подтвердил режим «Все»")
        except RPCError as exc:
            errors.append(f"Видимость ({label}): {err_text(exc)}")
    return errors


async def is_last_seen_hidden(acc):
    """Приватность «Время захода» = «Никто» (Nobody)."""
    p = await acc.client(functions.account.GetPrivacyRequest(key=types.InputPrivacyKeyStatusTimestamp()))
    names = [type(r).__name__ for r in getattr(p, "rules", [])]
    return "PrivacyValueDisallowAll" in names and "PrivacyValueAllowAll" not in names


async def set_last_seen_hidden(acc, hidden):
    """hidden=True — «Никто» (Nobody), False — снова «Все». -> список ошибок"""
    rule = types.InputPrivacyValueDisallowAll() if hidden else types.InputPrivacyValueAllowAll()
    try:
        await acc.client(functions.account.SetPrivacyRequest(key=types.InputPrivacyKeyStatusTimestamp(), rules=[rule]))
        if await is_last_seen_hidden(acc) != hidden:
            return ["Время захода: Telegram не подтвердил изменение"]
    except RPCError as exc:
        return [f"Время захода: {err_text(exc)}"]
    return []


async def full_info(acc):
    info = await get_full(acc)
    try:
        info["last_seen_hidden"] = await is_last_seen_hidden(acc)
    except Exception:
        info["last_seen_hidden"] = None
    for w, (_, cls_name) in PRIV.items():
        key = getattr(types, cls_name, None)
        try:
            info[f"{w}_public"] = await is_public(acc, key) if key else None
        except Exception:
            info[f"{w}_public"] = None
    return info


async def api_full(request):
    acc = get_acc(request)
    value = await full_info(acc)
    meta = ACCOUNT_PROFILE_CACHE.get(acc.name)
    if isinstance(meta, dict) and meta.get('telegram_id') == acc.me.id:
        meta.update({'bio': value['bio'], 'birthday': value['birthday']})
    return web.json_response(value)


async def api_last_seen(request):
    acc = get_acc(request)
    body = await request.json()
    errors = await set_last_seen_hidden(acc, bool(body.get("hidden")))
    try:
        hidden = await is_last_seen_hidden(acc)
    except Exception:
        hidden = None
    return web.json_response({"errors": errors, "last_seen_hidden": hidden})


async def api_open(request):
    acc = get_acc(request)
    errors = await set_public(acc, ["bio", "birthday", "photo"])
    return web.json_response({"errors": errors, **await full_info(acc)})

async def apply_template_to_account(acc, tpl):
    """Ставит на аккаунт: имя, фамилию, username, био, ДР, аватарку и открывает видимость.
    Возвращает список ошибок (пусто — всё ок)."""
    errors = []
    me = acc.me

    first = (tpl.get("first_name") or "").strip()
    last  = (tpl.get("last_name")  or "").strip()
    username = (tpl.get("username_new") or tpl.get("username") or "").strip().lstrip("@")

    if not first:
        errors.append("Имя: пустое (пропускаю имя/фамилию)")
    elif (first, last) != (me.first_name or "", me.last_name or ""):
        try:
            await acc.client(functions.account.UpdateProfileRequest(first_name=first, last_name=last))
        except RPCError as exc:
            errors.append("Имя: " + err_text(exc))

    if username and username != (me.username or ""):
        try:
            await acc.client(functions.account.UpdateUsernameRequest(username=username))
        except RPCError as exc:
            errors.append("Username: " + err_text(exc))

    # био
    bio = (tpl.get("bio") or "").strip()
    if bio:
        if len(bio) > 70:
            errors.append(f"Био: {len(bio)} символов, максимум 70")
        else:
            try:
                cur = await get_full(acc)
                if bio != cur["bio"]:
                    await acc.client(functions.account.UpdateProfileRequest(about=bio))
                    if (await get_full(acc))["bio"] != bio:
                        errors.append("Био: Telegram принял запрос, но проверка вернула другое описание")
            except RPCError as exc:
                errors.append("Био: " + err_text(exc))

    # день рождения
    bday_raw = (tpl.get("birthday") or "").strip()
    if bday_raw:
        try:
            cur = await get_full(acc)
            if bday_raw != cur["birthday"]:
                bd = parse_birthday(bday_raw)
                await acc.client(functions.account.UpdateBirthdayRequest(birthday=bd))
                if bd:
                    errors += await set_public(acc, ["birthday"])
        except ValueError as exc:
            errors.append(f"День рождения: {exc}")
        except RPCError as exc:
            errors.append("День рождения: " + err_text(exc))

    # аватарка
    # Архив фотографий не применяется автоматически при смене текстовых полей.
    # Управление аватарками доступно через отдельную ручную загрузку.

    # открыть видимость био и ДР
    if bio or bday_raw:
        errors += await set_public(acc, [w for w, v in (("bio", bio), ("birthday", bday_raw)) if v])

    acc.me = await acc.client.get_me()
    return errors


async def api_apply_template(request):
    acc = get_acc(request)
    tpl = await request.json()
    errors = await apply_template_to_account(acc, tpl)
    return web.json_response({"errors": errors, "account": ser_account(acc)})

async def api_profile(request):
    acc, data = get_acc(request), await request.json()
    me, errors = acc.me, []
    first = str(data.get("first_name", me.first_name or "")).strip()
    last = str(data.get("last_name", me.last_name or "")).strip()
    username = str(data.get("username", me.username or "")).strip().lstrip("@")
    if not first:
        fail("Имя не может быть пустым")
    if (first, last) != (me.first_name or "", me.last_name or ""):
        try:
            await acc.client(functions.account.UpdateProfileRequest(first_name=first, last_name=last))
        except RPCError as exc:
            errors.append("Имя: " + err_text(exc))
    if username != (me.username or ""):
        try:
            await acc.client(functions.account.UpdateUsernameRequest(username=username))
        except RPCError as exc:
            errors.append("Username: " + err_text(exc))
    if "bio" in data or "birthday" in data:
        cur = await get_full(acc)
        if "bio" in data:
            bio = str(data["bio"]).strip()
            if len(bio) > 70:
                errors.append(f"Био: {len(bio)} символов, максимум 70")
            elif bio != cur["bio"]:
                try:
                    await acc.client(functions.account.UpdateProfileRequest(about=bio))
                    if (await get_full(acc))["bio"] != bio:
                        errors.append("Био: Telegram принял запрос, но проверка вернула другое описание")
                except RPCError as exc:
                    errors.append("Био: " + err_text(exc))
        if "birthday" in data:
            raw = str(data["birthday"]).strip()
            if raw != cur["birthday"]:
                try:
                    bd = parse_birthday(raw)
                    await acc.client(functions.account.UpdateBirthdayRequest(birthday=bd))
                    if bd:
                        errors += await set_public(acc, ["birthday"])
                except ValueError as exc:
                    errors.append(f"День рождения: {exc}")
                except RPCError as exc:
                    errors.append("День рождения: " + err_text(exc))
    acc.me = await acc.client.get_me()
    invalidate_account_profile(acc)
    schedule_account_profile_refresh(acc, force=True)
    return web.json_response({"errors": errors, "account": ser_account(acc)})


def prepare_avatar(data):
    try:
        from PIL import Image
    except ImportError:
        return data
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        side = min(img.size)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))
        size = min(side, 1024)
        img = img.resize((size, size))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception as exc:
        raise ValueError(f"не удалось прочитать картинку: {exc}")


async def upload_avatar(acc, data, *, prepared=False):
    if not prepared:
        data = prepare_avatar(data)
    before = getattr(acc.me.photo, "photo_id", None)
    print(f"[AVATAR] {acc.name}: {len(data)} байт, фото до: {before}", flush=True)
    uploaded = await acc.client.upload_file(data, file_name="avatar.jpg")
    res = await acc.client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
    print(f"[AVATAR] {acc.name}: ответ Telegram: {type(res).__name__}", flush=True)
    me = await acc.client.get_me()
    after = getattr(me.photo, "photo_id", None)
    print(f"[AVATAR] {acc.name}: фото после: {after}", flush=True)
    if after is None or after == before:
        raise ValueError("Telegram принял запрос, но аватарка не изменилась")
    CACHE.pop(("me", acc.name), None)
    acc.me = me
    acc.avatar_ver = time.time()
    invalidate_account_profile(acc)
    schedule_account_profile_refresh(acc, force=True)


async def copy_profile_fields(acc, fields):
    requests = []
    if fields.get('first_name'):
        requests.append(('Имя', functions.account.UpdateProfileRequest(
            first_name=fields['first_name'], last_name=fields['last_name'])))
    for doc in reversed(fields.get('music', [])):
        requests.append(('Музыка', functions.account.SaveMusicRequest(
            id=types.InputDocument(doc['id'], doc['access_hash'], base64.b64decode(doc['file_reference'])), unsave=False)))
    if 'username' in fields and fields['username'] != acc.me.username:
        requests.append(('Юзернейм', functions.account.UpdateUsernameRequest(fields['username'])))
    if 'bio' in fields:
        requests.append(('Био', functions.account.UpdateProfileRequest(about=fields['bio'])))
    if 'birthday' in fields:
        requests.append(('День рождения', functions.account.UpdateBirthdayRequest(
            birthday=parse_birthday(fields['birthday']))))
    errors = []
    for label, request in requests:
        try:
            result = await asyncio.wait_for(acc.client(request), 30)
            if result is False:
                errors.append(f'{label}: Telegram не подтвердил изменение')
        except RPCError as exc:
            errors.append(f'{label}: {err_text(exc)}')
        except asyncio.TimeoutError:
            errors.append(f'{label}: Telegram не ответил. Проверьте значение в профиле.')
    try:
        acc.me = await asyncio.wait_for(acc.client.get_me(), 15)
    except (RPCError, asyncio.TimeoutError):
        pass
    errors.extend(await set_public(acc, ["bio", "birthday", "photo"]))
    errors.extend(await set_last_seen_hidden(acc, True))
    invalidate_account_profile(acc)
    schedule_account_profile_refresh(acc, force=True)
    return errors


async def clear_old_avatars(acc):
    photos = await asyncio.wait_for(acc.client.get_profile_photos('me', limit=None), 60)
    if photos:
        existing = [utils.get_input_photo(photo) for photo in photos]
        for start in range(0, len(existing), 50):
            batch = existing[start:start+50]
            deleted = await asyncio.wait_for(acc.client(functions.photos.DeletePhotosRequest(batch)), 30)
            if set(deleted) != {photo.id for photo in batch}:
                raise ValueError('Telegram не подтвердил удаление всех прежних аватарок')
        # The existing upload helper compares before/after photo IDs.
        me = await asyncio.wait_for(acc.client.get_me(), 20)
        if me is not None:
            acc.me = me
        acc.avatar_ver = time.time()


async def api_spam_status(request):
    acc = get_acc(request)
    if getattr(acc, 'spam_checking', False):
        fail('Проверка уже выполняется')
    acc.spam_checking = True
    try:
        async with acc.client.conversation('@SpamBot', timeout=20, exclusive=True) as chat:
            await chat.send_message('/start')
            message = await chat.get_response()
        raw = message.raw_text or ''
        lowered = raw.casefold()
        if re.search(r'no limits|no restrictions|нет ограничений|не ограничен|ограничений нет', lowered):
            status = 'Отсутствует'
        elif re.search(r'permanent|навсегда|бессроч|перманент', lowered):
            status = 'Вечный'
        elif re.search(r'geo.?restrict|country|your region|вашей стране|вашем регионе|по гео', lowered):
            status = 'По гео'
        elif re.search(r'until|до \d|временн|temporar', lowered):
            status = 'Временный'
        elif re.search(r'limit|restrict|огранич', lowered):
            status = 'Есть ограничение'
        else:
            status = 'Не определён'
        entry = {'label': status, 'reply': raw, 'checked_at': time.time()}
        SPAM_STATUS[str(acc.me.id)] = entry
        template_library.atomic_json_save(SPAM_STATUS_FILE, SPAM_STATUS)
        return web.json_response(entry)
    finally:
        acc.spam_checking = False


async def api_set_avatar(request):
    acc = get_acc(request)
    f = (await request.post()).get("file")
    if f is None or not hasattr(f, "file"):
        fail("Файл не получен")
    suffix = Path(f.filename or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        fail("Нужна картинка jpg/png/webp")
    try:
        await upload_avatar(acc, f.file.read())
    except ValueError as exc:
        fail(str(exc))
    errors = await set_public(acc, ["photo"])
    return web.json_response({"errors": errors, "account": ser_account(acc)})


# ---------- шаблон: загрузка из группы ----------
LEET = {'o': '0', 'i': '1', 'e': '3', 'a': '4', 's': '5', 't': '7', 'l': '1', 'g': '9'}


def vary_username(username, seed=0):
    """Генерирует похожий, но другой username. Детерминированно по (username, seed)."""
    if not username:
        return ""
    orig = username
    rng = random.Random(int(hashlib.md5(f"{username}:{seed}".encode()).hexdigest(), 16))
    for _ in range(40):
        u = orig
        s = rng.randint(0, 11)
        try:
            if s == 0:                                                      # + буква
                u += rng.choice("abcdefghijklmnopqrstuvwxyz")
            elif s == 1:                                                    # + цифра
                u += str(rng.randint(0, 9))
            elif s == 2:                                                    # + 2 цифры
                u += str(rng.randint(10, 99))
            elif s == 3:                                                    # обрезать хвост
                if len(u) > 8:
                    u = u[:-(rng.randint(1, 3))]
            elif s == 4:                                                    # leet
                idx = [i for i, c in enumerate(u) if c in LEET]
                if idx:
                    i = rng.choice(idx)
                    u = u[:i] + LEET[u[i]] + u[i + 1:]
            elif s == 5:                                                    # задвоить последний
                u = u + u[-1]
            elif s == 6:                                                    # поменять последнюю цифру
                if u[-1].isdigit():
                    u = u[:-1] + str((int(u[-1]) + 1) % 10)
            elif s == 7:                                                    # поменять любую цифру
                pos = [i for i, c in enumerate(u) if c.isdigit()]
                if pos:
                    i = rng.choice(pos)
                    u = u[:i] + str((int(u[i]) + rng.randint(1, 9)) % 10) + u[i + 1:]
            elif s == 8:                                                    # переставить 2 последних
                if len(u) > 2:
                    u = u[:-2] + u[-1] + u[-2]
            elif s == 9:                                                    # добавить "_"
                if len(u) < 31:
                    pos = rng.randint(1, len(u) - 1)
                    u = u[:pos] + '_' + u[pos:]
            elif s == 10:                                                   # задвоить случайную
                if len(u) < 32:
                    pos = rng.randint(0, len(u) - 1)
                    u = u[:pos + 1] + u[pos] + u[pos + 1:]
            elif s == 11:                                                   # заменить последнюю на гласную
                if len(u) > 3:
                    u = u[:-1] + rng.choice("aeiouy")
        except Exception:
            continue
        if 5 <= len(u) <= 32 and re.fullmatch(r"[A-Za-z0-9_]+", u) and u != orig:
            return u
    return (orig + str(rng.randint(100, 999)))[:32]


async def template_acc():
    for a in ACCOUNTS.values():
        if a.me is not None:
            return a
    raise ValueError("Нет подключённых аккаунтов")


async def template_user_full(acc, user):
    try:
        full = (await acc.client(GetFullUserRequest(user))).full_user
        bio = getattr(full, "about", "") or ""
        b = getattr(full, "birthday", None)
        bday = ""
        if b:
            bday = f"{b.day:02d}.{b.month:02d}" + (f".{b.year}" if getattr(b, "year", None) else "")
        return bio, bday
    except Exception as exc:
        print(f"[TEMPLATE] full {getattr(user, 'id', '?')}: {type(exc).__name__}: {exc}", flush=True)
        return "", ""


async def template_avatar(acc, user):
    """Archive up to five photos; keep their REAL Telegram upload dates when present."""
    folder = TEMPLATE_AVATARS / str(int(user.id))
    meta_path = folder / 'metadata.json'
    cached = template_library.json_load(meta_path)
    if not isinstance(cached, dict):
        cached = {}
    saved_count = int(cached.get('saved_avatar_count') or 0)
    cached_files = [(folder / f'{n:02d}.jpg') for n in range(1, saved_count + 1)]
    if (cached.get('managed') and all(path.is_file() for path in cached_files)):
        return template_library.photo_metadata(cached.get('avatar_count'), saved_count,
                                               cached.get('photo_dates'), cached.get('photo_sources'))
    if (saved_count and all(path.is_file() for path in cached_files)
            and len(cached.get('photo_dates') or []) >= saved_count):
        return template_library.photo_metadata(cached.get('avatar_count'), saved_count,
                                               cached.get('photo_dates'), cached.get('photo_sources'))
    folder.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    try:
        photos = await acc.client.get_profile_photos(user, limit=5)
        actual = int(getattr(photos, 'total', len(photos)))
        if actual < 2:
            return template_library.photo_metadata(actual, 0)
        dates, origins = [], []
        for idx, photo in enumerate(photos[:5], start=1):
            target = folder / f'{idx:02d}.jpg'
            if not (target.is_file() and target.stat().st_size > 200):
                result = await acc.client.download_media(photo, file=str(target))
                if not (result and target.is_file() and target.stat().st_size > 200):
                    target.unlink(missing_ok=True)
                    break
            downloaded += 1
            date = getattr(photo, 'date', None)
            dates.append(date.isoformat() if date is not None else '')
            origins.append('telegram')
        summary = template_library.photo_metadata(actual, downloaded, dates, origins)
        template_library.atomic_json_save(meta_path, summary)
        return summary
    except Exception as exc:
        print(f'[TEMPLATE] photos {user.id}: {type(exc).__name__}: {exc}', flush=True)
        return template_library.photo_metadata(cached.get('avatar_count', 0), downloaded,
                                               cached.get('photo_dates'), cached.get('photo_sources'))


def _participant_uid(p):
    """Достаёт user_id из разных типов ChannelParticipant."""
    peer = getattr(p, "peer", None)
    if peer is not None and hasattr(peer, "user_id"):
        return peer.user_id
    return getattr(p, "user_id", None)


async def fetch_participants(acc, entity, offset, limit):
    """Забирает одну страницу участников начиная с offset.
    Для супергрупп/каналов — низкоуровневый GetParticipantsRequest (без refetch с нуля).
    Для базовых групп и прочего — fallback через iter_participants с ручным скипом."""
    if isinstance(entity, types.Channel):
        res = await acc.client(functions.channels.GetParticipantsRequest(
            channel=entity,
            filter=types.ChannelParticipantsSearch(""),
            offset=offset,
            limit=limit,
            hash=0,
        ))
        user_map = {u.id: u for u in res.users}
        out = []
        for p in res.participants:
            uid = _participant_uid(p)
            if uid and uid in user_map:
                out.append(user_map[uid])
        return out

    # Базовые группы и всё остальное
    out, i = [], 0
    async for u in acc.client.iter_participants(entity):
        if i >= offset:
            out.append(u)
        i += 1
        if len(out) >= limit:
            break
    return out


TEMPLATE_SEEN_FILE = BASE_DIR / "template_seen.json"
TEMPLATE_SEEN: dict[str, set] = {}      # username группы -> id профилей, которые уже показывали (переживает перезапуск)
try:
    TEMPLATE_SEEN = {k: set(v) for k, v in json.loads(TEMPLATE_SEEN_FILE.read_text(encoding="utf-8")).items()}
except Exception:
    TEMPLATE_SEEN = {}


def save_template_seen():
    try:
        TEMPLATE_SEEN_FILE.write_text(json.dumps({k: sorted(v) for k, v in TEMPLATE_SEEN.items()}), encoding="utf-8")
    except Exception as exc:
        print(f"[TEMPLATE] не удалось сохранить список показанных: {exc}", flush=True)


async def fetch_template_candidates(acc, entity, offset, limit):
    """Read a predictable page of members, advancing by CHECKED members, not matches.

    Telegram may expose fewer members than the published group size. We only
    report exhaustion of the accessible list, never promise an exact target.
    """
    offset = max(0, int(offset))
    limit = max(1, min(80, int(limit)))
    if isinstance(entity, types.Channel):
        res = await acc.client(functions.channels.GetParticipantsRequest(
            channel=entity, filter=types.ChannelParticipantsSearch(''),
            offset=offset, limit=limit, hash=0))
        found = {u.id: u for u in res.users}
        ids = [_participant_uid(p) for p in res.participants]
        people = [found.get(uid) for uid in ids]
        total = max(0, int(getattr(res, 'count', 0) or 0))
        consumed = len(ids)
        done = consumed == 0 or (total > 0 and offset + consumed >= total) or (total == 0 and consumed < limit)
        return people, consumed, done
    # A normal group may not have a channel-style members API.
    people = []
    index = 0
    async for u in acc.client.iter_participants(entity):
        if index >= offset:
            people.append(u)
        index += 1
        if len(people) >= limit:
            break
    return people, len(people), len(people) < limit


async def _load_template_once(link, offset, limit=TEMPLATE_PAGE):
    """Find up to `limit` eligible profiles in one bounded scan of group members.

    `offset` and `next_offset` describe the number of PARTICIPANTS EXAMINED.
    A page with zero eligible results can still have more members to inspect.
    """
    acc = await template_acc()
    match = re.fullmatch(r'(?:https?://)?t\.me/([A-Za-z0-9_]+)/?', link.strip())
    if not match:
        raise ValueError(f'Неверная ссылка: {link}')
    username = match.group(1)
    if username not in TEMPLATE_CACHE and len(TEMPLATE_CACHE) > 20:
        TEMPLATE_CACHE.pop(next(iter(TEMPLATE_CACHE)))
    cache = TEMPLATE_CACHE.setdefault(username, {'full': {}})
    try:
        entity = await acc.client.get_entity(username)
    except Exception as exc:
        raise ValueError(f'Не удалось открыть {username}: {type(exc).__name__}: {exc}')
    try:
        page, count, end_of_page = await fetch_template_candidates(acc, entity, offset, 60)
    except FloodWaitError as exc:
        raise ValueError(f'FloodWait {exc.seconds} сек. — поиск можно продолжить позже')
    except Exception as exc:
        raise ValueError(f'Не удалось прочитать участников: {type(exc).__name__}: {exc}')
    sem = asyncio.Semaphore(4)

    async def prepare(u):
        if (u is None or getattr(u, 'bot', False) or getattr(u, 'deleted', False)
                or not getattr(u, 'username', None)
                or not isinstance(getattr(u, 'photo', None), types.UserProfilePhoto)):
            return None
        if u.id in cache['full']:
            bio, birthday = cache['full'][u.id]
        else:
            async with sem:
                bio, birthday = await template_user_full(acc, u)
            cache['full'][u.id] = (bio, birthday)
        if not bio or not birthday:
            return None
        async with sem:
            photos = await template_avatar(acc, u)
        if not photos['eligible']:
            return None
        return {
            'id': u.id,
            'first_name': u.first_name or '',
            'last_name': u.last_name or '',
            'name': display_name(u) or (u.username or str(u.id)),
            'username': u.username,
            'username_new': vary_username(u.username, seed=u.id),
            'bio': bio,
            'birthday': birthday,
            'has_avatar': True,
            'avatar_count': photos['avatar_count'],
            'saved_avatar_count': photos['saved_avatar_count'],
            'photo_dates': photos.get('photo_dates', []),
            'photo_sources': photos.get('photo_sources', []),
            'last_avatar_change': photos.get('last_avatar_change', ''),
            'is_bot': False,
        }

    matches = []
    consumed = 0
    # Small batches stop the photo downloads as soon as the requested number
    # of eligible profiles is found (at most 3 extra in-flight candidates).
    for start in range(0, len(page), 4):
        chunk = page[start:start + 4]
        results = await asyncio.gather(*(prepare(u) for u in chunk), return_exceptions=True)
        for result in results:
            consumed += 1
            if isinstance(result, Exception):
                logging.warning('[TEMPLATE] prep failed: %s: %s', type(result).__name__, result)
            elif result is not None:
                matches.append(result)
            if len(matches) >= limit:
                break
        if len(matches) >= limit:
            break
    # If the target was filled mid-page, continue from that person next time.
    next_offset = max(0, offset) + consumed
    exhausted = (not page or (end_of_page and consumed >= count))
    return matches, next_offset, exhausted


async def load_template_page(link, offset, limit=TEMPLATE_PAGE):
    """Bounded candidate scan; the browser repeats until enough MATCHES exist."""
    return await _load_template_once(link, offset, limit)


async def api_template_load(request):
    data = await request.json()
    link = str(data.get('link', '')).strip()
    try:
        offset = max(0, int(data.get('offset', 0)))
        limit = max(1, min(60, int(data.get('limit', TEMPLATE_PAGE))))
    except (TypeError, ValueError):
        fail('Неверное количество или позиция поиска')
    try:
        items, next_offset, done = await load_template_page(link, offset, limit=limit)
    except ValueError as exc:
        fail(str(exc))
    except Exception as exc:
        fail(f'{type(exc).__name__}: {exc}')
    return web.json_response({'items': items, 'offset': offset,
                              'next_offset': next_offset,
                              'checked': max(0, next_offset - offset),
                              'done': done})


async def api_template_avatar(request):
    uid = request.match_info["uid"]
    if not uid.isdigit():
        raise web.HTTPNotFound()
    path = TEMPLATE_AVATARS / uid / "01.jpg"
    if not path.is_file():
        path = TEMPLATE_AVATARS / f"{uid}.jpg"  # миграция старого кэша
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


async def api_template_photo(request):
    uid = request.match_info["uid"]
    num = int(request.match_info["num"])
    if not uid.isdigit() or not 1 <= num <= 5:
        raise web.HTTPNotFound()
    path = TEMPLATE_AVATARS / uid / f"{num:02d}.jpg"
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


def _template_library_item(uid):
    roster = template_library.json_load(TEMPLATE_ROSTER_FILE, default={})
    if not isinstance(roster, dict) or not isinstance(roster.get('items'), list):
        raise web.HTTPNotFound(text='Профиль не найден в списке')
    for item in roster['items']:
        if isinstance(item, dict) and str(item.get('id')) == str(uid):
            return roster, item
    raise web.HTTPNotFound(text='Профиль не найден в списке')


def _template_refresh_meta(uid, roster, item, dates, sources):
    folder = TEMPLATE_AVATARS / str(uid)
    files = sorted(folder.glob('[0-9][0-9].jpg'))
    meta = template_library.json_load(folder / 'metadata.json', default={})
    if not isinstance(meta, dict):
        meta = {}
    # Preserve the number known to Telegram even when a local copy is removed.
    count = max(len(files), int(meta.get('avatar_count') or item.get('avatar_count') or 0))
    values = template_library.photo_metadata(count, len(files), dates, sources)
    item.update({k: v for k, v in values.items() if k != 'eligible'})
    item['has_avatar'] = bool(files)
    template_library.atomic_json_save(folder / 'metadata.json', {**values, 'managed': True})
    template_library.atomic_json_save(TEMPLATE_ROSTER_FILE, template_library.sanitize_roster(roster))
    return item


async def api_template_photo_add(request):
    """Upload one image into local library ONLY; does not change Telegram profiles."""
    uid = request.match_info['uid']
    if not uid.isdigit():
        raise web.HTTPNotFound()
    roster, item = _template_library_item(uid)
    folder = TEMPLATE_AVATARS / uid
    folder.mkdir(parents=True, exist_ok=True)
    photos = sorted(folder.glob('[0-9][0-9].jpg'))
    if len(photos) >= 5:
        fail('Для одного шаблона допускается максимум 5 фотографий')
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != 'file':
        fail('Файл не получен')
    suffix = Path(part.filename or '').suffix.lower()
    if suffix not in ('.jpg', '.jpeg', '.png', '.webp'):
        fail('Поддерживаются JPG, PNG и WEBP')
    data = bytearray()
    while chunk := await part.read_chunk(size=65536):
        data.extend(chunk)
        if len(data) > 6 * 1024 * 1024:
            fail('Размер изображения не должен превышать 6 МБ')
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > 24_000_000:
            fail('Слишком большое изображение')
        im = ImageOps.exif_transpose(im).convert('RGB')
        im.thumbnail((1200, 1200))
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=90)
    except (OSError, ValueError, ImportError) as exc:
        fail(f'Не удалось обработать изображение: {exc}')
    target = folder / f'{len(photos)+1:02d}.jpg'
    with tempfile.NamedTemporaryFile(dir=folder, suffix='.part', delete=False) as tmp:
        try:
            tmp.write(buf.getvalue())
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(tmp.name, target)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    old = template_library.photo_metadata_from_roster_item(item)
    dates = old['photo_dates'] + [datetime.now().astimezone().isoformat()]
    sources = old['photo_sources'] + ['library']
    updated = _template_refresh_meta(uid, roster, item, dates, sources)
    return web.json_response({'ok': True, 'item': updated})


async def api_template_photo_delete(request):
    """Remove selected LOCAL archived photo and close its index gap."""
    uid = request.match_info['uid']
    if not uid.isdigit():
        raise web.HTTPNotFound()
    roster, item = _template_library_item(uid)
    try:
        num = int(request.match_info['num'])
    except (ValueError, TypeError):
        raise web.HTTPNotFound()
    folder = TEMPLATE_AVATARS / uid
    photos = sorted(folder.glob('[0-9][0-9].jpg'))
    if not 1 <= num <= len(photos):
        raise web.HTTPNotFound(text='Аватарка отсутствует')
    (folder / f'{num:02d}.jpg').unlink()
    for idx in range(num + 1, len(photos) + 1):
        (folder / f'{idx:02d}.jpg').rename(folder / f'{idx-1:02d}.jpg')
    old = template_library.photo_metadata_from_roster_item(item)
    dates = old['photo_dates']; dates.pop(num-1)
    sources = old['photo_sources']; sources.pop(num-1)
    updated = _template_refresh_meta(uid, roster, item, dates, sources)
    return web.json_response({'ok': True, 'item': updated})


async def api_template_roster_get(request):
    state = template_library.json_load(TEMPLATE_ROSTER_FILE, default={})
    return web.json_response(state if isinstance(state, dict) else {})


async def api_template_roster_save(request):
    try:
        roster = template_library.sanitize_roster(await request.json())
    except ValueError as exc:
        fail(str(exc))
    template_library.atomic_json_save(TEMPLATE_ROSTER_FILE, roster)
    return web.json_response({"ok": True, "count": len(roster["items"])})


async def api_template_export(request):
    """Download a ZIP of photos already saved for the selected roster."""
    roster = template_library.json_load(TEMPLATE_ROSTER_FILE, default={})
    if not isinstance(roster, dict) or not roster.get('items'):
        fail('Сначала создай и сохрани список профилей')
    with tempfile.TemporaryDirectory(prefix='template_export_') as tmp:
        path = Path(tmp) / 'template_avatars.zip'
        profiles, photos = await asyncio.to_thread(
            template_library.write_photo_archive, TEMPLATE_AVATARS, roster, path)
        if not photos:
            fail('Нет сохранённых фотографий для архива')
        response = web.StreamResponse(headers={
            'Content-Type': 'application/zip',
            'Content-Disposition': 'attachment; filename="template_avatars.zip"',
            'Cache-Control': 'no-store',
            'X-Profile-Count': str(profiles),
            'X-Photo-Count': str(photos),
        })
        await response.prepare(request)
        with path.open('rb') as inp:
            while data := inp.read(256 * 1024):
                await response.write(data)
        await response.write_eof()
        return response


async def api_template_exclude(request):
    """Удаляет локальные фото исключённого человека (без запросов в Telegram)."""
    data = await request.json()
    try:
        uid = int(data.get("id"))
    except (ValueError, TypeError):
        fail("Неверный ID профиля")
    if uid <= 0:
        fail("Неверный ID профиля")
    folder = TEMPLATE_AVATARS / str(uid)
    if folder.is_dir():
        shutil.rmtree(folder)
    (TEMPLATE_AVATARS / f"{uid}.jpg").unlink(missing_ok=True)
    return web.json_response({"ok": True})


async def api_template_roster_check(request):
    """Только проверяет локальный архив: ничего в Telegram не меняет."""
    state = template_library.json_load(TEMPLATE_ROSTER_FILE, default={})
    if not isinstance(state, dict):
        state = {}
    items = state.get("items", [])
    ready, problems = [], []
    for item in items:
        uid = int(item["id"])
        folder = TEMPLATE_AVATARS / str(uid)
        saved = sum(1 for n in range(1, 6) if (folder / f"{n:02d}.jpg").is_file())
        missing = [key for key in ("username_new", "bio", "birthday") if not item.get(key)]
        if saved < 2:
            missing.append("2+ фото")
        if missing:
            problems.append({"id": uid, "missing": missing})
        else:
            ready.append(uid)
    return web.json_response({"total": len(items), "ready": len(ready),
                              "ready_ids": ready, "problems": problems})


# ---------- чаты ----------
async def api_history(request):
    acc, cid = get_acc(request), int(request.match_info["chat_id"])
    entity = await resolve(acc, cid)
    msgs = await acc.client.get_messages(entity, limit=60)
    blocked = None
    if tl_name(entity) == "InputPeerUser":
        try:
            blocked = bool(getattr((await acc.client(GetFullUserRequest(entity))).full_user, "blocked", False))
        except Exception as exc:
            print(f"[HISTORY] статус блокировки {acc.name}:{cid}: {type(exc).__name__}: {exc}", flush=True)
    return web.json_response({"messages": [ser_message(m) for m in reversed(msgs)], "blocked": blocked})


async def api_block(request):
    acc, cid = get_acc(request), int(request.match_info["chat_id"])
    block = bool((await request.json()).get("block", True))
    entity = await resolve(acc, cid)
    if not tl_name(entity) == "InputPeerUser":
        fail("Блокировать можно только пользователей")
    await acc.client(functions.contacts.BlockRequest(id=entity) if block
                     else functions.contacts.UnblockRequest(id=entity))
    print(f"[BLOCK] {acc.name}: {cid} {'заблокирован' if block else 'разблокирован'}", flush=True)
    return web.json_response({"blocked": block})


async def api_click(request):
    """Нажатие inline-кнопки (callback) под сообщением бота."""
    acc, cid = get_acc(request), int(request.match_info["chat_id"])
    data = await request.json()
    try:
        mid, row, col = int(data["message_id"]), int(data["row"]), int(data["col"])
    except (KeyError, TypeError, ValueError):
        fail("Неверные параметры кнопки")
    msg = await acc.client.get_messages(await resolve(acc, cid), ids=mid)
    if not msg or not is_inline_markup(getattr(msg, "reply_markup", None)):
        fail("У сообщения нет inline-кнопок")
    try:
        btn = msg.reply_markup.rows[row].buttons[col]
    except IndexError:
        fail("Такой кнопки уже нет — сообщение изменилось")
    if btn_kind(btn) != "cb":
        fail(f"Этот тип кнопки ({tl_name(btn)}) в панели не поддерживается")
    try:
        try:
            res = await msg.click(row, col)
        except (AttributeError, TypeError, ValueError, NotImplementedError):   # msg.click не понял кнопку — шлём запрос напрямую
            data = btn_data(btn)
            if data is None:
                raise
            res = await acc.client(functions.messages.GetBotCallbackAnswerRequest(
                peer=await resolve(acc, cid), msg_id=mid, data=data))
    except BotResponseTimeoutError:
        return web.json_response({"note": "Кнопка нажата, бот не ответил", "alert": False, "url": ""})
    if isinstance(res, str):
        return web.json_response({"note": "", "alert": False, "url": res})
    return web.json_response({"note": getattr(res, "message", "") or "", "alert": bool(getattr(res, "alert", False)),
                              "url": getattr(res, "url", "") or ""})


async def api_webview(request):
    """Кнопка-мини-приложение (WebView): просим у Telegram ссылку для входа и отдаём её в браузер."""
    acc, cid = get_acc(request), int(request.match_info["chat_id"])
    data = await request.json()
    try:
        mid, row, col = int(data["message_id"]), int(data["row"]), int(data["col"])
    except (KeyError, TypeError, ValueError):
        fail("Неверные параметры кнопки")
    entity = await resolve(acc, cid)
    msg = await acc.client.get_messages(entity, ids=mid)
    if not msg or not is_inline_markup(getattr(msg, "reply_markup", None)):
        fail("У сообщения нет inline-кнопок")
    try:
        btn = msg.reply_markup.rows[row].buttons[col]
    except IndexError:
        fail("Такой кнопки уже нет — сообщение изменилось")
    if btn_kind(btn) != "web":
        fail(f"Это не мини-приложение ({tl_name(btn)})")
    url = getattr(btn, "url", None)
    if "Simple" in tl_name(btn):
        res = await acc.client(functions.messages.RequestSimpleWebViewRequest(bot=entity, platform="web", url=url))
    else:
        res = await acc.client(functions.messages.RequestWebViewRequest(peer=entity, bot=entity, platform="web", url=url))
    link = getattr(res, "url", "") or ""
    if not link:
        fail("Telegram не вернул ссылку на мини-приложение")
    return web.json_response({"url": link})


async def api_miniapp(request):
    """Получить у Telegram ссылку для входа в мини-приложение от имени аккаунта."""
    acc = get_acc(request)
    username, short, param = multi.parse_miniapp_link((await request.json()).get("link", ""))
    ent = await acc.client.get_input_entity(username)
    if short:
        from telethon.tl.types import InputBotAppShortName
        res = await acc.client(functions.messages.RequestAppWebViewRequest(
            peer=ent, app=InputBotAppShortName(bot_id=utils.get_input_user(ent), short_name=short),
            platform="web", start_param=param or None))
    else:   # главное мини-приложение бота (t.me/bot?startapp=...)
        res = await acc.client(functions.messages.RequestMainWebViewRequest(
            peer=ent, bot=ent, platform="web", start_param=param or None))
    link = getattr(res, "url", "") or ""
    if not link:
        fail("Telegram не вернул ссылку на мини-приложение")
    print(f"[MINIAPP] {acc.name}: @{username}{'/' + short if short else ''}", flush=True)
    return web.json_response({"url": link})


async def sound(request):
    f = BASE_DIR / "sound.mp3"
    if not f.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Cache-Control": "private, max-age=3600"})


async def api_send(request):
    acc, cid = get_acc(request), int(request.match_info["chat_id"])
    text = str((await request.json()).get("text", "")).strip()
    if not text:
        fail("Пустое сообщение")
    entity = await resolve(acc, cid)
    sent = await acc.client.send_message(entity, text)
    try:
        await acc.client.send_read_acknowledge(entity)
    except Exception:
        pass
    key = f"{acc.name}:{cid}"
    card = INBOX.get(key)
    if card:
        card["unread"] = 0
    else:   # переписка начата из панели — добавляем её в список слева (только личные, группы туда не попадают)
        try:
            ent = await acc.client.get_entity(entity)
            if isinstance(ent, types.User) and not ent.bot:   # чаты с ботами во «Входящие» не добавляем
                INBOX[key] = {"account": acc.name, "chat_id": cid, "unread": 0, "title": display_name(ent) or str(cid),
                              "kind": "user", "last_text": text,
                              "last_sender": "", "date": sent.date.timestamp() if sent.date else time.time()}
        except Exception as exc:
            print(f"[SEND] карточка чата {key}: {type(exc).__name__}: {exc}", flush=True)
    return web.json_response({"message": ser_message(sent)})


async def api_read(request):
    acc, cid = get_acc(request), int(request.match_info["chat_id"])
    try:
        await acc.client.send_read_acknowledge(await resolve(acc, cid))
    except Exception:
        pass
    card = INBOX.get(f"{acc.name}:{cid}")
    if card:
        card["unread"] = 0
    publish({"type": "read"})
    return web.json_response({"ok": True})


def parse_peer_query(raw):
    """@username / t.me/username / +телефон / числовой id -> то, что понимает get_entity."""
    s = str(raw or "").strip()
    if not s:
        raise ValueError("Укажи username, ссылку или номер")
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", s, re.I)
    if m:
        return m.group(1), False
    if re.fullmatch(r"\+\d{5,15}", s):
        return s, True                      # телефон
    if re.fullmatch(r"\d{5,15}", s):
        return int(s), True                 # id пользователя
    s = s.lstrip("@")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", s):
        return s, False
    raise ValueError("Не похоже на username (нужен @username, t.me/username, +номер или id)")


async def api_resolve(request):
    """Находит человека для новой переписки: возвращает chat_id, с которым дальше работают обычные /chats/ эндпоинты."""
    acc, data = get_acc(request), await request.json()
    try:
        target, weak = parse_peer_query(data.get("query"))
    except ValueError as exc:
        fail(str(exc))
    try:
        ent = await acc.client.get_entity(target)
    except FloodWaitError as exc:
        fail(f"FloodWait {exc.seconds} сек. — попробуй позже")
    except (ValueError, RPCError) as exc:
        if weak:
            fail("Аккаунт не знает этого пользователя: по номеру/id можно писать только тем, кто уже в контактах "
                 "или в диалогах этого аккаунта. Используй @username")
        fail("Пользователь не найден" if isinstance(exc, ValueError) or "Username" in type(exc).__name__
             else f"{type(exc).__name__}: {exc}")
    if isinstance(ent, types.User):
        if ent.is_self:
            fail("Это сам аккаунт — выбери другого человека")
        if getattr(ent, "deleted", False):
            fail("Аккаунт удалён")
        kind = "user"
    elif isinstance(ent, (types.Chat, types.Channel)):
        if isinstance(ent, types.Channel) and not ent.megagroup and not (ent.creator or ent.admin_rights):
            fail("Это канал: писать в него могут только админы. Нужна группа (чат)")
        if getattr(ent, "left", False) or getattr(ent, "deactivated", False):
            fail("Аккаунт не состоит в этой группе — сначала подпиши его во вкладке «Каналы»")
        kind = "group"
    else:
        fail("Не удалось определить, кому писать")
    return web.json_response({"chat_id": utils.get_peer_id(ent),
                              "title": display_name(ent) or getattr(ent, "username", None) or str(ent.id),
                              "kind": kind, "username": getattr(ent, "username", None) or ""})


async def api_inbox(request):
    items = sorted(INBOX.values(), key=lambda c: c["date"], reverse=True)
    return web.json_response({"items": items, "unread": sum(c["unread"] for c in items)})


async def load_recent_dialogs(acc, want):
    """Берёт последние диалоги аккаунта (личные, не боты) и добавляет/обновляет их карточки во «Входящих»."""
    added = 0
    async for d in acc.client.iter_dialogs(limit=max(want * 3, want + 20)):
        if added >= want:
            break
        ent = d.entity
        if not isinstance(ent, types.User) or ent.bot or ent.is_self or getattr(ent, "deleted", False):
            continue
        cid = utils.get_peer_id(ent)
        msg = d.message
        text = ((msg.message if msg else "") or (media_type(msg) if msg else "")) or "Нет сообщений"
        card = INBOX.setdefault(f"{acc.name}:{cid}", {"account": acc.name, "chat_id": cid, "unread": 0})
        card.update(title=display_name(ent) or str(cid), kind="user", last_text=text,
                    last_sender="" if not msg or msg.out else (display_name(ent) or ""),
                    date=msg.date.timestamp() if msg and msg.date else time.time())
        if msg and not msg.out:
            card["unread"] = d.unread_count or card["unread"]
        added += 1
    return added


async def api_inbox_load(request):
    """Подгружает последние N личных чатов для выбранных аккаунтов и добавляет их в список слева."""
    data = await request.json()
    names = [str(n) for n in (data.get("accounts") or [])]
    try:
        count = int(data.get("count", 20))
    except (TypeError, ValueError):
        count = 20
    count = max(1, min(100, count))
    accs = [ACCOUNTS[n] for n in names if n in ACCOUNTS and ACCOUNTS[n].me is not None]
    if not accs:
        fail("Выбери хотя бы один подключённый аккаунт")
    total, errors = 0, []
    for acc in accs:
        try:
            total += await load_recent_dialogs(acc, count)
        except FloodWaitError as exc:
            errors.append(f"{acc.name}: FloodWait {exc.seconds} сек.")
        except Exception as exc:
            errors.append(f"{acc.name}: {type(exc).__name__}: {exc}")
    print(f"[INBOX] загрузка диалогов: аккаунтов {len(accs)}, добавлено/обновлено {total}, "
          f"ошибок {len(errors)}", flush=True)
    publish({"type": "read"})
    return web.json_response({"added": total, "errors": errors})


async def api_events(request):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q = asyncio.Queue(maxsize=200)
    SUBSCRIBERS.add(q)
    try:
        await resp.write(b": hello\n\n")
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except ConnectionError:
        pass
    finally:
        SUBSCRIBERS.discard(q)
    return resp


# ---------- кнопки ----------
BTN_CHECKS: dict[str, dict] = {}
BTN_JOB: dict = {}
BTN_TASK = None


def parse_telegram_url(url):
    url = str(url or "").strip().rstrip("/")
    match = re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]+)(?:/(\d+))?", url)
    if not match:
        raise ValueError(f"Неверная ссылка Telegram: {url or '(пусто)'}")
    return match.group(1), int(match.group(2)) if match.group(2) else None


async def subscribed_to_all(acc, channels):
    for ch in channels:
        inp = await acc.client.get_input_entity(ch)
        if not isinstance(inp, types.InputPeerChannel):
            raise ValueError(f"{ch}: это не канал")
        try:
            await acc.client(functions.channels.GetParticipantRequest(inp, types.InputPeerSelf()))
        except (UserNotParticipantError, ChannelPrivateError):
            return False
    return True


async def api_buttons_check(request):
    data = await request.json()
    try:
        channel, message_id = parse_telegram_url(data.get("post"))
    except ValueError as exc:
        fail(str(exc))
    extra = []
    for raw in data.get("channels") or []:
        if not str(raw).strip():
            continue
        try:
            ch, _ = parse_telegram_url(raw)
        except ValueError as exc:
            fail(str(exc))
        if ch.lower() not in [x.lower() for x in extra]:
            extra.append(ch)
    accs = [a for a in ACCOUNTS.values() if a.me is not None and general_account(a.name)]
    if not accs:
        fail("Нет подключённых аккаунтов с меткой «Общий»")

    sem, errors = asyncio.Semaphore(8), {}

    async def one(a):
        async with sem:
            try:
                return a, await subscribed_to_all(a, extra)
            except FloodWaitError as exc:
                errors[a.name] = f"FloodWait {exc.seconds} сек."
            except Exception as exc:
                errors[a.name] = f"{type(exc).__name__}: {exc}"
            return a, False

    ok = [a for a, good in await asyncio.gather(*(one(a) for a in accs)) if good]
    print(f"[BUTTONS] проверка: каналов {len(extra)}, подписаны {len(ok)} из {len(accs)}, ошибок {len(errors)}",
          flush=True)

    post = None
    if message_id is not None:
        for a in ok[:3]:
            try:
                msg = await a.client.get_messages(await a.client.get_entity(channel), ids=message_id)
                if msg:
                    post = {"text": (msg.text or "")[:300],
                            "buttons": [[b.text for b in row] for row in (msg.buttons or [])]}
                    break
            except Exception as exc:
                print(f"[BUTTONS] {a.name}: не удалось прочитать пост: {type(exc).__name__}: {exc}", flush=True)

    cid = secrets.token_hex(6)
    while len(BTN_CHECKS) >= 20:
        BTN_CHECKS.pop(next(iter(BTN_CHECKS)))
    BTN_CHECKS[cid] = {"accounts": [a.name for a in ok], "used": set(), "channel": channel, "message_id": message_id}
    return web.json_response({
        "id": cid, "mode": "click" if message_id is not None else "join", "channel": channel,
        "total": len(accs), "subscribed": len(ok), "available": len(ok), "post": post,
        "errors_count": len(errors), "errors": [f"{n}: {m}" for n, m in list(errors.items())[:5]]})


async def press_one(acc, channel, message_id):
    entity = await acc.client.get_entity(channel)
    if message_id is None:
        result = await acc.client(JoinChannelRequest(entity))
        count = await channels.view_after_join(acc.client, result, entity)
        return "подписался на канал" + (f" · просмотрено постов: {count}" if count else "")
    msg = await acc.client.get_messages(entity, ids=message_id)
    if not msg:
        raise ValueError("пост не найден или недоступен")
    if not msg.buttons:
        raise ValueError("у поста нет кнопок")
    try:
        res = await msg.click(0, 0)
    except BotResponseTimeoutError:
        return "нажато, бот не ответил"
    if isinstance(res, str):
        return "кнопка-ссылка: " + res
    text = getattr(res, "message", None)
    return "нажато" + (f": {text}" if text else "")


async def run_buttons_job(job, chk, dmin, dmax):
    try:
        for i, it in enumerate(job["items"]):
            if job["stop"]:
                for rest in job["items"][i:]:
                    rest["state"] = "skip"
                break
            if i:
                await asyncio.sleep(random.uniform(dmin, dmax))
            acc = ACCOUNTS.get(it["account"])
            it["state"] = "active"
            try:
                if acc is None or acc.me is None:
                    raise ValueError("аккаунт не подключён")
                if not general_account(acc.name):
                    it.update(state="skip", note="аккаунт зарезервирован для Автопилота")
                    continue
                it["note"] = await press_one(acc, chk["channel"], chk["message_id"])
                it["state"] = "ok"
            except FloodWaitError as exc:
                it["state"], it["note"] = "err", f"FloodWait {exc.seconds} сек."
            except Exception as exc:
                it["state"], it["note"] = "err", f"{type(exc).__name__}: {exc}"
            print(f"[BUTTONS] {it['account']}: {it['state']} — {it['note']}", flush=True)
        job["status"] = "stopped" if job["stop"] else "done"
    except Exception as exc:
        job["status"], job["error"] = "stopped", f"{type(exc).__name__}: {exc}"


def buttons_job_view():
    if not BTN_JOB:
        return None
    return {k: v for k, v in BTN_JOB.items() if k != "stop"}


async def api_buttons_click(request):
    global BTN_TASK
    data = await request.json()
    chk = BTN_CHECKS.get(str(data.get("check_id", "")))
    if not chk:
        fail("Сначала нажми «Проверить»")
    if BTN_JOB.get("status") == "running":
        fail("Задача уже выполняется")
    pool = [n for n in chk["accounts"] if n not in chk["used"] and general_account(n)]
    try:
        count = int(data.get("count"))
        dmin, dmax = float(data.get("delay_from") or 0), float(data.get("delay_to") or 0)
    except (TypeError, ValueError):
        fail("Проверь числа: количество и задержка")
    if not pool:
        fail("Все подходящие аккаунты уже использованы — нажми «Проверить» заново")
    if not 1 <= count <= len(pool):
        fail(f"Количество аккаунтов: от 1 до {len(pool)}")
    dmin = max(0.0, dmin)
    dmax = max(dmin, dmax)
    chosen = random.sample(pool, count)
    chk["used"].update(chosen)
    mode = "join" if chk["message_id"] is None else "click"
    BTN_JOB.clear()
    BTN_JOB.update(status="running", stop=False, mode=mode, title=f"t.me/{chk['channel']}"
                   + (f"/{chk['message_id']}" if chk["message_id"] else ""), error="",
                   items=[{"account": n, "state": "queue", "note": ""} for n in chosen])
    BTN_TASK = asyncio.create_task(run_buttons_job(BTN_JOB, chk, dmin, dmax))
    return web.json_response({"job": buttons_job_view(), "available": len(pool) - count})


async def api_buttons_job(request):
    return web.json_response({"job": buttons_job_view()})


async def api_buttons_stop(request):
    if BTN_JOB.get("status") == "running":
        BTN_JOB["stop"] = True
    return web.json_response({"ok": True})


# ---------- загрузка аккаунтов по ссылкам ----------
UPLOAD_JOB: dict = {}
UPLOAD_TASK = None
URL_RE = re.compile(r"https?://\S+", re.I)


def upload_provider(url):
    """Определяет сервис по адресу ссылки."""
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    if host == "mega.nz" or host.endswith(".mega.nz") or host == "mega.co.nz":
        return "mega"
    if host == "drive.google.com" or host.endswith(".google.com") or host == "docs.google.com":
        return "gdrive"
    return "other"


def download_one(url, dest_dir):
    """Качает файл Google Drive или публичный файл MEGA в dest_dir.
    Блокирующая функция — вызывается через asyncio.to_thread."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    provider = upload_provider(url)
    if provider == "mega":
        # mega.py использует asyncio.coroutine, удалённый начиная с Python 3.11.
        # types.coroutine сохраняет нужную совместимость для generator-based coroutines.
        if not hasattr(asyncio, "coroutine"):
            import types as _types
            asyncio.coroutine = _types.coroutine
        try:
            from mega import Mega
        except ImportError as exc:
            raise ImportError("не установлен mega.py (pip install mega.py)") from exc
        # mega.py ожидает старый формат #!HANDLE!KEY. Преобразуем
        # современный /file/HANDLE#KEY в совместимый вид.
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if "/folder/" in parsed.path or parsed.fragment.startswith("F!"):
            raise ValueError("MEGA: ссылки на папки пока не поддерживаются — нужна публичная ссылка на файл")
        mega_url = url
        if "/file/" in parsed.path:
            file_id = parsed.path.split("/file/", 1)[1].split("/", 1)[0]
            key = parsed.fragment
            if not file_id or not key:
                raise ValueError("MEGA: в ссылке отсутствует ключ доступа после #")
            mega_url = f"{parsed.scheme or 'https'}://{parsed.netloc}/#!{file_id}!{key}"
        # Ключ не выводим в логи.
        safe_url = mega_url.split("#", 1)[0] + ("#***" if "#" in mega_url else "")
        print(f"[UPLOAD] Скачиваю MEGA: {safe_url}", flush=True)
        result = Mega().download_url(mega_url, dest_path=str(dest_dir))
        if result and isinstance(result, (str, os.PathLike)):
            result_path = Path(result)
            if result_path.is_file() and result_path.stat().st_size > 0:
                return [p for p in dest_dir.rglob("*") if p.is_file() and p.stat().st_size > 0] or [result_path]
    elif provider == "gdrive":
        import gdown
        print(f"[UPLOAD] Скачиваю Google Drive: {url}", flush=True)
        if "/folders/" in url:
            import inspect
            kw = dict(url=url, output=str(dest_dir), quiet=True)
            # remaining_ok есть только в старых gdown (лимит 50 файлов), в новых его убрали
            if "remaining_ok" in inspect.signature(gdown.download_folder).parameters:
                kw["remaining_ok"] = True
            gdown.download_folder(**kw)
        else:
            try:
                result = gdown.download(url=url, output=str(dest_dir) + os.sep, quiet=True, fuzzy=True)
            except TypeError:      # старая версия gdown без fuzzy
                result = gdown.download(url=url, output=str(dest_dir) + os.sep, quiet=True)
            if not (result and os.path.exists(result) and os.path.getsize(result) > 0):
                raise ValueError("не удалось скачать файл — проверь ссылку и доступ")
    else:
        raise ValueError("сервис ссылки не поддерживается — используй Google Drive или публичную ссылку MEGA на файл")

    files = [p for p in dest_dir.rglob("*") if p.is_file() and p.stat().st_size > 0]
    if not files:
        raise ValueError("не удалось скачать файл — проверь ссылку, ключ доступа и права")
    print(f"[UPLOAD] Готово: файлов {len(files)}", flush=True)
    return files


def move_to_sorting(files, root):
    """Переносит скачанное в Sorting (сохраняя папки, чтобы имена .session/.json не менялись)."""
    for p in files:
        rel = p.relative_to(root)
        dst = SORTING_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while dst.exists():     # два архива с одинаковым именем не должны затереть друг друга
            dst = dst.with_name(f"{dst.stem}_{n}{dst.suffix}")
            n += 1
        shutil.move(str(p), str(dst))


async def connect_new_accounts():
    new = load_accounts()
    sem = asyncio.Semaphore(10)

    async def one(a):
        async with sem:
            return await connect_account(a)
    ok = sum(1 for r in await asyncio.gather(*(one(a) for a in new)) if r)
    return len(new), ok


async def run_upload_job(job):
    try:
        for it in job["items"]:
            if it["state"] != "queue":
                continue
            if job["stop"]:
                it["state"] = "skip"
                continue
            it["state"], it["note"] = "active", "скачиваю…"
            tmp = TEMP_DIR / f"dl_{secrets.token_hex(4)}"
            try:
                files = await asyncio.to_thread(download_one, it["url"], tmp)
                it["note"] = "разбираю архив…"
                move_to_sorting(files, tmp)
                st = await asyncio.to_thread(sort_incoming)
                job["totals"]["json"] += st["json"]
                job["totals"]["session"] += st["session"]
                if st["errors"]:
                    raise ValueError("; ".join(st["errors"])[:300])
                if not (st["json"] or st["session"]):
                    note = "в файле не найдено ни .session, ни .json"
                    if st["dup"]:
                        note += f" (уже подключены: {st['dup']})"
                    raise ValueError(note)
                it["note"] = "подключаю аккаунты…"
                found, online = await connect_new_accounts()
                job["totals"]["new"] += found
                job["totals"]["online"] += online
                note = f"session: {st['session']}, json: {st['json']}, подключено: {online} из {found}"
                if st["dup"]:
                    note += f", уже были: {st['dup']}"
                if st["no_session"]:
                    note += f", без session: {len(st['no_session'])}"
                it["state"], it["note"] = "ok", note
            except ImportError as exc:
                it["state"], it["note"] = "err", str(exc)
            except Exception as exc:
                it["state"], it["note"] = "err", str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            log_url = it["url"].split("#", 1)[0] + ("#***" if "#" in it["url"] else "")
            print(f"[UPLOAD] {log_url}: {it['state']} — {it['note']}", flush=True)
        job["status"] = "stopped" if job["stop"] else "done"
    except Exception as exc:
        job["status"], job["error"] = "stopped", f"{type(exc).__name__}: {exc}"


def upload_job_view():
    if not UPLOAD_JOB:
        return None
    return {k: v for k, v in UPLOAD_JOB.items() if k != "stop"}


async def api_upload_start(request):
    global UPLOAD_TASK
    data = await request.json()
    if UPLOAD_JOB.get("status") == "running":
        fail("Загрузка уже выполняется")
    items, seen = [], set()
    for line in str(data.get("links", "")).splitlines():
        line = line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        if URL_RE.fullmatch(line):
            items.append({"url": line, "state": "queue", "note": ""})
        else:
            items.append({"url": line, "state": "err", "note": "это не ссылка (нужна http/https)"})
    queued = [i["url"] for i in items if i["state"] == "queue"]
    if any(upload_provider(u) == "gdrive" for u in queued) and importlib.util.find_spec("gdown") is None:
        fail("Для Google Drive не установлен gdown: выполни  python -m pip install gdown")
    if any(upload_provider(u) == "mega" for u in queued) and importlib.util.find_spec("mega") is None:
        fail("Для MEGA не установлен mega.py: выполни  python -m pip install mega.py")
    if not queued:
        fail("Вставь хотя бы одну ссылку (одна ссылка на строку)")
    UPLOAD_JOB.clear()
    UPLOAD_JOB.update(status="running", stop=False, error="", items=items,
                      totals={"json": 0, "session": 0, "new": 0, "online": 0})
    UPLOAD_TASK = asyncio.create_task(run_upload_job(UPLOAD_JOB))
    return web.json_response({"job": upload_job_view()})


async def api_upload_job(request):
    return web.json_response({"job": upload_job_view()})


async def api_upload_stop(request):
    if UPLOAD_JOB.get("status") == "running":
        UPLOAD_JOB["stop"] = True
    return web.json_response({"ok": True})


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def load_password():
    if PASSWORD_FILE.is_file():
        p = PASSWORD_FILE.read_text(encoding="utf-8").strip()
        if p:
            return p
    p = secrets.token_urlsafe(9)
    PASSWORD_FILE.write_text(p, encoding="utf-8")
    return p


async def main():
    global PANEL_PASSWORD, SESSION_ADMIN, ACCOUNT_FLAGS, PANEL_PREFERENCES
    print("[START] Скрипт запущен.", flush=True)
    TEMPLATE_AVATARS.mkdir(exist_ok=True)
    preferences = PANEL_PREFERENCES = panel_settings.PanelSettings(BASE_DIR)
    ACCOUNT_FLAGS = account_flags.AccountFlags(BASE_DIR)
    sort_incoming()
    load_accounts()
    if not ACCOUNTS:
        print("[!] Аккаунтов пока нет — добавь их во вкладке «Загрузка» или положи файлы в папку Sorting.", flush=True)
    else:
        print(f"[1] Загружено аккаунтов: {len(ACCOUNTS)}. Сначала запускаю веб-панель, затем подключаю Telegram.", flush=True)

    PANEL_PASSWORD = load_password()
    PASSWORD_FILE.chmod(0o600)
    app = web.Application(middlewares=[auth_mw, error_mw], client_max_size=15 * 1024 * 1024)
    deploy = account_deploy.AccountDeploy(BASE_DIR, lambda: ACCOUNTS, connect_new_accounts)
    deploy.register(app)
    SECURITY.register(app)

    async def retire_session(source):
        # Called only after the independent replacement is saved and connected.
        destination = BASE_DIR/'security'/'retired'/source.name
        destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        await asyncio.wait_for(source.client.disconnect(), 10)
        source.client.session.close()
        for folder, suffix in ((ACCOUNT_DIR, '.json'), (SESSION_DIR, '.session'), (SESSION_DIR, '.session-journal')):
            path = folder/(source.name+suffix)
            if path.exists():
                shutil.move(str(path), destination/(path.name+'.'+secrets.token_hex(4)))
        ACCOUNTS.pop(source.name, None)
        ACCOUNT_FLAGS.forget(source.name)
        for key in [k for k, value in INDEX.items() if value is source]:
            INDEX.pop(key, None)
        invalidate_account_profile(source)

    sessions = session_admin.SessionAdmin(BASE_DIR, lambda: ACCOUNTS, SECURITY, connect_new_accounts, retire_session)
    SESSION_ADMIN = sessions
    sessions.register(app)
    deploy.on_created = lambda name: sessions.mark_owned(ACCOUNTS[name]) if name in ACCOUNTS else None
    copies = profile_copy.ProfileCopies(BASE_DIR / 'profile_copies', lambda: ACCOUNTS, prepare_avatar,
                                        lambda acc, data: upload_avatar(acc, data, prepared=True),
                                        copy_profile_fields, clear_photos=clear_old_avatars)
    copies.register(app, get_acc)
    subscription_panel = subscriptions.Subscriptions(BASE_DIR, lambda: ACCOUNTS, preferences.get,
                                                       eligible=lambda acc: general_account(acc.name))

    async def update_preferences(request):
        old = preferences.get()
        response = await preferences.update(request)
        new = preferences.get()
        if any(old[key] != new[key] for key in ('subscription_hours', 'subscription_spread')):
            subscription_panel.reschedule()
        return response

    app.router.add_get('/api/panel-settings', preferences.read)
    app.router.add_post('/api/panel-settings', update_preferences)
    subscription_panel.register(app)
    vpn.VpnSubscriptions(BASE_DIR, lambda: ACCOUNTS, eligible=lambda acc: general_account(acc.name)).register(app)
    scenarios.Scenarios(BASE_DIR, lambda: ACCOUNTS, account_flag=flag_for).register(app)

    async def background(app):
        async def expire_logins():
            while True:
                await deploy.cleanup()
                await sessions.cleanup()
                await asyncio.sleep(30)
        tasks = [asyncio.create_task(SECURITY.run(), name="account-security"),
                 asyncio.create_task(expire_logins(), name="login-cleanup"),
                 asyncio.create_task(copies.run(), name="profile-copies")]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await sessions.close()
        await deploy.shutdown()

    app.cleanup_ctx.append(background)
    chat = r"/api/accounts/{acc}/chats/{chat_id:-?\d+}"
    app.add_routes([
        web.post("/api/accounts/{acc}/apply-template", api_apply_template),
        web.get("/", index),
        web.get("/api/accounts", api_accounts),
        web.delete("/api/accounts/{acc}", api_delete_account),
        web.post("/api/accounts/{acc}/flag", api_account_flag),
        web.get("/api/accounts/{acc}/avatar", api_my_avatar),
        web.get("/api/accounts/{acc}/profile-photos", api_account_photos),
        web.get("/api/accounts/{acc}/music", api_account_music),
        web.post("/api/accounts/{acc}/music", api_account_music_add),
        web.get(r"/api/accounts/{acc}/profile-photos/{pid:\d+}/image", api_account_photo_image),
        web.delete(r"/api/accounts/{acc}/profile-photos/{pid:\d+}", api_account_photo_delete),
        web.post("/api/accounts/{acc}/profile", api_profile),
        web.post("/api/accounts/{acc}/spam-status", api_spam_status),
        web.get("/api/accounts/{acc}/full", api_full),
        web.post("/api/accounts/{acc}/open", api_open),
        web.post("/api/accounts/{acc}/last-seen", api_last_seen),
        web.post("/api/accounts/{acc}/avatar", api_set_avatar),
        web.post("/api/template/load", api_template_load),
        web.get("/api/template/export", api_template_export),
        web.get(r"/api/template/avatar/{uid:\d+}", api_template_avatar),
        web.get(r"/api/template/photo/{uid:\d+}/{num:\d+}", api_template_photo),
        web.delete(r"/api/template/photo/{uid:\d+}/{num:\d+}", api_template_photo_delete),
        web.post(r"/api/template/photo/{uid:\d+}", api_template_photo_add),
        web.get("/api/template/roster", api_template_roster_get),
        web.post("/api/template/roster", api_template_roster_save),
        web.post("/api/template/check", api_template_roster_check),
        web.post("/api/template/exclude", api_template_exclude),
        web.get(chat + "/avatar", api_chat_avatar),
        web.get(chat + r"/media/{msg_id:\d+}", api_chat_media),
        web.get(chat + "/messages", api_history),
        web.post(chat + "/send", api_send),
        web.post(chat + "/read", api_read),
        web.post(chat + "/block", api_block),
        web.post(chat + "/click", api_click),
        web.post(chat + "/webview", api_webview),
        web.get("/sound.mp3", sound),
        web.post("/api/accounts/{acc}/miniapp", api_miniapp),
        web.get("/api/inbox", api_inbox),
        web.post("/api/inbox/load-dialogs", api_inbox_load),
        web.get("/api/events", api_events),
        web.post("/api/buttons/check", api_buttons_check),
        web.post("/api/buttons/click", api_buttons_click),
        web.get("/api/buttons/job", api_buttons_job),
        web.post("/api/buttons/stop", api_buttons_stop),
        web.post("/api/accounts/{acc}/resolve", api_resolve),
        web.post("/api/upload/start", api_upload_start),
        web.get("/api/upload/job", api_upload_job),
        web.post("/api/upload/stop", api_upload_stop),
    ])
    general_clients = lambda: service_clients(general_only=True)
    channels.set_usernames(lambda: {n: (a.me.username or "") for n, a in ACCOUNTS.items() if a.me is not None})
    automatic_multi.register(app, service_clients, BASE_DIR,
                       account_ids=lambda: {n: a.me.id for n, a in ACCOUNTS.items()
                                            if a.me is not None}, preferences=preferences.get)
    app[channels.ORDER_JOBS_DIRECTORY] = BASE_DIR / "order_jobs"
    app[channels.ORDER_ACCOUNT_CONNECTING] = (
        lambda name: name in ACCOUNTS and ACCOUNTS[name].error_kind not in ("session", "other"))
    app[channels.ORDER_ACCOUNT_IDS] = lambda: {n: a.me.id for n, a in ACCOUNTS.items() if a.me is not None}
    app[channels.ORDER_ACCOUNT_ALLOWED] = general_account
    channels.register(app, general_clients)
    multi.register_all(app, general_clients)
    unsub.register_all(app, general_clients)
    cs3bet.register_all(app, general_clients, account_ids=lambda: {n: a.me.id for n, a in ACCOUNTS.items() if a.me is not None})
    presence.keeper.configure(general_clients, BASE_DIR / "presence.json")
    presence.register(app)
    presence.keeper.start()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    print(f"\n[2] Веб-панель слушает {HOST}:{PORT}", flush=True)
    print("    Открывай её по адресу и порту, назначенным в разделе Network/Allocations Pterodactyl.", flush=True)
    print(f"    Логин: {PANEL_USER}. Пароль хранится в {PASSWORD_FILE.name}", flush=True)
    # Start Telegram connections only AFTER the HTTP socket is listening.
    # A slow/unreachable Telegram data centre cannot make the panel unavailable.
    connect_task = asyncio.create_task(connect_initial_accounts(), name="telegram-initial-connect") if ACCOUNTS else None
    retry_task = asyncio.create_task(reconnect_failed_accounts(), name="telegram-auto-reconnect")
    if connect_task:
        print("[TELEGRAM] Подключения запущены в фоне (не более 4 одновременно).", flush=True)
    print("[TELEGRAM] Автоповтор временных сетевых ошибок включён; ошибки сессий показываются без повторов.", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        retry_task.cancel()
        await asyncio.gather(retry_task, return_exceptions=True)
        if connect_task:
            connect_task.cancel()
            await asyncio.gather(connect_task, return_exceptions=True)
        await runner.cleanup()
        await presence.keeper.stop()   # все, кто сейчас в сети по расписанию, выходят из сети
        await asyncio.gather(*(a.client.disconnect() for a in ACCOUNTS.values()), return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOP] Программа остановлена.", flush=True)
    except Exception as exc:
        print(f"\n[КРИТИЧЕСКАЯ ОШИБКА] {type(exc).__name__}: {exc}", flush=True)
