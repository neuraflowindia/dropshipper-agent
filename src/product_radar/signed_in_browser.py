"""The program's one lasting signed-in Chrome profile and the owner's sign-ins.

The profile lives in the owner's Windows account (resolved through PowerShell,
never hard-coded) and is never the owner's everyday profile. Chrome keeps the
sign-ins encrypted there; this module never stores, reads or prints passwords
or cookies. It keeps only small local records:

* ``sign-in-window.json``: whether the owner's plain sign-in window is open;
* ``site-states.json``: per site, whether the latest read saw it signed in.

Reads record what they saw with ``record_site_observation``. Routes ask
``signed_in_wait`` before planning a read; a wait is calm (no attempt is made).
Only one signed-in Chrome runs at a time (``browser_lock``), and the owner's
sign-in window counts as that Chrome: reads never close it.
"""

from contextlib import ExitStack
import base64
from datetime import timedelta
import json
from pathlib import Path, PureWindowsPath
import re
import subprocess
import time

from .models import SafetyError, timestamp, utc_now
from .runtime_storage import runtime_path
from .storage import atomic_json, file_lock, load_json, safe_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_FOLDER = ("NeuraFlowDropshipper", "research-browser-profile")
SITES = {
    "google": {"label": "Google account", "url": "https://accounts.google.com/", "required": True},
    "google-trends": {"label": "Google Trends", "url": "https://trends.google.com/trends/?geo=IN", "required": True},
    "indiamart": {"label": "IndiaMART", "url": "https://www.indiamart.com/", "required": True},
    "deodap": {"label": "DeoDap", "url": "https://deodap.in/", "required": False},
    "baapstore": {"label": "BaapStore", "url": "https://baapstore.com/", "required": False},
    "dropdash": {"label": "Dropdash", "url": "https://app.dropdash.co/", "required": True},
}
# Sites the owner dropped (Dropship India, 15 October 2026: it showed nothing
# useful, signed in or not). A record saved before then may still name one:
# that entry is ignored when read and gone at the next save.
RETIRED_SITES = frozenset(("dropship-india",))
STATES = ("signed_in", "sign_in_needed", "expired", "not_checked", "not_signed_in")
WAIT_CODES = ("OWNER_SIGN_IN_REQUIRED", "OWNER_SIGN_IN_WINDOW_OPEN", "OWNER_DESKTOP_UNAVAILABLE",
              "SIGNED_IN_BROWSER_BUSY")
WINDOW_RECHECK_SECONDS = 120
DESKTOP_RECHECK_SECONDS = 120
# Any other calm wait (sign-in needed, a busy or unstartable browser) is
# looked at again after this long; looking is a local record check only.
WAIT_RECHECK_SECONDS = 120
_PROFILE_CACHE = {}
_WINDOWS_PROFILE = re.compile(r"[A-Za-z]:\\[^\x00-\x1f\"*?<>|]{1,180}\\NeuraFlowDropshipper\\research-browser-profile")

# Chrome copies --user-data-dir to every child process (--type=renderer,
# gpu, crashpad...) but never the remote-control flag, so only browser (main)
# processes count. The owner's sign-in window is a browser process without the
# remote-control flag; a program Chrome left behind by an interrupted read is
# one with it, and is closed (its window's own close first, a forced end only
# if it stays, never past ``$by``) before the window check runs.
CLOSE_LEFTOVERS = r"""
function Get-ProfileBrowsers([string]$profile) {
  return @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'"|Where-Object {$_.CommandLine -and $_.CommandLine.IndexOf($profile,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and $_.CommandLine -notmatch '--type='})
}
function Get-SignInWindow([string]$profile) {
  return @(Get-ProfileBrowsers $profile|Where-Object {$_.CommandLine -notmatch '--remote-debugging-port'})
}
function Close-Leftovers([string]$profile,[DateTime]$by) {
  foreach($left in @(Get-ProfileBrowsers $profile|Where-Object {$_.CommandLine -match '--remote-debugging-port'})) {
    & taskkill.exe /PID $left.ProcessId /T | Out-Null
    $until=[DateTime]::UtcNow.AddSeconds(8); if($by -lt $until){$until=$by}
    while((Get-CimInstance Win32_Process -Filter ('ProcessId='+$left.ProcessId)) -and [DateTime]::UtcNow -lt $until){Start-Sleep -Milliseconds 200}
    $still=Get-CimInstance Win32_Process -Filter ('ProcessId='+$left.ProcessId)
    if($still -and $still.CommandLine -and $still.CommandLine.IndexOf($profile,[StringComparison]::OrdinalIgnoreCase) -ge 0){& taskkill.exe /PID $left.ProcessId /T /F | Out-Null}
  }
}
"""

