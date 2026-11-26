"""Local dashboard aggregates. Collecting them performs no Telegram requests."""
import asyncio
import logging
import math
import time
from collections import Counter
from pathlib import Path

from aiohttp import web

from template_library import atomic_json_save, json_load

ORDER_SERVICES = web.AppKey("dashboard_order_services", dict)


class Dashboard:
    def __init__(self, directory, accounts, balances):
        self.path = Path(directory) / "security" / "dashboard.json"
        self.accounts = accounts
        self.balances = balances
        self.state = {"series": [], "completed": {}} | json_load(self.path)

    def archive_order(self, service, job):
        if self.count_order(service, job):
            try:
                atomic_json_save(self.path, self.state)
            except OSError:
                logging.exception("Could not archive the completed order in Overview")

    def count_order(self, service, job):
        done = sum(item['state'] == 'ok' for item in job['items'])
        if done:
            key = service + '/' + job['id']
            recorded = self.state['completed']
            recorded[key] = max(done, recorded.get(key, 0))
        return done

    def snapshot(self, app, *, save=False):
        accounts = self.accounts()
        frozen = {a.me.id if a.me else a.name for a in accounts.values()
                  if (getattr(a, 'freeze', None) or {}).get('frozen')}
        active = {a.me.id for a in accounts.values() if a.me and a.client.is_connected() and a.me.id not in frozen}
        balances = []
        for name, row in self.balances().items():
            if name not in accounts:
                continue
            try:
                amount = float(row.get('balance'))
            except (TypeError, ValueError):
                continue
            if math.isfinite(amount):
                balances.append(amount)
        point = {"at": int(time.time()), "active": len(active), "balance": sum(balances) if balances else None,
                 "largest": max(balances, default=None)}
        recorded = self.state["completed"]
        running = []
        for name, service in app.get(ORDER_SERVICES, {}).items():
            for job in service.jobs.values():
                done = self.count_order(name, job)
                if job['status'] == 'running':
                    running.append({"service": name, "title": job['title'], "done": done, "total": len(job['items'])})
        hub = app.get('automatic_hub')
        posts = []
        if hub:
            from automatic import parse_timestamp

            for monitor in hub.monitors.values():
                for post in monitor.state['posts']:
                    plans = post.get('execution') or {}
                    for name, plan in plans.items():
                        key = name + '/automatic/' + monitor.state['channel'] + '/' + str(post['post_id'])
                        done = sum(action['status'] == 'done' for action in plan['actions'])
                        if done:
                            recorded[key] = max(done, recorded.get(key, 0))
                    plan = plans.get('views', {})
                    if not plan.get('target'):
                        continue
                    actions = plan['actions']
                    due = [action['due'] for action in actions if action['status'] == 'pending']
                    now = time.time()
                    due_now = sum(due_time.timestamp() <= now for action in actions
                                  if action['status'] != 'duplicate'
                                  and (due_time := parse_timestamp(action.get('due'))) is not None)
                    posts.append({"channel": monitor.state['channel'], "id": post['post_id'], "date": post['post_date'],
                                  "target": plan['target'], "due": due_now, "next": min(due, default=None),
                                  "done": sum(action['status'] == 'done' for action in actions),
                                  "mode": (post.get('settings') or {}).get('view_mode', 'legacy')})
        totals = Counter()
        for key, done in recorded.items():
            totals[key.split('/', 1)[0]] += done
        series = self.state["series"]
        if save and (not series or point['at'] // 300 > series[-1]['at'] // 300):
            series.append(point)
            del series[:-4032]
        if save:
            atomic_json_save(self.path, self.state)
        return {"accounts": len(accounts), "active": len(active), "frozen": len(frozen),
                "completed": sum(totals.values()), "services": dict(totals), "series": series,
                "balance": point['balance'], "largest": point['largest'], "running": running,
                "posts": sorted(posts, key=lambda row: row['date'], reverse=True)[:6]}

    async def run(self, app):
        while True:
            try:
                self.snapshot(app, save=True)
            except OSError:
                logging.exception("Could not save the dashboard history")
            await asyncio.sleep(60)

    async def read(self, request):
        return web.json_response(self.snapshot(request.app), headers={"Cache-Control": "no-store"})

    def register(self, app):
        app.router.add_get('/api/overview', self.read)


OVERVIEW = web.AppKey("overview", Dashboard)
