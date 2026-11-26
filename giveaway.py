"""Read explicit Telegram giveaway instructions and deadlines."""
import re
from datetime import datetime, timedelta, timezone

MOSCOW = timezone(timedelta(hours=3))
GIVEAWAY = re.compile(r"\b(?:розыгрыш\w*|giveaway|разыгра\w*|конкурс\w*)\b", re.I)
JOIN = re.compile(r"\b(?:подписат\w*|подпиш\w*|подписывай\w*|вступ\w*)\b", re.I)
PRESS = re.compile(r"\b(?:наж\w*|жм\w*|клик\w*)\b[^\n]{0,60}?(?:\n[ \t]*)?\bкноп\w*", re.I)
DEADLINE = re.compile(r"\b(?:итог\w*|заверш\w*|оконч\w*|результат\w*|до)\b", re.I)
DATE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2})(?!\d))?(?![\d./])(?:[ \t]*(?:в[ \t]*)?(\d{1,2}):(\d{2}))?")
RELATIVE = re.compile(r"\bчерез\s+(\d{1,3})\s*(день|дня|дней|сутки|суток|час\w*|минут\w*)\b", re.I)
CHANNEL = re.compile(r"(?<![\w./])(?:https?://)?(?:t\.me|telegram\.me)/(?:\+[-_\w]+|joinchat/[-_\w]+|[A-Za-z][A-Za-z0-9_]{3,})(?![\w/])|(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,}", re.I)


def relative_offset(match):
    value, units = match.groups()
    units = units.lower()
    unit = "hours" if units.startswith("час") else "minutes" if units.startswith("минут") else "days"
    return timedelta(**{unit: int(value)})


def giveaway_deadline(text, published):
    local = published.astimezone(MOSCOW)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        marker = DEADLINE.search(line)
        if not marker:
            continue
        clause = line[marker.end():]
        if index + 1 < len(lines) and not clause.strip(" :—-"):
            clause = lines[index + 1]
        if match := RELATIVE.search(clause):
            return local + relative_offset(match)
        if match := DATE.search(clause):
            day, month, year, hour, minute = match.groups()
            year = local.year if year is None else int(year) + (2000 if len(year) == 2 else 0)
            try:
                return datetime(year, int(month), int(day), int(hour or 0), int(minute or 0), tzinfo=MOSCOW)
            except ValueError:
                return None
    # A relative time can stand alone, e.g. “через 3 дня”.
    for line in lines:
        if match := RELATIVE.fullmatch(line.strip(" .!🎁🏆")):
            return local + relative_offset(match)
    return None


def select_button(buttons, heart):
    candidates = [button for button in buttons if not heart or "❤" in button.text]
    return candidates[0] if len(candidates) == 1 else None


def read_giveaway(text, published, buttons):
    text = text.translate({0x200b: None, 0x2060: None, 0xfeff: None})
    press = PRESS.search(text)
    if not (GIVEAWAY.search(text) and press):
        return None
    heart = "❤" in press.string[press.end():].split("\n", 1)[0]
    if select_button(buttons, heart) is None:
        return {"status":"skipped", "note":"Не удалось однозначно определить кнопку" + (" ❤️" if heart else "")}
    channels = []
    collecting = False
    for line in text.splitlines():
        if JOIN.search(line):
            collecting = True
        elif PRESS.search(line) or DEADLINE.search(line):
            collecting = False
        if collecting:
            links = CHANNEL.findall(line)
            channels.extend(link if link.startswith(("@", "http")) else "https://" + link for link in links)
    channels = list(dict.fromkeys(channels))
    if JOIN.search(text) and not channels:
        return {"status":"skipped", "note":"В условиях подписки нет ссылок на каналы"}
    deadline = giveaway_deadline(text, published)
    if deadline is None:
        return {"status":"skipped", "note":"Не удалось определить срок"}
    return {"status":"pending", "channels":channels, "deadline":deadline.isoformat(), "heart":heart}
