"""Multi-channel coordinator for the existing per-channel Monitor.

Every public channel has its own persisted Monitor state and its own 24-hour
post windows. Channel registration is persisted separately and legacy single-
channel automatic_state.json is transparently reused on upgrade.
"""
import asyncio
import hashlib
import json
import logging
from pathlib import Path

from aiohttp import web

from automatic import MAX_PARALLEL_ACTIONS, MembershipRequests, Monitor, clean_link, utc_now

LOG = logging.getLogger(__name__)
MAX_CHANNELS = 100


class _JsonRequest:
    """Use the existing Monitor HTTP handlers without rewriting their logic."""
    def __init__(self, payload=None):
        self.payload = payload or {}

    async def json(self):
        return self.payload


def channel_key(link):
    return hashlib.sha256(link.lower().encode('utf-8')).hexdigest()[:16]


class Hub:
    def __init__(self, directory, clients, account_ids=None, preferences=None):
        self.directory = Path(directory)
        self.registry_path = self.directory / 'automatic_channels.json'
        self.clients = clients
        self.account_ids = account_ids
        self.preferences = preferences
        self.monitors = {}
        # Shared cross-channel limits, so adding channels does not multiply the
        # number of simultaneous Telegram actions / DeepSeek HTTP requests.
        self.action_semaphore = asyncio.Semaphore(MAX_PARALLEL_ACTIONS)
        self.analysis_semaphore = asyncio.Semaphore(2)
        self.membership_requests = MembershipRequests()
        self.order = []
        self.lock = asyncio.Lock()
        self.load()

    def load(self):
        entries = []
        if self.registry_path.exists():
            try:
                raw = json.loads(self.registry_path.read_text(encoding='utf-8'))
                entries = raw.get('channels', []) if isinstance(raw, dict) else []
            except (ValueError, OSError):
                LOG.exception('Cannot load automatic channels registry')
        else:
            # Upgrade from the one-channel system. Preserve every historical post
            # and action status in the original JSON, without resetting its cursor.
            old_path = self.directory / 'automatic_state.json'
            if old_path.exists():
                try:
                    old = json.loads(old_path.read_text(encoding='utf-8'))
                    if old.get('channel'):
                        entries = [old['channel']]
                except (ValueError, OSError):
                    LOG.exception('Cannot load legacy automatic state')
        for item in entries:
            link = item.get('channel') if isinstance(item, dict) else item
            try:
                self._add_local(clean_link(link))
            except web.HTTPBadRequest:
                LOG.warning('Skipping invalid automatic channel: %s', link)
        self.save()

    def storage_for(self, link):
        legacy = self.directory / 'automatic_state.json'
        if legacy.exists():
            try:
                old = json.loads(legacy.read_text(encoding='utf-8'))
                if str(old.get('channel', '')).lower() == link.lower():
                    return legacy
            except (OSError, ValueError):
                pass
        return self.directory / 'automatic_channels' / f'{channel_key(link)}.json'

    def _add_local(self, link):
        ident = channel_key(link)
        if ident not in self.monitors:
            mon = Monitor(self.storage_for(link), self.clients, self.account_ids,
                          env_directory=self.directory, preferences=self.preferences)
            mon.action_semaphore = self.action_semaphore
            mon.analysis_semaphore = self.analysis_semaphore
            mon.membership_requests = self.membership_requests
            # A stored monitor for this channel may have been stopped and removed
            # from registry; the same JSON is recovered if it is later re-added.
            self.monitors[ident] = mon
            self.order.append(ident)
        return ident

    def save(self):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        path = self.registry_path.with_suffix('.tmp')
        records = [{'id': ident, 'channel': self.monitors[ident].state['channel']}
                   for ident in self.order]
        path.write_text(json.dumps({'version': 1, 'channels': records}, ensure_ascii=False, indent=2), encoding='utf-8')
        path.replace(self.registry_path)

    def snapshot(self):
        items = []
        for ident in self.order:
            mon = self.monitors[ident]
            item = mon.snapshot()
            item['id'] = ident
            items.append(item)
        return {
            'server_now': utc_now().isoformat(), 'channels': items,
            'channel_count': len(items),
            'active_channels': sum(x['active'] for x in items),
            'connected_accounts': items[0]['connected_accounts'] if items else len(self.clients()),
            'active_posts_count': sum(x['active_posts_count'] for x in items),
        }

    def lookup(self, ident):
        mon = self.monitors.get(ident)
        if mon is None:
            raise web.HTTPNotFound(text=json.dumps({'error': 'Канал не найден'}, ensure_ascii=False), content_type='application/json')
        return mon

    async def get(self, request):
        return web.json_response(self.snapshot())

    async def add(self, request):
        payload = await request.json()
        link = clean_link(payload.get('channel'))
        async with self.lock:
            ident = channel_key(link)
            if ident not in self.monitors and len(self.order) >= MAX_CHANNELS:
                raise web.HTTPBadRequest(text=json.dumps({'error': f'Максимум {MAX_CHANNELS} каналов'}, ensure_ascii=False), content_type='application/json')
            # Reusing an existing channel is idempotent and doesn't reset history.
            new = ident not in self.monitors
            if new:
                self._add_local(link)
                self.monitors[ident].state['channel'] = link
                self.save()
            mon = self.monitors[ident]
            # Start monitoring immediately after a link is committed. Failure
            # leaves channel in list, with a retry available from UI.
            if not mon.state['active']:
                try:
                    await mon.start(_JsonRequest({'channel': link}))
                except Exception as exc:
                    mon.state['error'] = str(exc)[:500]
                    mon.state['active'] = False
                    mon.save()
        return web.json_response(self.snapshot())

    async def remove(self, request):
        ident = request.match_info['id']
        async with self.lock:
            mon = self.lookup(ident)
            await mon.stop(_JsonRequest())
            self.order.remove(ident)
            self.monitors.pop(ident)
            # Keep per-channel history file if user later re-adds this channel.
            self.save()
        return web.json_response(self.snapshot())

    async def channel_start(self, request):
        mon = self.lookup(request.match_info['id'])
        await mon.start(_JsonRequest({'channel': mon.state['channel']}))
        return web.json_response(self.snapshot())

    async def channel_stop(self, request):
        await self.lookup(request.match_info['id']).stop(_JsonRequest())
        return web.json_response(self.snapshot())

    async def channel_config(self, request):
        mon = self.lookup(request.match_info['id'])
        payload = await request.json()
        await mon.configure(_JsonRequest({
            'hours': payload.get('hours', mon.state['hours']),
            'view_activity': payload.get('view_activity', mon.state['view_activity']),
            'view_day2_pct': payload.get('view_day2_pct', mon.state['view_day2_pct']),
            'view_mode': payload.get('view_mode', mon.state['view_mode']),
            'max_views': payload.get('max_views', mon.state['max_views']),
            'max_reactions': payload.get('max_reactions', mon.state['max_reactions']),
            'execute_plan': payload.get('execute_plan', mon.state['execute_plan']),
            'views_enabled': payload.get('views_enabled', mon.state['views_enabled']),
            'subscribers_only': payload.get('subscribers_only', mon.state['subscribers_only']),
            'allowed_reactions': payload.get('allowed_reactions', mon.state['allowed_reactions']),
            'reaction_prompt': payload.get('reaction_prompt', mon.state['reaction_prompt']),
        }))
        return web.json_response(self.snapshot())

    async def preview_views(self, request):
        return await self.lookup(request.match_info['id']).preview_views(request)

    async def start_all(self, request):
        failures = {}
        for ident in list(self.order):
            mon = self.monitors[ident]
            if mon.state['active']:
                continue
            try:
                await mon.start(_JsonRequest({'channel': mon.state['channel']}))
            except Exception as exc:
                failures[ident] = str(exc)[:250]
                mon.state['error'] = failures[ident]
                mon.save()
        data = self.snapshot()
        data['start_errors'] = failures
        return web.json_response(data)

    async def stop_all(self, request):
        for ident in list(self.order):
            await self.monitors[ident].stop(_JsonRequest())
        return web.json_response(self.snapshot())

    async def startup(self, app):
        for mon in self.monitors.values():
            if mon.state['active'] and mon.state['channel']:
                mon.task = asyncio.create_task(mon.loop())
                for post in mon.state['posts']:
                    if (post.get('suggestion') or {}).get('status') == 'pending':
                        mon.request_analysis(post['post_id'])

    async def shutdown(self, app):
        await asyncio.gather(*(mon.close() for mon in self.monitors.values()))
        self.save()


def register(app, clients, directory, account_ids=None, preferences=None):
    hub = Hub(directory, clients, account_ids, preferences)
    app['automatic_hub'] = hub
    app.on_startup.append(hub.startup)
    app.on_cleanup.append(hub.shutdown)
    app.router.add_get('/api/automatic', hub.get)
    app.router.add_post('/api/automatic/channels', hub.add)
    app.router.add_delete('/api/automatic/channels/{id}', hub.remove)
    app.router.add_post('/api/automatic/channels/{id}/start', hub.channel_start)
    app.router.add_post('/api/automatic/channels/{id}/stop', hub.channel_stop)
    app.router.add_post('/api/automatic/channels/{id}/config', hub.channel_config)
    app.router.add_post('/api/automatic/channels/{id}/preview-views', hub.preview_views)
    app.router.add_post('/api/automatic/start-all', hub.start_all)
    app.router.add_post('/api/automatic/stop-all', hub.stop_all)
