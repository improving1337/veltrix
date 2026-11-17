"""Music in a Telegram user profile (account.saveMusic / getSavedMusicIds).

No Telegram session or credentials are stored here; all operations use an already
connected user client, never a bot or another user's account.
"""
import re
from urllib.parse import urlsplit


class MusicProfileError(ValueError):
    """A user-facing validation or compatibility error."""


def parse_music_post_url(raw):
    """Return (public_channel_username, message_id) for t.me/<name>/<number>."""
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 300:
        raise MusicProfileError('Укажи ссылку на музыкальный пост: https://t.me/канал/123')
    url = raw.strip()
    if not re.match(r'^https?://', url, flags=re.I):
        url = 'https://' + url
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        path = parsed.path.strip('/').split('/')
    except (ValueError, TypeError):
        raise MusicProfileError('Некорректная ссылка на Telegram-пост')
    if parsed.scheme not in ('http', 'https') or host not in ('t.me', 'www.t.me', 'telegram.me', 'www.telegram.me'):
        raise MusicProfileError('Принимаются только ссылки на сообщения t.me/канал/123')
    if len(path) == 3 and path[0] == 's':
        path = path[1:]
    if len(path) != 2 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', path[0]) or not re.fullmatch(r'[0-9]{1,12}', path[1]):
        raise MusicProfileError('Формат ссылки: https://t.me/имя_канала/номер_сообщения')
    message_id = int(path[1])
    if message_id <= 0:
        raise MusicProfileError('ID сообщения должен быть положительным числом')
    return path[0], message_id


def music_requests(functions):
    account_api = getattr(functions, 'account', None)
    save = getattr(account_api, 'SaveMusicRequest', None)
    get_ids = getattr(account_api, 'GetSavedMusicIdsRequest', None)
    if save is None or get_ids is None:
        raise MusicProfileError('Установленная версия Telethon не поддерживает музыку профиля. Обнови: python -m pip install -U "telethon>=1.41,<2"')
    return save, get_ids


async def get_profile_music_count(client, functions):
    """Fetch the actual number of saved profile songs, not a local approximation."""
    _, get_ids = music_requests(functions)
    result = await client(get_ids(hash=0))
    ids = getattr(result, 'ids', None)
    if ids is None:
        raise MusicProfileError('Telegram не вернул список песен профиля')
    return len(ids)


async def add_profile_music(client, functions, utils, link):
    """Save a document representing music from a public channel message."""
    save, _ = music_requests(functions)
    channel, post_id = parse_music_post_url(link)
    try:
        message = await client.get_messages(channel, ids=post_id)
    except ValueError as exc:
        raise MusicProfileError('Канал не найден или этот аккаунт не имеет доступа к публикации') from exc
    if message is None:
        raise MusicProfileError('Публикация не найдена или недоступна этому аккаунту')
    document = getattr(message, 'document', None)
    if document is None or not getattr(message, 'audio', None) or getattr(message, 'voice', None):
        raise MusicProfileError('В публикации нет музыкального аудиофайла (не голосового сообщения)')
    try:
        input_document = utils.get_input_document(document)
    except (ValueError, TypeError) as exc:
        raise MusicProfileError('Telegram не предоставил доступный аудиодокумент') from exc
    accepted = await client(save(id=input_document, unsave=False))
    if not accepted:
        raise MusicProfileError('Telegram не подтвердил добавление песни в профиль')
    return {'channel': channel, 'post_id': post_id}
