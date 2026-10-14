"""Remove the research account's own details from a signed-in IndiaMART page.

Removal is by position, never by guessing words, so supplier names, listing
links and catalogue addresses are never rewritten:

* the page data's visitor object (``imeshVObject``) keeps its keys, with every
  non-empty value replaced by "redacted" (so a signed-in visitor stays
  recognisable), and the header's own HTML (``headerData``) is dropped;
* the contents of the page header, the login strip, the messages menu and the
  identified-visitor pop-up are removed, and form fields lose their values;
* only the account's full mobile number (with or without 91, +91 or 0) and
  e-mail address, read from the visitor object, are removed wherever else they
  are shown - never inside a link, a file name or a longer number.

The saved text and links are then rebuilt from the cleaned HTML. A saved
browser result that cannot be read or cleaned is moved, unread, into a
quarantine folder that is never backed up (``protect_saved``).
"""

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin

from .models import SafetyError
from .storage import atomic_json, safe_path

REDACTED = "redacted"
QUARANTINE = "signed-in-quarantine"
MAX_SAVED = 12 * 1024 * 1024
MAX_TEXT = 24000
_ACCOUNT_IDS = {"remote-header-shell", "loginstrip", "sign_in", "identifiedpopuphtml", "message-center",
                "hd_usercity", "hd_usercity_new"}