_PROFILE_SCRIPT = r"""
$ErrorActionPreference='Stop'
$path=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'NeuraFlowDropshipper\research-browser-profile'
$null=New-Item -ItemType Directory -Force -Path $path
[Console]::Out.Write($path)
"""

_WINDOW_SCRIPT = r"""
$ErrorActionPreference='Stop'
$request=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
CLOSE_LEFTOVERS
if($request.close_leftovers){Close-Leftovers $request.profile ([DateTime]::UtcNow.AddSeconds(10))}
[Console]::Out.Write((@{window=((Get-SignInWindow $request.profile).Count -gt 0)}|ConvertTo-Json -Compress))
"""

# Plain Chrome without the remote-control flag: Google refuses sign-in in
# automation-controlled browsers. Sync stays off; the owner closes it.
_OPEN_SCRIPT = r"""
$ErrorActionPreference='Stop'
$request=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
CLOSE_LEFTOVERS
Close-Leftovers $request.profile ([DateTime]::UtcNow.AddSeconds(8))
if((Get-SignInWindow $request.profile).Count){[Console]::Out.Write((@{state='already_open'}|ConvertTo-Json -Compress));exit 0}
$flags=@('--no-first-run','--disable-sync','--no-default-browser-check',('--user-data-dir='+$request.profile),'--new-window')+@($request.urls)
$quoted=($flags|ForEach-Object {'"'+($_ -replace '"','\"')+'"'}) -join ' '
$null=Start-Process -FilePath $request.browser -ArgumentList $quoted -WindowStyle Normal
[Console]::Out.Write((@{state='opened'}|ConvertTo-Json -Compress))
"""


def _folder(root):
    return safe_path(runtime_path(root, "data/operations"), "signed-in-browser")


