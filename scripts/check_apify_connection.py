#!/usr/bin/env python3
"""Explicit, read-only Apify check. Never print secrets or raw responses."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_FILE = ROOT / "config/local/providers.env"
ENDPOINT = "https://api.apify.com/v2/actor-runs?limit=1&desc=true"
MAX_SECRET_BYTES = 8192
MAX_RESPONSE_BYTES = 65536


class ConfigurationError(Exception):
    """Invalid local input; never include its contents in exception messages."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to a redirected address.
        return None


def parse_token(contents: str) -> str:
    values = []
    for line in contents.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if key.strip() == "APIFY_TOKEN":
            if not separator:
                raise ConfigurationError("invalid setting")
            values.append(value.strip())
    if len(values) != 1:
        raise ConfigurationError("missing or duplicate setting")
    token = values[0]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", token):
        raise ConfigurationError("empty or malformed setting")
    return token


def load_token(path: Path = PRIVATE_FILE) -> str:
    # Bounded input; no shell sourcing, environment dumps or command-line token.
    with path.open("rb") as handle:
        raw = handle.read(MAX_SECRET_BYTES + 1)
    if len(raw) > MAX_SECRET_BYTES:
        raise ConfigurationError("oversized file")
    return parse_token(raw.decode("utf-8-sig"))


def valid_run_list(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return False
    count, total, offset = (data.get(key) for key in ("count", "total", "offset"))
    return (
        all(type(value) is int for value in (count, total, offset))
        and offset == 0
        and 0 <= count <= 1
        and total >= count
        and count == len(data["items"])
        and all(isinstance(item, dict) for item in data["items"])
    )


def check_connection(token: str, opener=None) -> tuple[bool, str]:
    """One GET, no retries, no writes; return only fixed, non-sensitive messages."""
    request = Request(
        ENDPOINT,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "User-Agent": "NeuraFlow-Connection-Check/1.0"},
        method="GET",
    )
    client = opener if opener is not None else build_opener(NoRedirects())
    try:
        with client.open(request, timeout=20) as response:
            if response.status != 200:
                return False, "Unexpected HTTP status; connection not confirmed."
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return False, "Response exceeded the safety limit; connection not confirmed."
        if not valid_run_list(json.loads(raw)):
            return False, "Unexpected response structure; connection not confirmed."
        return True, "Authenticated run-list read succeeded (HTTP 200). No Actor was started."
    except HTTPError as error:
        code = error.code
        error.close()
        if code == 401:
            return False, "Authentication rejected (HTTP 401). Check the saved key or its expiry."
        if code == 403:
            return False, "Access denied (HTTP 403). Review scope/account restrictions; do not assume the key is invalid."
        if code == 429:
            return False, "Rate limit reached (HTTP 429). No automatic retry was attempted."
        if 300 <= code < 400:
            return False, "Redirect refused; no credential was forwarded."
        return False, "Apify returned an HTTP error. No raw response was logged."
    except (URLError, TimeoutError, OSError):
        return False, "Network or TLS connection failed. Credential validity remains unverified."
    except (ValueError, UnicodeError):
        return False, "Response could not be decoded; connection not confirmed."


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Perform one authenticated read-only request.")
    args = parser.parse_args(argv)
    if not args.live:
        print("No network request made. Use --live for one read-only connection check.")
        return 2
    try:
        token = load_token()
        success, message = check_connection(token)
    except (OSError, UnicodeError, ConfigurationError):
        print("CHECK FAILED: Save one valid APIFY_TOKEN in config/local/providers.env. File contents were not displayed.")
        return 2
    except Exception:
        # Guard the CLI boundary against accidental secret-bearing tracebacks.
        print("CHECK FAILED: Unexpected local error; diagnostic details were suppressed to protect credentials.")
        return 2
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print(("PASS: " if success else "CHECK FAILED: ") + message)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