_ACCOUNT_TAGS = {"header", "textarea"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_SKIP = {"script", "style", "noscript", "template", "svg"}
_BLOCKS = {"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "footer", "form", "h1", "h2",
           "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "section", "table", "td", "th",
           "tr", "ul", "button"}
_NEXT = re.compile(r"(<script\b[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>)(.*?)(</script>)", re.I | re.S)
_VISITOR = re.compile(r'("imeshVObject"\s*:\s*)(\{[^{}]*\})')
_INPUT = re.compile(r"<input\b[^>]*>", re.I)
_VALUE = re.compile(r"""(\bvalue\s*=\s*)("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_MOBILE_KEYS = {"mb1", "mb2", "mobile", "phone", "pn"}
_EMAIL_KEYS = {"em", "email"}


def _blank(visitor, contacts):
    """Keys kept; text and numbers become "redacted", nested values are dropped."""
    result = {}
    for field, item in visitor.items():
        shown = str(item).strip() if isinstance(item, (str, int, float)) and not isinstance(item, bool) else ""
        if shown:
            contacts.append((field.casefold(), shown))
        result[field] = (REDACTED if shown else "" if isinstance(item, str) else
                         item if isinstance(item, bool) or item is None else None)
    return result


def _data(value, contacts):
    """The page data with the visitor object blanked and the header HTML dropped."""
    if isinstance(value, dict):
        result = {}
        for field, item in value.items():
            if field.casefold() == "imeshvobject" and isinstance(item, dict):
                result[field] = _blank(item, contacts)
            elif field.casefold() == "headerdata":
                result[field] = {}
            else:
                result[field] = _data(item, contacts)
        return result
    if isinstance(value, list):
        return [_data(item, contacts) for item in value]
    return value


def _script_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _clean_data(html, contacts):
    def next_data(found):
        try:
            value = json.loads(found[2])
        except ValueError:
            return found[1] + "{}" + found[3]  # unreadable page data cannot be checked, so none is kept
        return found[1] + _script_json(_data(value, contacts)) + found[3]

    def visitor(found):
        try:
            value = json.loads(found[2])
        except ValueError:
            return found[1] + "{}"
        return found[1] + _script_json(_blank(value, contacts)) if isinstance(value, dict) else found[1] + "{}"
    return _VISITOR.sub(visitor, _NEXT.sub(next_data, html))


class _AccountParts(HTMLParser):
    """Character spans of the account's own page parts (contents only)."""

    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.starts, self.stack, self.spans, self.size = [0], [], [], len(html)
        for line in html.splitlines(keepends=True):
            self.starts.append(self.starts[-1] + len(line))

    def _offset(self):
        line, column = self.getpos()
        return self.starts[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        ident = (dict(attrs).get("id") or "").casefold()
        self.stack.append((tag, self._offset() + len(self.get_starttag_text()), tag in _ACCOUNT_TAGS or ident in _ACCOUNT_IDS))

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, -1, -1):
            if self.stack[position][0] == tag:
                if self.stack[position][2]:
                    self.spans.append((self.stack[position][1], self._offset()))
                del self.stack[position:]
                return

    def unclosed(self):
        # An account part left open runs to the end of the page: removed too.
        return [(start, self.size) for _, start, matched in self.stack if matched]


def _cut(html):
    parts = _AccountParts(html)
    parts.feed(html)
    parts.close()
    kept, position = [], 0
    for start, end in sorted(parts.spans + parts.unclosed()):
        if start >= position:
            kept.append(html[position:start])
            position = end
        else:
            position = max(position, end)
    kept.append(html[position:])
    return "".join(kept)


def _contact_patterns(contacts):
    patterns = []
    for field, item in contacts:
        digits = re.sub(r"\D", "", item)
        if field in _MOBILE_KEYS and len(digits) >= 10:
            number = r"[\s-]?".join(digits[-10:])
            patterns.append(re.compile(r"(?<![\w/.=%-])(?:\+?91[\s-]?|0)?" + number + r"(?![\w])"))
        elif field in _EMAIL_KEYS and "@" in item:
            patterns.append(re.compile(r"(?<![\w.%+-])" + re.escape(item) + r"(?![\w.-])", re.I))
    return patterns


def _without_contacts(value, patterns):
    for pattern in patterns:
        value = pattern.sub(REDACTED, value)
    return value


class _Text(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links, self.skip = base, [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += tag not in _VOID
        if tag in _BLOCKS:
            self.parts.append("\n")
        href = dict(attrs).get("href") if tag == "a" else None
        if href and len(self.links) < 60:
            try:
                address = urljoin(self.base, href)
            except ValueError:
                return
            if address.startswith(("http://", "https://")):
                self.links.append(address)

    def handle_endtag(self, tag):
        if tag in _SKIP and self.skip:
            self.skip -= 1
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).split("\n"))
        return "\n".join(line for line in lines if line)


def redact_snapshot(snapshot):
    """The page snapshot without the account's details; text and links rebuilt."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("html"), str):
        return snapshot
    contacts = []
    html = _cut(_clean_data(snapshot["html"], contacts))
    html = _INPUT.sub(lambda found: _VALUE.sub(r'\1""', found[0]), html)
    patterns = _contact_patterns(contacts)
    html = _without_contacts(html, patterns)
    reader = _Text(snapshot.get("url") if isinstance(snapshot.get("url"), str) else "")
    reader.feed(html)
    reader.close()
    text = reader.text()
    title = snapshot.get("title")
    return dict(snapshot, html=html, text=text[:MAX_TEXT], text_truncated=len(text) > MAX_TEXT, links=reader.links,
                title=_without_contacts(title, patterns) if isinstance(title, str) else title)


def redact_output(output):
    """A native browser result with its page snapshot cleaned."""
    if isinstance(output, dict) and isinstance(output.get("snapshot"), dict):
        return dict(output, snapshot=redact_snapshot(output["snapshot"]))
    return output


def _quarantine(path):
    folder = safe_path(path.parent.parent / QUARANTINE)
    target = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + path.parent.name + "-" + path.name + ".unread")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.rename(target)
    except OSError:
        try:
            path.unlink()
        except OSError:
            raise SafetyError("SIGNED_IN_REDACTION_FAILED") from None


def protect_saved(path):
    """Clean a saved browser result in place; one that cannot be read or cleaned
    is moved, unread, to the never-backed-up quarantine. Returns what happened."""
    path = safe_path(path)
    if not path.exists() and not path.is_symlink():
        return "absent"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SAVED:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        clean = redact_output(value)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        _quarantine(path)
        return "quarantined"
    if clean != value:
        atomic_json(path, clean)
    return "redacted"
