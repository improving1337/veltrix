"""Persist shared panel preferences."""
from pathlib import Path

from aiohttp import web

from template_library import atomic_json_save, json_load

TABS = {'accounts', 'examples', 'upload', 'twofa', 'messages', 'orders', 'miniapp', 'cs3bet', 'automatic', 'vpn', 'scenarios', 'subscriptions'}
DEFAULTS = {'hidden_tabs': [], 'view_spread': 10, 'reaction_spread': 20, 'search_workers': 5,
            'profile_group': 'https://t.me/minecraft_beseda764',
            'subscription_hours': 72, 'subscription_spread': 0, 'autopilot_for_new_accounts': False}


class PanelSettings:
    def __init__(self, directory):
        self.path = Path(directory) / 'panel_settings.json'
        self.values = DEFAULTS | json_load(self.path)

    def get(self):
        return self.values.copy()

    async def read(self, request):
        return web.json_response(self.get())

    async def update(self, request):
        payload = await request.json()
        if not isinstance(payload, dict):
            return web.json_response({'error': 'Некорректные настройки'}, status=400)
        values = self.get()
        for key, low, high in [('view_spread', 0, 100), ('reaction_spread', 0, 100),
                               ('subscription_hours', 1, 8760), ('subscription_spread', 0, 100)]:
            value = payload.get(key, values[key])
            if type(value) is not int or not low <= value <= high:
                return web.json_response({'error': f'Значение должно быть от {low} до {high}'}, status=400)
            values[key] = value
        workers = payload.get('search_workers', values['search_workers'])
        if type(workers) is not int or workers < 1:
            return web.json_response({'error': 'Число аккаунтов для поиска должно быть целым и не меньше 1'},
                                     status=400)
        values['search_workers'] = workers
        autopilot = payload.get('autopilot_for_new_accounts', values['autopilot_for_new_accounts'])
        if type(autopilot) is not bool:
            return web.json_response({'error': 'Некорректная метка для новых аккаунтов'}, status=400)
        values['autopilot_for_new_accounts'] = autopilot
        tabs = payload.get('hidden_tabs', values['hidden_tabs'])
        group = payload.get('profile_group', values['profile_group'])
        if not isinstance(tabs, list) or any(not isinstance(tab, str) or tab not in TABS for tab in tabs):
            return web.json_response({'error': 'Некорректный список вкладок'}, status=400)
        if not isinstance(group, str) or len(group) > 300:
            return web.json_response({'error': 'Некорректная ссылка группы'}, status=400)
        values.update(hidden_tabs=sorted(set(tabs)), profile_group=group.strip() or DEFAULTS['profile_group'])
        atomic_json_save(self.path, values)
        self.values = values
        return web.json_response(self.get())
