"""Daily sharing hold, shared by both channels and stored in settings.json."""
from datetime import datetime, timedelta, timezone
import json
import re
import time


KST = timezone(timedelta(hours=9))


def default_schedule():
    return {'enabled': True, 'start': '00:00', 'end': '09:00'}


def validate_schedule(value):
    if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
        raise ValueError('공유 보류 사용 여부를 확인하세요.')
    for key in ('start', 'end'):
        if not isinstance(value.get(key), str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value[key]):
            raise ValueError('공유 보류 시각은 00:00~23:59 사이로 설정하세요.')
    if value['start'] == value['end']:
        raise ValueError('보류 시작과 공유 재개 시각을 다르게 설정하세요.')
    return {key: value[key] for key in ('enabled', 'start', 'end')}


def normalize_schedule(value):
    try:
        return validate_schedule(value)
    except ValueError:
        return default_schedule()


def load_schedule(directory):
    # Store.save_config replaces this file atomically. Read on every claim so
    # applying settings affects workers and other instances without a restart.
    try:
        value = json.loads((directory / 'settings.json').read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return default_schedule()
    return normalize_schedule(value.get('sharing_schedule') if isinstance(value, dict) else None)


def hold_until(schedule, now=None):
    """Return the end of the current hold, or zero. Start inclusive, end exclusive."""
    if not schedule['enabled']:
        return 0
    current = datetime.fromtimestamp(time.time() if now is None else now, KST)
    start, end = (int(schedule[key][:2]) * 60 + int(schedule[key][3:]) for key in ('start', 'end'))
    minute = current.hour * 60 + current.minute
    overnight = start > end
    held = (minute >= start or minute < end) if overnight else start <= minute < end
    if not held:
        return 0
    release = current.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if overnight and minute >= start:
        release += timedelta(days=1)
    return release.timestamp()


def schedule_status(schedule, now=None):
    release = hold_until(schedule, now)
    if release:
        return '공유 보류 중 · ' + datetime.fromtimestamp(release, KST).strftime('%m.%d %H:%M') + '부터 모아서 자동 공유'
    if schedule['enabled']:
        return f"공유 가능 · 매일 {schedule['start']}~{schedule['end']} 보류 (한국시간)"
    return '공유 보류 OFF · 시간 제한 없이 자동 공유'
