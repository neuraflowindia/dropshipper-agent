"""Pacing for reads in the owner's signed-in research profile.

Each paced site has a minimum gap between reads and a ceiling on reads in any
24 hours (``config/research/signed_in_reads.json``); a site without rules is
never read. A read is counted when its browser is about to start
(``reserve_signed_in_read``) and uncounted only when the browser proved it
never navigated (``release_signed_in_read``), so a locked desktop or a busy
browser never uses up the day's reads. Only start times are kept, beside the
signed-in browser's own records (which also hold the desktop recheck).
"""

from datetime import timedelta
from pathlib import Path

from .models import SafetyError, timestamp, utc_now
from .signed_in_browser import SITES, records_lock, records_path
from .storage import atomic_json, load_json

SETTINGS = Path(__file__).resolve().parents[2] / "config/research/signed_in_reads.json"
WINDOW = timedelta(hours=24)
WAIT_CODES = ("SIGNED_IN_READ_SPACING", "SIGNED_IN_DAILY_CEILING")


def pacing_rules(site):
    """The site's ``min_interval_seconds`` and ``daily_ceiling`` (24 hours)."""
    value = load_json(SETTINGS, 16384)
    sites = value.get("sites") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != {"schema_version", "kind", "sites"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != "signed-in-read-pacing-v1" or not isinstance(sites, dict) or not set(sites) <= set(SITES)
            or any(not isinstance(rules, dict) or set(rules) != {"min_interval_seconds", "daily_ceiling"}
                   or type(rules["min_interval_seconds"]) is not int or not 1 <= rules["min_interval_seconds"] <= 86400
                   or type(rules["daily_ceiling"]) is not int or not 1 <= rules["daily_ceiling"] <= 10000
                   for rules in sites.values())):
        raise SafetyError("SIGNED_IN_PACING_SETTINGS_INVALID")
    if site not in sites:
        raise SafetyError("SIGNED_IN_PACING_RULES_MISSING")
    return sites[site]


def _path(root):
    return records_path(root, "read-pacing.json")


def _records(root):
    path = _path(root)
    if not path.exists() and not path.is_symlink():
        return {"schema_version": 1, "reads": {}}
    value = load_json(path, 1024 * 1024)
    reads = value.get("reads") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != {"schema_version", "reads"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(reads, dict) or not set(reads) <= set(SITES)
            or any(not isinstance(rows, list) or len(rows) > 10000 or any(not isinstance(at, str) for at in rows)
                   for rows in reads.values())):
        raise SafetyError("SIGNED_IN_PACING_RECORD_INVALID")
    try:
        for at in (at for rows in reads.values() for at in rows):
            timestamp(at)
    except SafetyError:
        raise SafetyError("SIGNED_IN_PACING_RECORD_INVALID") from None
    return value


def _hold(reason, until):
    return {"reason": reason, "next_retry_at": until.isoformat()}


def signed_in_pace(root, site, *, as_of=None):
    """None when ``site`` may start a signed-in read now, else its calm wait."""
    rules, value = pacing_rules(site), _records(root)
    now = timestamp(as_of or utc_now())
    reads = sorted(timestamp(at) for at in value["reads"].get(site, []) if timestamp(at) > now - WINDOW)
    if reads and now < reads[-1] + timedelta(seconds=rules["min_interval_seconds"]):
        return _hold("SIGNED_IN_READ_SPACING", reads[-1] + timedelta(seconds=rules["min_interval_seconds"]))
    if len(reads) >= rules["daily_ceiling"]:
        return _hold("SIGNED_IN_DAILY_CEILING", reads[len(reads) - rules["daily_ceiling"]] + WINDOW)
    return None


def reserve_signed_in_read(root, site, *, at=None):
    """Count one read just before its browser starts; refuse it while paced."""
    at = at or utc_now()
    with records_lock(root, "pacing"):
        hold = signed_in_pace(root, site, as_of=at)
        if hold:
            raise SafetyError(hold["reason"])
        value = _records(root)
        cutoff = timestamp(at) - WINDOW
        value["reads"][site] = [row for row in value["reads"].get(site, []) if timestamp(row) > cutoff] + [at]
        atomic_json(_path(root), value)
    return at


def release_signed_in_read(root, site, at):
    """Uncount a reserved read whose browser proved it never navigated."""
    with records_lock(root, "pacing"):
        value = _records(root)
        rows = value["reads"].get(site, [])
        if at in rows:
            del rows[len(rows) - 1 - rows[::-1].index(at)]
            atomic_json(_path(root), value)
