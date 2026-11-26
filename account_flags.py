"""Persistent account roles shared by all sessions of a Telegram identity."""
from pathlib import Path

from account_security import private_json, read_json

GENERAL = "general"
AUTOPILOT = "autopilot"
PUBLIC = "public"
FLAGS = (GENERAL, AUTOPILOT, PUBLIC)


class AccountFlags:
    def __init__(self, directory):
        self.path = Path(directory) / "security" / "account_flags.json"
        self.state = {"accounts": {}, "identities": {}} | read_json(self.path)

    def save(self, **updates):
        state = self.state | updates
        private_json(self.path, state)
        self.state = state

    def register(self, names, default=GENERAL):
        accounts = self.state["accounts"]
        fresh = {name: {"flag": default} for name in names if name not in accounts}
        if fresh:
            self.save(accounts=accounts | fresh)

    def bind(self, name, telegram_id):
        identity = str(telegram_id)
        accounts, identities = self.state["accounts"], self.state["identities"]
        record = accounts.get(name, {"flag": GENERAL})
        if record.get("identity") == identity and identity in identities:
            return
        flag = record["flag"] if record.get("selected") else identities.get(identity, record["flag"])
        self.save(accounts=accounts | {name: {"flag": flag, "identity": identity}},
                  identities=identities | {identity: flag})

    def get(self, name):
        record = self.state["accounts"].get(name, {})
        return self.state["identities"].get(record.get("identity"), record.get("flag", GENERAL))

    def select(self, name, flag):
        if flag not in FLAGS:
            raise ValueError("Выбери метку «Общий», «Автопилот» или «Публичный»")
        accounts = self.state["accounts"]
        record = accounts.get(name, {})
        identity = record.get("identity")
        if identity is not None:
            self.save(identities=self.state["identities"] | {identity: flag})
        else:
            self.save(accounts=accounts | {name: {"flag": flag, "selected": True}})

    def forget(self, name):
        accounts = self.state["accounts"]
        if name in accounts:
            self.save(accounts={key: value for key, value in accounts.items() if key != name})