def _powershell(script, runner, *, timeout=20):
    from .native_public_browser import POWERSHELL, _command
    if not POWERSHELL.is_file():
        raise SafetyError("SIGNED_IN_BROWSER_NOT_INSTALLED")
    try:
        reply = runner(_command(script), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise SafetyError("SIGNED_IN_BROWSER_COMMAND_FAILED") from None
    output = (reply.stdout or "").strip()
    if reply.returncode != 0 or not output or len(output) > 4096:
        raise SafetyError("SIGNED_IN_BROWSER_COMMAND_FAILED")
    return output


def _scripted(script, payload):
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return script.replace("CLOSE_LEFTOVERS", CLOSE_LEFTOVERS).replace("PAYLOAD", encoded)


def research_profile(runner=subprocess.run):
    """Windows path of the lasting profile, created on first use and cached."""
    if runner not in _PROFILE_CACHE:
        path = _powershell(_PROFILE_SCRIPT, runner)
        if not _WINDOWS_PROFILE.fullmatch(path) or PureWindowsPath(path).parts[-2:] != PROFILE_FOLDER:
            raise SafetyError("SIGNED_IN_PROFILE_PATH_INVALID")
        _PROFILE_CACHE[runner] = path
    return _PROFILE_CACHE[runner]


def records_path(root, name):
    """A file beside the signed-in browser's own records (for its helpers)."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,60}", name):
        raise SafetyError("SIGNED_IN_RECORD_INVALID")
    return _folder(root) / name


def records_lock(root, name, *, wait=5):
    """A named lock (``<name>.lock``) beside the signed-in records."""
    return file_lock(records_path(root, name + ".lock"), wait, "SIGNED_IN_RECORD_BUSY")


def browser_lock(root=PROJECT_ROOT, *, wait=5):
    """One signed-in Chrome at a time: a read, or the launch of the sign-in window."""
    return file_lock(_folder(root) / "browser.lock", wait, "SIGNED_IN_BROWSER_BUSY")


def _load(root, name, empty):
    path = _folder(root) / name
    if not path.exists() and not path.is_symlink():
        return empty
    value = load_json(path, 65536)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SafetyError("SIGNED_IN_RECORD_INVALID")
    return value


def sign_in_window(root=PROJECT_ROOT):
    """The owner's sign-in window as last verified; nothing is probed here."""
    value = _load(root, "sign-in-window.json", {"schema_version": 1, "open": False, "checked_at": None,
                                                 "last_opened_at": None})
    if (set(value) != {"schema_version", "open", "checked_at", "last_opened_at"} or type(value["open"]) is not bool
            or any(value[field] is not None and not isinstance(value[field], str) for field in ("checked_at", "last_opened_at"))
            or value["open"] and value["checked_at"] is None):
        raise SafetyError("SIGNED_IN_RECORD_INVALID")
    for field in ("checked_at", "last_opened_at"):
        if value[field] is not None:
            timestamp(value[field])
    return value


def note_sign_in_window(root, is_open, *, opened=False, at=None):
    """Save a verified window state. Only the owner's own ``open`` passes
    ``opened`` (a new sign-in to re-check); probes never do."""
    at = at or utc_now()
    if not is_open and not opened and not sign_in_window(root)["open"]:
        return sign_in_window(root)  # nothing changed; nothing is written
    with file_lock(_folder(root) / "records.lock", 5, "SIGNED_IN_RECORD_BUSY"):
        value = sign_in_window(root)
        value.update(open=bool(is_open), checked_at=at)
        if opened:
            value["last_opened_at"] = at
        atomic_json(_folder(root) / "sign-in-window.json", value)
    return value


def without_retired_sites(value, field):
    """A saved per-site record with ``value[field]`` cleared of retired sites."""
    if isinstance(value, dict) and isinstance(value.get(field), dict):
        value[field] = {site: row for site, row in value[field].items() if site not in RETIRED_SITES}
    return value


def _site_records(root):
    value = without_retired_sites(_load(root, "site-states.json", {"schema_version": 1, "sites": {}}), "sites")
    sites = value.get("sites")
    if set(value) != {"schema_version", "sites"} or not isinstance(sites, dict) or not set(sites) <= set(SITES):
        raise SafetyError("SIGNED_IN_RECORD_INVALID")
    for row in sites.values():
        if (not isinstance(row, dict) or set(row) != {"signed_in", "observed_at", "since", "last_signed_in_at", "route"}
                or type(row["signed_in"]) is not bool or row["route"] is not None and not isinstance(row["route"], str)):
            raise SafetyError("SIGNED_IN_RECORD_INVALID")
        for field in ("observed_at", "since") + (("last_signed_in_at",) if row["last_signed_in_at"] is not None else ()):
            timestamp(row[field])
    return value


def record_site_observation(root, site, signed_in, *, route=None, observed_at=None):
    """A read saw ``site`` signed in (True) or behind its sign-in wall (False).

    Called by signed-in routes after each read that showed a clear marker;
    pages without a clear marker record nothing. No page content is kept here.
    """
    if (site not in SITES or type(signed_in) is not bool
            or route is not None and (not isinstance(route, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", route))):
        raise SafetyError("SIGNED_IN_OBSERVATION_INVALID")
    observed_at = observed_at or utc_now()
    timestamp(observed_at)
    with file_lock(_folder(root) / "records.lock", 5, "SIGNED_IN_RECORD_BUSY"):
        value = _site_records(root)
        previous = value["sites"].get(site)
        changed = previous is None or previous["signed_in"] != signed_in
        value["sites"][site] = {
            "signed_in": signed_in, "observed_at": observed_at, "route": route,
            "since": observed_at if changed else previous["since"],
            "last_signed_in_at": observed_at if signed_in else previous and previous["last_signed_in_at"]}
        atomic_json(_folder(root) / "site-states.json", value)
    return site_states(root)[site]


def site_states(root=PROJECT_ROOT):
    """Each site as signed in / sign-in needed / expired / not checked, from saved markers."""
    window, records = sign_in_window(root), _site_records(root)["sites"]
    opened = window["last_opened_at"]
    result = {}
    for site, facts in SITES.items():
        row = records.get(site)
        if row is None:
            state = "sign_in_needed" if facts["required"] and opened is None else "not_checked"
        elif row["signed_in"]:
            state = "signed_in"
        elif opened is not None and timestamp(opened) > timestamp(row["observed_at"]):
            state = "not_checked"  # the owner signed in again after this wall; the next read checks
        elif row["last_signed_in_at"]:
            state = "expired"
        else:
            # Optional sites (public feeds carry them) never need the owner.
            state = "sign_in_needed" if facts["required"] else "not_signed_in"
        result[site] = {"site": site, "label": facts["label"], "required": facts["required"], "state": state,
                        "observed_at": row and row["observed_at"], "since": row["since"] if row else None,
                        "route": row and row["route"]}
    return result


def signed_in_wait(root, site, *, as_of=None):
    """None when a signed-in read of ``site`` may start, else its calm wait code."""
    if site not in SITES:
        raise SafetyError("SIGNED_IN_OBSERVATION_INVALID")
    window = sign_in_window(root)
    as_of = as_of or utc_now()
    if desktop_unavailable(root, as_of=as_of):
        return "OWNER_DESKTOP_UNAVAILABLE"
    if window["open"] and (timestamp(as_of) - timestamp(window["checked_at"])).total_seconds() < WINDOW_RECHECK_SECONDS:
        return "OWNER_SIGN_IN_WINDOW_OPEN"
    if site_states(root)[site]["state"] in ("sign_in_needed", "expired"):
        return "OWNER_SIGN_IN_REQUIRED"
    from .signed_in_round import round_wait
    return round_wait(site)  # this programme round's share of the browser


def signed_in_recheck_at(root, reason, *, as_of=None):
    """When a calm signed-in wait is next worth checking: the end of the saved
    desktop or window recheck, else ``WAIT_RECHECK_SECONDS`` from now. Never
    earlier than now, so a waiting question always carries a retry time."""
    now = timestamp(as_of or utc_now())
    anchor, seconds = now, WAIT_RECHECK_SECONDS
    if reason == "OWNER_DESKTOP_UNAVAILABLE" and desktop_unavailable(root, as_of=now.isoformat()):
        anchor, seconds = timestamp(_load(root, "desktop.json", None)["unavailable_at"]), DESKTOP_RECHECK_SECONDS
    elif reason == "OWNER_SIGN_IN_WINDOW_OPEN" and sign_in_window(root)["open"]:
        anchor, seconds = timestamp(sign_in_window(root)["checked_at"]), WINDOW_RECHECK_SECONDS
    return max(anchor + timedelta(seconds=seconds), now).isoformat()


def note_desktop_unavailable(root, *, at=None):
    """A read found the owner's Windows session locked or signed out."""
    at = at or utc_now()
    timestamp(at)
    with file_lock(_folder(root) / "records.lock", 5, "SIGNED_IN_RECORD_BUSY"):
        atomic_json(_folder(root) / "desktop.json", {"schema_version": 1, "unavailable_at": at})


def desktop_unavailable(root=PROJECT_ROOT, *, as_of=None):
    """True for two minutes after a read found the desktop unavailable, so a
    locked computer overnight is one calm wait, not one probe per round."""
    value = _load(root, "desktop.json", None)
    if value is None:
        return False
    if set(value) != {"schema_version", "unavailable_at"} or not isinstance(value["unavailable_at"], str):
        raise SafetyError("SIGNED_IN_RECORD_INVALID")
    age = (timestamp(as_of or utc_now()) - timestamp(value["unavailable_at"])).total_seconds()
    return 0 <= age < DESKTOP_RECHECK_SECONDS


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_ACCOUNT_QUERY = re.compile(r"([?&#;](?:login_hint|authuser|email|Email|username)=)[^&#;\s\"'<>\\]*")
_FILE_ENDINGS = ("png", "jpg", "jpeg", "webp", "gif", "svg", "avif", "ico", "js", "css")
_ACCOUNT_LABEL = re.compile(r"(Google Account: )[^\"<>\n\\]*")
# The account's own picture (lh3...googleusercontent.com/a/..., /a-/..., /ogw/...)
# and Google's 21-digit account ids where they appear on their own.
_AVATAR = re.compile(r"(//lh[3-6]\.googleusercontent\.com/(?:a|a-|ogw)/)[A-Za-z0-9_=-]+")
_ACCOUNT_ID = re.compile(r"(?<![\w.%-])1\d{20}(?![\w])")


def redact_signed_in(value):
    """Remove the research account's identity from saved signed-in artefacts:
    e-mail addresses (also %40-encoded), account query values (login_hint,
    authuser, email ...), Google's account-label text, the account's picture
    address and its 21-digit account ids. JSON and HTML stay well formed; keys
    and every other value are unchanged."""
    if isinstance(value, str):
        value = _ACCOUNT_QUERY.sub(r"\1redacted", value)
        value = _ACCOUNT_ID.sub("[account id redacted]", _AVATAR.sub(r"\1redacted", value))
        # "logo@2x.png" is a file name, not an address.
        value = _EMAIL.sub(lambda found: found[0] if found[0].rsplit(".", 1)[1].lower() in _FILE_ENDINGS
                           else "[email redacted]", value)
        return _ACCOUNT_LABEL.sub(r"\1[account redacted]", value)
    if isinstance(value, dict):
        return {name: redact_signed_in(item) for name, item in value.items()}
    if isinstance(value, list):
        return [redact_signed_in(item) for item in value]
    return value


def sign_in_needs(root=PROJECT_ROOT):
    """Desktop view: one id per need (a new id means a new notification)."""
    return [{"id": row["site"] + ":" + row["state"] + ":" + (row["since"] or "never"), "site": row["site"],
             "label": row["label"], "state": row["state"]}
            for row in site_states(root).values() if row["state"] in ("sign_in_needed", "expired")]


def sign_in_line(root=PROJECT_ROOT):
    window, needs = sign_in_window(root), sign_in_needs(root)
    if window["open"]:
        return "The sign-in window is open. Close that Chrome window when you have finished signing in."
    needed = [row["label"] for row in needs if row["state"] == "sign_in_needed"]
    expired = [row["label"] for row in needs if row["state"] == "expired"]
    parts = (["Sign-in needed: " + ", ".join(needed) + "."] if needed else []) + (
        ["Sign-in expired: " + ", ".join(expired) + "."] if expired else [])
    if parts:
        return " ".join(parts) + " Click Sign in to research sites."
    if any(row["state"] == "not_checked" and row["required"] for row in site_states(root).values()):
        return "Sign-ins will be confirmed on the next research reads."
    return "Research sites are signed in."


def check_sign_in_window(root=PROJECT_ROOT, *, runner=subprocess.run):
    """Look at local Chrome processes only (no site visit) and save the result.

    Program Chrome left behind is closed only while this holds the browser
    lock; during a running read the window check alone runs (it never counts
    the read's Chrome as the owner's window)."""
    with ExitStack() as stack:
        try:
            stack.enter_context(browser_lock(root, wait=0))
            held = True
        except SafetyError as error:
            if str(error) != "SIGNED_IN_BROWSER_BUSY":
                raise
            held = False
        script = _scripted(_WINDOW_SCRIPT, {"profile": research_profile(runner), "close_leftovers": held})
        try:
            is_open = json.loads(_powershell(script, runner))["window"]
        except (ValueError, KeyError, TypeError):
            raise SafetyError("SIGNED_IN_BROWSER_COMMAND_FAILED") from None
    if type(is_open) is not bool:
        raise SafetyError("SIGNED_IN_BROWSER_COMMAND_FAILED")
    return note_sign_in_window(root, is_open)


def open_sign_in_window(root=PROJECT_ROOT, *, runner=subprocess.run, wait=5):
    """Open plain Chrome on the lasting profile with one tab per research site."""
    from .native_public_browser import CHROME, _windows_path
    if not CHROME.is_file():
        raise SafetyError("SIGNED_IN_BROWSER_NOT_INSTALLED")
    with browser_lock(root, wait=wait):
        payload = {"profile": research_profile(runner), "browser": _windows_path(CHROME, runner),
                   "urls": [facts["url"] for facts in SITES.values()]}
        try:
            state = json.loads(_powershell(_scripted(_OPEN_SCRIPT, payload), runner, timeout=15))["state"]
        except (ValueError, KeyError, TypeError):
            raise SafetyError("SIGNED_IN_BROWSER_COMMAND_FAILED") from None
        if state not in ("opened", "already_open"):
            raise SafetyError("SIGNED_IN_BROWSER_COMMAND_FAILED")
        # The owner's own click: a new sign-in either way, so walled sites are re-checked.
        note_sign_in_window(root, True, opened=True)
    return state


def sign_in_status(root=PROJECT_ROOT, *, runner=subprocess.run):
    """Window state (verified only if it was open) and each site's saved state."""
    window = check_sign_in_window(root, runner=runner) if sign_in_window(root)["open"] else sign_in_window(root)
    return {"window_open": window["open"], "line": sign_in_line(root), "sites": site_states(root),
            "needs": sign_in_needs(root)}


def signed_in_read(site, url, expression, folder, *, marker, root=PROJECT_ROOT, route=None, browser=None, **options):
    """One read in the lasting profile, gated and recorded for ``site``.

    ``marker(snapshot)`` returns True (signed in), False (sign-in wall) or None
    (no clear marker). The result carries ``signed_in`` with that answer.
    """
    wait = signed_in_wait(root, site)
    if wait:
        return {"state": "waiting", "reason": wait, "navigation_started": False, "stage": "sign_in_gate",
                "snapshot": None}
    if browser is None:
        from .native_public_browser import native_snapshot as browser
    from .signed_in_round import note_round_read
    started, result = time.monotonic(), None
    try:
        result = browser(url, expression, folder, profile="signed-in", root=root, **options)
    finally:
        if result is None or result.get("navigation_started") is not False:
            note_round_read(site, time.monotonic() - started)  # a read that never started uses no share
    if result.get("state") == "captured":
        seen = marker(result.get("snapshot"))
        if seen is not None:
            record_site_observation(root, site, seen, route=route)
        result = dict(result, signed_in=seen)
        if seen is False:
            result.update(sign_in_reason="OWNER_SIGN_IN_REQUIRED")
    return result
