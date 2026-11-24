"""Hot-reloadable Gmail delivery settings and OAuth transport."""
import base64
import json
import os
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from automatic import env_value

TOKEN_URL = 'https://oauth2.googleapis.com/token'
SEND_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
GMAIL_SEND_SCOPE = 'https://www.googleapis.com/auth/gmail.send'
FIELDS = {
    'transport': 'GMAIL_TRANSPORT', 'email': 'GMAIL_EMAIL', 'smtp_port': 'GMAIL_SMTP_PORT',
    'app_password': 'GMAIL_APP_PASSWORD', 'client_id': 'GMAIL_CLIENT_ID',
    'client_secret': 'GMAIL_CLIENT_SECRET', 'refresh_token': 'GMAIL_REFRESH_TOKEN',
}
PRIVATE_FIELDS = ('app_password', 'client_id', 'client_secret', 'refresh_token')


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


GOOGLE_HTTP = build_opener(NoRedirects())


class GmailSettings:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'security' / 'gmail.json'
        try:
            self.saved = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            self.saved = {}

    def effective(self):
        saved = self.saved
        settings = {key: saved[key] if key in saved else env_value(variable, self.directory)
                    for key, variable in FIELDS.items()}
        settings['transport'] = settings['transport'] or 'smtp'
        settings['smtp_port'] = settings['smtp_port'] or '465'
        return settings

    def snapshot(self):
        settings = self.effective()
        return {key: settings[key] for key in ('transport', 'email', 'smtp_port')} | {
            'configured': {key: bool(settings[key]) for key in PRIVATE_FIELDS}
        }

    def update(self, data, validate_email):
        if not isinstance(data, dict):
            raise ValueError('Проверьте настройки Gmail')
        saved = self.saved.copy()
        if 'transport' in data:
            if data['transport'] not in ('api', 'smtp'):
                raise ValueError('Выберите Gmail API или SMTP')
            saved['transport'] = data['transport']
        if 'smtp_port' in data:
            if data['smtp_port'] not in ('465', '587'):
                raise ValueError('Порт SMTP должен быть 465 или 587')
            saved['smtp_port'] = data['smtp_port']
        if 'email' in data:
            saved['email'] = validate_email(data['email'])
        for key in PRIVATE_FIELDS:
            if key not in data:
                continue
            value = data[key]
            if not isinstance(value, str) or len(value) > 4096 or any(ch in value for ch in '\r\n'):
                raise ValueError('Проверьте данные Gmail. Каждое значение должно быть на одной строке.')
            if value.strip():
                saved[key] = value.strip()

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.gmail-', dir=self.path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(saved, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)
        self.saved = saved
        return self.snapshot()


def post_google(url, body, headers, phase):
    request = Request(url, data=body, headers=headers, method='POST')
    try:
        with GOOGLE_HTTP.open(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            error = json.load(exc)
        except (ValueError, OSError):
            error = {}
        finally:
            exc.close()
        if phase == 'oauth' and isinstance(error, dict):
            if error.get('error') == 'invalid_grant':
                raise ValueError('Google отклонил Refresh Token. Пройдите вход Google заново.') from None
            if error.get('error') in ('invalid_client', 'unauthorized_client'):
                raise ValueError('Google отклонил OAuth-клиент. Проверьте Client ID и Client Secret.') from None
        if phase == 'send' and exc.code == 403:
            raise ValueError('Gmail API отказал в отправке (403). Проверьте, включён ли Gmail API и выдан ли доступ gmail.send.') from None
        raise ValueError(f'Google вернул HTTP {exc.code} при {"получении токена" if phase == "oauth" else "отправке письма"}.') from None
    except (URLError, OSError, ValueError):
        if phase == 'send':
            raise ValueError('Gmail API не подтвердил отправку. Проверьте HTTPS-доступ к gmail.googleapis.com и папку «Отправленные».') from None
        raise ValueError('Не удалось получить токен Google. Проверьте HTTPS-доступ к oauth2.googleapis.com.') from None


def send_gmail_api(settings, message):
    if not all(settings[key] for key in ('client_id', 'client_secret', 'refresh_token')):
        raise ValueError('Заполните Client ID, Client Secret и Refresh Token в настройках Gmail.')
    form = urlencode({
        'client_id': settings['client_id'], 'client_secret': settings['client_secret'],
        'refresh_token': settings['refresh_token'], 'grant_type': 'refresh_token',
    }).encode()
    oauth = post_google(TOKEN_URL, form, {'Content-Type': 'application/x-www-form-urlencoded'}, 'oauth')
    token = oauth.get('access_token') if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token or any(char.isspace() for char in token):
        raise ValueError('Google не вернул токен доступа Gmail.')
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode('ascii')
    result = post_google(SEND_URL, json.dumps({'raw': raw}).encode(), {
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
    }, 'send')
    if not isinstance(result, dict) or not result.get('id'):
        raise ValueError('Gmail API не подтвердил отправку. Проверьте папку «Отправленные».')
