"""Управление локальной библиотекой разрешённых шаблонов профилей.

Никаких сетевых запросов и автоматической смены данных аккаунтов здесь нет.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

MAX_TEMPLATES = 1000
MAX_SAVED_PHOTOS = 5
MIN_SAVED_PHOTOS = 2


def atomic_json_save(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as out:
        json.dump(payload, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def json_load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {} if default is None else default


def photo_summary(actual_count, saved_count):
    """Данные о всех доступных фото и сохранённых (не более пяти)."""
    actual_count = max(0, int(actual_count or 0))
    saved_count = max(0, min(MAX_SAVED_PHOTOS, int(saved_count or 0)))
    return {
        'avatar_count': actual_count,
        'saved_avatar_count': saved_count,
        'eligible': actual_count >= MIN_SAVED_PHOTOS and saved_count >= MIN_SAVED_PHOTOS,
    }


def normalized_photo_dates(values, count):
    """Validate persisted timestamps without fabricating dates for old caches."""
    values = values if isinstance(values, list) else []
    result = []
    for raw in values[:count]:
        text = str(raw or '')[:40]
        try:
            dt = datetime.fromisoformat(text.replace('Z', '+00:00')) if text else None
            result.append(dt.isoformat() if dt else '')
        except ValueError:
            result.append('')
    return result + [''] * max(0, count - len(result))


def normalized_photo_sources(values, count):
    values = values if isinstance(values, list) else []
    result = [v if v in ('telegram', 'library') else 'unknown' for v in values[:count]]
    return result + ['unknown'] * max(0, count - len(result))


def photo_metadata(actual_count, saved_count, dates=None, sources=None):
    summary = photo_summary(actual_count, saved_count)
    count = summary['saved_avatar_count']
    times = normalized_photo_dates(dates, count)
    origins = normalized_photo_sources(sources, count)
    summary['photo_dates'] = times
    summary['photo_sources'] = origins
    # Upload time in the local library must NOT be presented as a Telegram install date.
    installed = [t for t, source in zip(times, origins) if t and source == 'telegram']
    summary['last_avatar_change'] = max(installed, default='')
    return summary


def photo_metadata_from_roster_item(item):
    return photo_metadata(item.get('avatar_count'), item.get('saved_avatar_count'),
                          item.get('photo_dates'), item.get('photo_sources'))

def sanitize_roster(data):
    """Сохраняет только ожидаемые поля, не доверяет путям и HTML из браузера."""
    if not isinstance(data, dict):
        raise ValueError('Нужен объект JSON')
    link = str(data.get('link', ''))[:256]
    try:
        limit = max(1, min(MAX_TEMPLATES, int(data.get('limit', 58))))
    except (TypeError, ValueError):
        limit = 58
    try:
        scan_offset = max(0, min(50_000_000, int(data.get('scan_offset', 0))))
    except (TypeError, ValueError):
        scan_offset = 0
    scan_done = bool(data.get('scan_done', False))
    items = data.get('items', [])
    if not isinstance(items, list):
        raise ValueError('items должен быть массивом')
    seen = set()
    clean = []
    allowed = ('id', 'first_name', 'last_name', 'name', 'username', 'username_new',
               'bio', 'birthday', 'has_avatar', 'is_bot', 'avatar_count', 'saved_avatar_count',
               'photo_dates', 'photo_sources', 'last_avatar_change')
    for item in items[:MAX_TEMPLATES]:
        if not isinstance(item, dict):
            continue
        try:
            uid = int(item['id'])
        except (KeyError, TypeError, ValueError):
            continue
        if uid <= 0 or uid in seen:
            continue
        seen.add(uid)
        obj = {key: item[key] for key in allowed if key in item}
        obj['id'] = uid
        for key in ('first_name', 'last_name', 'name', 'username', 'username_new', 'bio', 'birthday'):
            obj[key] = str(obj.get(key) or '')[:256 if key == 'bio' else 100]
        obj.update(photo_metadata_from_roster_item(obj))
        obj.pop('eligible', None)
        obj['has_avatar'] = obj['saved_avatar_count'] > 0
        obj['is_bot'] = bool(obj.get('is_bot', False))
        clean.append(obj)
    return {'version': 2, 'link': link, 'limit': limit, 'scan_offset': scan_offset,
            'scan_done': scan_done, 'items': clean[:limit]}


def write_photo_archive(avatar_root, roster, destination):
    """Export EXISTING local images for selected roster IDs; never fetch remotely.

    The ZIP contains <numeric_user_id>/01.jpg, <numeric_user_id>/02.jpg, ...
    Missing files are skipped; no arbitrary client paths are ever accepted.
    Returns (profiles_with_photos, image_count).
    """
    from zipfile import ZipFile, ZIP_DEFLATED

    avatar_root = Path(avatar_root)
    roster = sanitize_roster(roster)
    profile_count = 0
    image_count = 0
    with ZipFile(destination, 'w', compression=ZIP_DEFLATED, compresslevel=4) as archive:
        for item in roster['items']:
            uid = int(item['id'])
            if uid <= 0:
                continue
            folder = avatar_root / str(uid)
            saved = 0
            for num in range(1, min(MAX_SAVED_PHOTOS, item['saved_avatar_count']) + 1):
                image_path = folder / f'{num:02d}.jpg'
                if image_path.is_symlink() or not image_path.is_file():
                    continue
                archive.write(image_path, arcname=f'{uid}/{num:02d}.jpg')
                saved += 1
                image_count += 1
            if saved:
                profile_count += 1
    return profile_count, image_count
