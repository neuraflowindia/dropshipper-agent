#!/usr/bin/env python3
"""Check QuickCommerceAPI credits without searching or displaying credentials."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_FILE = ROOT / "config/local/quickcommerce.env"
ENDPOINT = "https://api.quickcommerceapi.com/v1/credits"
MAX_SECRET_BYTES = 8192
MAX_RESPONSE_BYTES = 65536


class ConfigurationError(Exception):
    """Safe error without credential contents."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_key(contents: str) -> str:
    values = []
    for line in contents.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if key.strip() == "QUICKCOMMERCE_API_KEY":
            if not separator:
                raise ConfigurationError("invalid setting")
            values.append(value.strip())
    if len(values) != 1:
        raise ConfigurationError("missing or duplicate setting")
    key = values[0]
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", key):
        raise ConfigurationError("empty or malformed setting")
    return key


def load_key(path: Path = PRIVATE_FILE) -> str:
    with path.open("rb") as handle:
        raw = handle.read(MAX_SECRET_BYTES + 1)
    if len(raw) > MAX_SECRET_BYTES:
        raise ConfigurationError("oversized file")
    return parse_key(raw.decode("utf-8-sig"))


def safe_summary(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return None
    fields = ("total_available", "total_used", "active_packs")
    if any(type(summary.get(field)) is not int or summary[field] < 0 for field in fields):
        return None
    if any(not isinstance(payload.get(field), list) for field in ("active", "inactive")):
        return None
    # Only these validated integer aggregates may leave this function.
    return {field: summary[field] for field in fields}


def check_connection(key: str, opener=None) -> tuple[bool, str, dict | None]:
    request = Request(ENDPOINT, headers={
        "X-API-Key": key, "Accept": "application/json",
        "User-Agent": "NeuraFlow-Connection-Check/1.0",
    }, method="GET")
    client = opener if opener is not None else build_opener(NoRedirects())
    try:
        with client.open(request, timeout=20) as response:
            if response.status != 200:
                return False, "Unexpected HTTP status; connection not confirmed.", None
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return False, "Response exceeded the safety limit; connection not confirmed.", None
        summary = safe_summary(json.loads(raw))
        if summary is None:
            return False, "Unexpected credit response structure; connection not confirmed.", None
        return True, "Authenticated credit check succeeded (HTTP 200). No product search was made.", summary
    except HTTPError as error:
        code = error.code
        error.close()
        if code == 401:
            return False, "Authentication rejected (HTTP 401). Check the saved key.", None
        if code == 403:
            return False, "Access denied (HTTP 403). Review account restrictions; do not assume an invalid key.", None
        if code == 402:
            return False, "Provider returned HTTP 402 on the documented free credit endpoint; investigate before purchasing.", None
        if code == 429:
            return False, "Rate limit reached (HTTP 429). No automatic retry was attempted.", None
        if 300 <= code < 400:
            return False, "Redirect refused; no credential was forwarded.", None
        return False, "Provider returned an HTTP error. No raw response was logged.", None
    except (URLError, TimeoutError, OSError):
        return False, "Network or TLS connection failed. Credential validity remains unverified.", None
    except (ValueError, UnicodeError):
        return False, "Response could not be decoded; connection not confirmed.", None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Perform one free authenticated credit check.")
    args = parser.parse_args(argv)
    if not args.live:
        print("No network request made. Use --live for one free credit-balance check.")
        return 2
    try:
        success, message, summary = check_connection(load_key())
    except (OSError, UnicodeError, ConfigurationError):
        print("CHECK FAILED: Save one valid QUICKCOMMERCE_API_KEY in config/local/quickcommerce.env. File contents were not displayed.")
        return 2
    except Exception:
        print("CHECK FAILED: Unexpected local error; details suppressed to protect credentials.")
        return 2
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print(("PASS: " if success else "CHECK FAILED: ") + message)
    if success:
        print(json.dumps(summary, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
