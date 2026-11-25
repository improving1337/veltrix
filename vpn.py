"""Issue and track VPN subscriptions through a configured Telegram bot."""
import asyncio
import hashlib
import json
import logging
import re
import secrets
import smtplib
import socket
import ssl
import time
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from telethon import functions, types, utils
from telethon.errors import BotResponseTimeoutError, FloodWaitError, RPCError, UserAlreadyParticipantError

from gmail_delivery import GmailSettings, send_gmail_api
from template_library import atomic_json_save

DAYS = 86400
LOG = logging.getLogger(__name__)
DEFAULTS = {
    'channel': 'https://t.me/antizhalostinet',
    'bot': '@antizhalostinet_bot',
    'host': 'zestful.bond',
    'steps': [
        {'label': 'Ваша подписка', 'position': 1, 'delay': 1},
        {'label': 'Получить пробный доступ', 'position': 2, 'delay': 0},
        {'label': 'Получить доступ', 'position': 0, 'delay': 0},
    ],
    'poll_delay': 5,
    'attempts': 3,
}
TG_NAME = re.compile(r'(?:https?://(?:t\.me|telegram\.me)/|@)?([A-Za-z][A-Za-z0-9_]{3,31})/?')
DOMAIN = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
LINK = re.compile(r'https://[^\s<>"\']+', re.I)


