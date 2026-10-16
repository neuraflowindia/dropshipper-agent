"""Refuse a rendered report edition that could run code or fetch anything.

The client edition is one self-contained HTML file: every embedded resource is
a ``data:`` URL and links only open public pages or jump inside the document.
This reads the markup the way a browser does (tags, attributes and style sheets),
not as raw text, so escaped words in the report ("url(", "href=") are never
mistaken for markup, while a fetching attribute, a ``<link>``, an ``@import`` or
a remote ``url()`` is always caught.
"""

from html.parser import HTMLParser

# Elements that run code, embed another document or fetch media/styles by themselves.
FORBIDDEN_TAGS = {"script", "iframe", "frame", "frameset", "object", "embed", "applet", "portal", "link", "base",
                  "audio", "video", "source", "track", "form", "input", "button", "textarea", "select"}
# Attributes that fetch (or submit) a resource; never needed in the report.
FETCHING_ATTRIBUTES = {"srcset", "imagesrcset", "poster", "background", "action", "formaction", "ping", "lowsrc",
                       "dynsrc", "codebase", "archive", "data", "longdesc", "manifest", "srcdoc", "xlink:actuate"}
LINK_ATTRIBUTES = {"href", "xlink:href"}
PAGE_LINK = ("https://", "http://", "#")


def css_fetches(css):
    """True when a style sheet could fetch something: a non-data ``url()``, an ``@import``,
    an ``image-set()``, or a CSS escape outside a string (which could spell any of them).
    Text inside CSS strings (for example a footer's ``content:"..."``) is not a fetch."""
    index, size = 0, len(css)
    while index < size:
        char = css[index]
        if char in "\"'":
            end = index + 1
            while end < size and css[end] != char:
                end += 2 if css[end] == "\\" else 1
            index = end + 1
            continue
        if css.startswith("/*", index):
            end = css.find("*/", index + 2)
            index = size if end < 0 else end + 2
            continue
        if char == "\\":
            return True
        head = css[index:index + 18].lower()
        if head.startswith(("@import", "image-set(", "-webkit-image-set(")):
            return True
        if head.startswith("url("):
            argument = css[index + 4:].lstrip()
            if argument[:1] in "\"'":
                argument = argument[1:]
            if not argument[:5].lower() == "data:":
                return True
            end = css.find(")", index)
            index = size if end < 0 else end + 1
            continue
        index += 1
    return False


class _Guard(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refused = False
        self.in_style = 0

    def _attributes(self, tag, attrs):
        for name, value in attrs:
            name, value = name.lower(), (value or "").strip()
            lowered = value.lower()
            if (name.startswith("on") or name in FETCHING_ATTRIBUTES
                    or name == "src" and not lowered.startswith("data:")
                    or name in LINK_ATTRIBUTES and not (lowered.startswith(PAGE_LINK) if tag == "a"
                                                        else lowered.startswith(("#", "data:")))
                    or name == "style" and css_fetches(value)
                    or name == "http-equiv"):
                self.refused = True

    def handle_starttag(self, tag, attrs):
        if tag in FORBIDDEN_TAGS:
            self.refused = True
        self._attributes(tag, attrs)
        if tag == "style":
            self.in_style += 1

    def handle_startendtag(self, tag, attrs):
        if tag in FORBIDDEN_TAGS:
            self.refused = True
        self._attributes(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "style" and self.in_style:
            self.in_style -= 1

    def handle_data(self, data):
        if self.in_style and css_fetches(data):
            self.refused = True


def fetches_or_runs(html):
    """True when the edition's markup could run code or fetch a resource."""
    guard = _Guard()
    guard.feed(html)
    guard.close()
    return guard.refused
