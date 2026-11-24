"""Run on the Gmail owner's computer to obtain credentials for Veltrix."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/gmail.send'


def authorize(client, open_browser=webbrowser.open):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    callback = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # The redirect query contains the authorization code.

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path != '/oauth2callback':
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            received = query.get('state', [''])[0]
            if not hmac.compare_digest(received.encode(), state.encode()):
                self.send_error(400, 'Invalid OAuth state')
                return
            callback['code'] = query.get('code', [''])[0]
            answer = ('Вход получен. Вернитесь к терминалу.' if callback['code']
                      else 'Google не выдал разрешение. Вернитесь к терминалу.')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(answer.encode())

    with HTTPServer(('127.0.0.1', 0), Handler) as listener:
        redirect = f'http://127.0.0.1:{listener.server_port}/oauth2callback'
        target = AUTH_URL + '?' + urlencode({
            'client_id': client['client_id'], 'redirect_uri': redirect, 'response_type': 'code',
            'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
        })
        listener.timeout = 1
        deadline = time.monotonic() + 300
        print('Войдите в Gmail и разрешите отправку писем.', flush=True)
        if not open_browser(target):
            print('Откройте эту ссылку в браузере на этом компьютере:', target, flush=True)
        while not callback and time.monotonic() < deadline:
            listener.handle_request()
    if not callback.get('code'):
        raise ValueError('Вход Google не завершён. Проверьте аудиторию приложения и список тестовых пользователей.')
    return exchange(client, callback['code'], redirect, verifier)


def exchange(client, code, redirect, verifier):
    form = urlencode({
        'client_id': client['client_id'], 'client_secret': client['client_secret'],
        'code': code, 'redirect_uri': redirect, 'code_verifier': verifier, 'grant_type': 'authorization_code',
    }).encode()
    request = Request(TOKEN_URL, data=form, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urlopen(request, timeout=20) as response:
            tokens = json.load(response)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise ValueError(f'Google отклонил обмен кода (HTTP {status}). Запустите вход заново.') from None
    except (URLError, OSError, ValueError):
        raise ValueError('Не удалось подключиться к OAuth Google по HTTPS.') from None
    refresh_token = tokens.get('refresh_token') if isinstance(tokens, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError('Google не вернул Refresh Token. Проверьте, что при входе вы дали согласие на отправку писем.')
    return refresh_token


def write_settings(path, client, token):
    fields = {
        'GMAIL_TRANSPORT': 'api', 'GMAIL_CLIENT_ID': client['client_id'],
        'GMAIL_CLIENT_SECRET': client['client_secret'], 'GMAIL_REFRESH_TOKEN': token,
    }
    if any(any(char in value for char in '\r\n') for value in fields.values()):
        raise ValueError('Google вернул недопустимое значение OAuth.')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        for key, value in fields.items():
            output.write(f'{key}={value}\n')


def main():
    parser = argparse.ArgumentParser(description='Получить Refresh Token Gmail для Veltrix.')
    parser.add_argument('client_json', type=Path, help='скачанный JSON OAuth-клиента Desktop app')
    parser.add_argument('--output', type=Path, default=Path('gmail-credentials.env'))
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError('Файл вывода уже существует. Укажите другое имя через --output.')
        data = json.loads(args.client_json.read_text(encoding='utf-8-sig'))
        client = data.get('installed') if isinstance(data, dict) else None
        if not isinstance(client, dict) or any(not isinstance(client.get(key), str) or not client[key]
                                               for key in ('client_id', 'client_secret')):
            raise ValueError('Нужен JSON для OAuth-клиента типа Desktop app.')
        write_settings(args.output, client, authorize(client))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f'Данные сохранены в {args.output}. Перенесите значения в настройки Gmail в Veltrix.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