def validated_settings(data):
    if not isinstance(data, dict):
        raise ValueError('Нужен объект настроек')
    settings = DEFAULTS | data
    for key in ('bot', 'channel'):
        value = settings[key]
        if not isinstance(value, str) or (value.strip() or key == 'bot') and not TG_NAME.fullmatch(value.strip()):
            raise ValueError('Укажите публичную ссылку или @username для бота и канала')
        settings[key] = value.strip()
    host = settings['host']
    if not isinstance(host, str) or not DOMAIN.fullmatch(host.strip().lower()):
        raise ValueError('Укажите домен ссылки, например zestful.bond')
    settings['host'] = host.strip().lower()
    for key, low, high in [('poll_delay', 1, 30), ('attempts', 1, 10)]:
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ValueError(f'{key}: введите целое число от {low} до {high}')
    steps = settings['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
        raise ValueError('Добавьте от 1 до 12 шагов')
    checked = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError('Проверьте шаги сценария')
        label, position, delay = step.get('label', ''), step.get('position', 0), step.get('delay', 0)
        if not isinstance(label, str) or len(label) > 150 or type(position) is not int or not 0 <= position <= 100:
            raise ValueError('Укажите текст кнопки и её номер либо 0 для поиска по тексту')
        if not label.strip() and not position:
            raise ValueError('Для каждого шага нужен текст или номер кнопки')
        if type(delay) is not int or not 0 <= delay <= 30:
            raise ValueError('Задержка после кнопки должна быть от 0 до 30 секунд')
        checked.append({'label': label.strip(), 'position': position, 'delay': delay})
    return {key: settings[key] for key in DEFAULTS} | {'steps': checked}


def match_link(message, host):
    raw = getattr(message, 'raw_text', '') or ''
    entities = getattr(message, 'entities', ()) or ()
    candidates = [entity.url for entity in entities
                  if isinstance(entity, types.MessageEntityTextUrl)]
    candidates.extend(utils.get_inner_text(raw, [entity for entity in entities
                                                 if isinstance(entity, types.MessageEntityUrl)]))
    for row in getattr(message, 'buttons', None) or ():
        candidates.extend(button.url for button in row if getattr(button, 'url', None))
    candidates.extend(match.group().rstrip('.,;!?)\u00bb')
                      for match in LINK.finditer(raw))
    for link in candidates:
        try:
            url = urlsplit(link)
        except ValueError:
            continue
        if url.scheme == 'https' and url.netloc.lower() == host and url.path not in ('', '/'):
            return link
    return None


def revision(message):
    buttons = [[(button.text, getattr(button, 'url', None), repr(getattr(button, 'data', None)))
                for button in row] for row in (getattr(message, 'buttons', None) or [])]
    contents = (getattr(message, 'raw_text', ''), buttons,
                [entity.url for entity in getattr(message, 'entities', ()) or ()
                 if isinstance(entity, types.MessageEntityTextUrl)])
    return hashlib.sha256(repr(contents).encode()).hexdigest()


def newer(message, anchor):
    return not message.out and (message.id > anchor[0] or message.id == anchor[0] and revision(message) != anchor[1])


def find_button(message, step):
    buttons = [button for row in (getattr(message, 'buttons', None) or ()) for button in row]
    label = step['label'].casefold()
    position = step['position']
    if position and position <= len(buttons):
        button = buttons[position - 1]
        if not label or label in button.text.casefold():
            return button
    if label:
        return next((button for button in buttons if label in button.text.casefold()), None)
    return None


def recipient_address(value):
    if not isinstance(value, str) or len(value) > 254 or any(ch in value for ch in ('\r', '\n')):
        raise ValueError('Введите один адрес электронной почты')
    value = value.strip()
    try:
        address = Address(addr_spec=value)
        if not address.username or not address.domain:
            raise ValueError()
    except ValueError:
        raise ValueError('Введите один адрес электронной почты') from None
    return address.addr_spec


def gmail_message(sender, recipient, link):
    message = EmailMessage()
    message['From'] = recipient_address(sender)
    message['To'] = recipient
    message['Subject'] = 'Ссылка на подписку VPN'
    message.set_content(link)
    return message


def send_gmail(directory, recipient, link, settings=None):
    if settings is None:
        settings = GmailSettings(directory).effective()
    sender = settings['email']
    password = settings['app_password'].replace(' ', '')
    if not sender or not password:
        raise ValueError('Укажите адрес Gmail и пароль приложения в настройках Gmail.')
    port = settings['smtp_port']
    if port not in ('465', '587'):
        raise ValueError('Порт SMTP должен быть 465 или 587')
    message = gmail_message(sender, recipient, link)
    host = 'smtp.gmail.com'
    context = ssl.create_default_context()
    sending = accepted = False
    try:
        if port == '465':
            connection = smtplib.SMTP_SSL(host, 465, timeout=20, context=context)
        else:
            connection = smtplib.SMTP(host, 587, timeout=20)
        with connection as server:
            if port == '587':
                server.starttls(context=context)
            server.login(message['From'].addresses[0].addr_spec, password)
            sending = True
            server.send_message(message)
            accepted = True
    except socket.gaierror:
        raise ValueError('Хостинг не смог разрешить имя smtp.gmail.com. Проверьте DNS и доступ к Gmail SMTP.') from None
    except smtplib.SMTPAuthenticationError:
        raise ValueError('Gmail отклонил вход. Проверьте адрес и пароль приложения.') from None
    except smtplib.SMTPRecipientsRefused:
        raise ValueError('Gmail отклонил адрес получателя. Проверьте, как он введён.') from None
    except smtplib.SMTPNotSupportedError:
        raise ValueError('SMTP-сервер не предложил безопасный вход. Проверьте доступ к Gmail SMTP и режим TLS.') from None
    except ssl.SSLError:
        if accepted:
            return
        if sending:
            raise ValueError('Защищённое соединение оборвалось во время отправки. Проверьте папку «Отправленные».') from None
        raise ValueError(f'Не удалось установить защищённое соединение с {host}:{port}. '
                         'Проверьте TLS и сертификаты на хостинге.') from None
    except smtplib.SMTPResponseException as exc:
        if not accepted:
            raise ValueError(f'Gmail вернул ошибку SMTP {exc.smtp_code}.') from None
    except (OSError, smtplib.SMTPException):
        if accepted:
            return  # The server already accepted the message before QUIT failed.
        if sending:
            raise ValueError('Gmail оборвал соединение во время отправки. Проверьте папку «Отправленные».') from None
        raise ValueError(f'Нет соединения с {host}:{port}. Проверьте исходящий TCP-доступ на хостинге.') from None


class VpnSubscriptions:
    def __init__(self, directory, accounts, clock=time.time, sleep=asyncio.sleep, mail=None, *, eligible=None):
        self.directory = Path(directory)
        self.path = self.directory / 'security' / 'vpn.json'
        self.accounts = accounts
        self.eligible = eligible or (lambda account: True)
        self.clock = clock
        self.sleep = sleep
        self.gmail = GmailSettings(self.directory)
        self.mail = self.send_link if mail is None else mail
        self.issue_lock = asyncio.Lock()
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            data = {}
        self.settings = validated_settings(data.get('settings', {}))
        self.records = data.get('records', {})

    def send_link(self, directory, recipient, link):
        settings = self.gmail.effective()
        if settings['transport'] == 'api':
            if not settings['email']:
                raise ValueError('Укажите адрес Gmail, под которым получен Refresh Token.')
            send_gmail_api(settings, gmail_message(settings['email'], recipient, link))
        elif settings['transport'] == 'smtp':
            send_gmail(directory, recipient, link, settings)
        else:
            raise ValueError('Выберите Gmail API или SMTP в настройках.')

    def save(self, records, settings=None):
        settings = self.settings if settings is None else settings
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json_save(self.path, {'settings': settings, 'records': records})
        self.path.chmod(0o600)
        self.settings, self.records = settings, records

    def put(self, account_id, record):
        self.save(self.records | {account_id: record})

    def snapshot(self):
        now = self.clock()
        accounts = {str(account.me.id): account for account in self.accounts().values() if account.me is not None}
        records = []
        for account_id, record in self.records.items():
            if record.get('excluded'):
                continue
            issued = record.get('issued_at')
            remaining = max(0, issued + 31 * DAYS - now) if issued is not None else None
            status = 'pending' if remaining is None else 'expired' if remaining == 0 else 'ending' if remaining < 5 * DAYS else 'active'
            account = accounts.get(account_id)
            records.append({'id': account_id, 'username': account.me.username if account else record['username'],
                            'name': account.name if account else record['name'],
                            'status': status, 'issued_at': issued,
                            'remaining': int(remaining) if remaining is not None else None})
        return {'settings': self.settings, 'records': records, 'busy': self.issue_lock.locked()}

    async def join_channel(self, client, channel):
        if channel:
            try:
                await client(functions.channels.JoinChannelRequest(TG_NAME.fullmatch(channel)[1]))
            except UserAlreadyParticipantError:
                pass

    async def bot_messages(self, client, bot):
        messages = await client.get_messages(bot, limit=8)
        return sorted((message for message in messages if message and not message.out),
                      key=lambda message: message.id, reverse=True)

    async def wait_for_button(self, client, bot, anchor, step):
        for attempt in range(4):
            if attempt:
                await self.sleep(1)
            messages = await self.bot_messages(client, bot)
            if messages and newer(messages[0], anchor):
                message = messages[0]
                button = find_button(message, step)
                if button:
                    if not isinstance(button.button.type, (types.ButtonTypeDefault, types.InlineButtonTypeCallback)):
                        raise ValueError('Шаг сценария должен нажимать обычную кнопку Telegram')
                    return message, button
        target = step['label'] or str(step['position'])
        raise ValueError(f'Бот не показал кнопку «{target}» в новом сообщении')

    async def get_link(self, client, bot, profile):
        for _ in range(profile['attempts']):
            await self.sleep(profile['poll_delay'])
            for message in await self.bot_messages(client, bot):
                if newer(message, profile['anchor']) and (link := match_link(message, profile['host'])):
                    return link
        return None

    async def issue(self):
        if self.issue_lock.locked():
            raise ValueError('Получение ссылки уже выполняется')
        async with self.issue_lock:
            live = {str(account.me.id): account for account in self.accounts().values()
                    if account.me is not None and account.client.is_connected() and self.eligible(account)}
            for account_id, record in self.records.items():
                if account_id in live and not record.get('excluded') and record.get('issued_at') is None:
                    return await self.complete(account_id, live[account_id].client, record)
            target = next((account_id for account_id in live if account_id not in self.records), None)
            if target is None:
                raise ValueError('Нет подключённых аккаунтов, которые ещё не запрашивали подписку')
            account = live[target]
            client = account.client
            settings = self.settings
            await self.join_channel(client, settings['channel'])
            bot_name = TG_NAME.fullmatch(settings['bot'])[1]
            bot = await client.get_entity(bot_name)
            if not getattr(bot, 'bot', False):
                raise ValueError('Указанный Telegram-аккаунт должен быть ботом')
            sent = await client.send_message(bot, '/start')
            anchor = (sent.id, '')
            for index, step in enumerate(settings['steps']):
                message, button = await self.wait_for_button(client, bot, anchor, step)
                anchor = (message.id, revision(message))
                if index == len(settings['steps']) - 1:
                    record = {'name': account.name, 'username': account.me.username or '', 'bot': bot_name,
                              'anchor': anchor, 'host': settings['host'], 'poll_delay': settings['poll_delay'],
                              'attempts': settings['attempts'], 'requested_at': self.clock(),
                              'receipt': secrets.token_hex(16)}
                    self.put(target, record)
                try:
                    await button.click()
                except BotResponseTimeoutError:
                    pass
                if step['delay']:
                    await self.sleep(step['delay'])
            return await self.complete(target, client, record, bot)

    async def complete(self, account_id, client, record, bot=None):
        if bot is None:
            bot = await client.get_entity(record['bot'])
        link = await self.get_link(client, bot, record)
        if link is None:
            raise ValueError('Бот пока не прислал ссылку. «Получить ссылку» повторно проверит сообщения.')
        issued_at = self.clock()
        self.put(account_id, {**self.records[account_id], 'url': link, 'issued_at': issued_at})
        return {'url': link, 'receipt': record['receipt']}

    async def email(self, data):
        recipient = recipient_address(data.get('recipient'))
        receipt = data.get('receipt')
        record = next((record for record in self.records.values()
                       if receipt and record['receipt'] == receipt and record.get('issued_at') is not None
                       and not record.get('excluded')), None)
        if record is None:
            raise ValueError('Сначала получите ссылку')
        await asyncio.to_thread(self.mail, self.directory, recipient, record['url'])

    def edit(self, account_id, issued_at=None, exclude=False):
        record = self.records.get(account_id)
        if not record or record.get('excluded'):
            raise ValueError('Аккаунт не найден')
        if exclude:
            self.put(account_id, {**record, 'excluded': True})
            return
        if (record.get('issued_at') is None or type(issued_at) not in (int, float)
                or not 0 < issued_at <= self.clock()):
            raise ValueError('Укажите действительную дату получения подписки')
        self.put(account_id, {**record, 'issued_at': issued_at})

    def register(self, app):
        def response(payload, status=200):
            return web.json_response(payload, status=status, headers={'Cache-Control': 'no-store'})

        async def run(operation):
            try:
                return response(await operation())
            except ValueError as exc:
                return response({'error': str(exc)}, 400)
            except FloodWaitError as exc:
                return response({'error': f'Telegram просит подождать {exc.seconds} сек.'}, 429)
            except RPCError as exc:
                LOG.warning('VPN Telegram error: %s', type(exc).__name__)
                return response({'error': 'Telegram отклонил действие. Проверьте доступ аккаунта к каналу и боту.'}, 502)

        async def read(request):
            return response(self.snapshot())

        async def configure(request):
            async def update():
                data = await request.json()
                self.save(self.records, validated_settings(data))
                return self.snapshot()
            return await run(update)

        async def issue(request):
            return await run(self.issue)

        async def email(request):
            async def send():
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError('Укажите адрес электронной почты и ссылку')
                await self.email(data)
                return {'ok': True}
            return await run(send)

        async def read_gmail(request):
            return response(self.gmail.snapshot())

        async def configure_gmail(request):
            async def update():
                data = await request.json()
                try:
                    return self.gmail.update(data, recipient_address)
                except OSError:
                    raise ValueError('Не удалось сохранить настройки Gmail. Проверьте права на запись в папку security.') from None
            return await run(update)

        async def edit(request):
            async def update():
                exclude = request.method == 'DELETE'
                data = {} if exclude else await request.json()
                if not isinstance(data, dict):
                    raise ValueError('Укажите дату получения подписки')
                self.edit(request.match_info['id'], data.get('issued_at'), exclude)
                return self.snapshot()
            return await run(update)

        app.add_routes([
            web.get('/api/vpn', read), web.post('/api/vpn/settings', configure),
            web.post('/api/vpn/issue', issue), web.post('/api/vpn/email', email),
            web.get('/api/vpn/gmail-settings', read_gmail), web.post('/api/vpn/gmail-settings', configure_gmail),
            web.patch(r'/api/vpn/records/{id:\d+}', edit), web.delete(r'/api/vpn/records/{id:\d+}', edit),
        ])
