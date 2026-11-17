"""Pure helpers for displaying real Telegram account photo history in the UI."""
from datetime import datetime


def serialize_photo(photo):
    """Use Telegram's actual upload timestamp; never infer from local file dates."""
    when = getattr(photo, 'date', None)
    return {'id': str(getattr(photo, 'id')),
            'date': when.isoformat() if isinstance(when, datetime) else ''}


def summary_from_photos(photos):
    if hasattr(photos, 'total'):
        total = max(0, int(photos.total))
    else:
        # A plain list fetched with limit can be truncated; a count is unknown.
        total = None
    first = next(iter(photos), None)
    stamp = getattr(first, 'date', None) if first is not None else None
    return {'avatar_count': total,
            'last_avatar_change': stamp.isoformat() if isinstance(stamp, datetime) else ''}
