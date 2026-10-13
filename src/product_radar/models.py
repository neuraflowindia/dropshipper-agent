"""Small validated contracts for the first benchmark; no provider calls."""

from dataclasses import dataclass, asdict, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


class SafetyError(Exception):
    """Fixed codes only: never include keys, server bodies or private values."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise SafetyError("INVALID_IDENTIFIER")
    return value


def usd(value):
    # Refuse floats/bools: reservations must not depend on binary rounding.
    if not isinstance(value, (str, Decimal)):
        raise SafetyError("INVALID_USD")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise SafetyError("INVALID_USD") from None
    if not number.is_finite() or number < 0 or number > 10000 or number.as_tuple().exponent < -8:
        raise SafetyError("INVALID_USD")
    return number


def utc_now(timespec="seconds"):
    """Whole seconds by default (truncated, not rounded).

    A commit compared with a sub-second source time (the owned Windows browser
    stamps 7-digit completion times) asks for "microseconds"; otherwise an
    earlier observation in the same second looks later than its own commit.
    """
    return datetime.now(timezone.utc).isoformat(timespec=timespec)


def timestamp(value):
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        return instant
    except (TypeError, ValueError):
        raise SafetyError("INVALID_TIME") from None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode_json(raw, *, decimals=False):
    """Reject ambiguous duplicate fields and non-JSON numbers at boundaries."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(value):
        raise ValueError

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant,
                          parse_float=Decimal if decimals else float)
    except (ValueError, UnicodeError, RecursionError):
        raise SafetyError("JSON_RESPONSE_INVALID") from None


def json_safe(value):
    # Original wire bytes remain authoritative. Normalized metadata can store
    # an exact Decimal as a string without pretending it was an original string.
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class AccountBinding:
    # Private file only. account_key is an opaque stable ID for the actual
    # provider account, not a role, email, token hash or arbitrary new alias.
    account_key: str = field(repr=False)
    period: str
    role: str
    generation: str
    token_digest: str = field(repr=False)
    checked_at: str
    access_confirmed: bool
    owner_account_confirmed: bool

    def __post_init__(self):
        for value in (self.account_key, self.period, self.generation):
            identifier(value)
        if self.role not in ("benchmark", "production"):
            raise SafetyError("INVALID_ROLE")
        if not isinstance(self.token_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.token_digest):
            raise SafetyError("INVALID_BINDING")
        timestamp(self.checked_at)
        if type(self.access_confirmed) is not bool or type(self.owner_account_confirmed) is not bool:
            raise SafetyError("INVALID_BINDING")

    @property
    def namespace(self):
        return self.account_key + ":" + self.period

    def validate_key(self, token, role, now=None):
        if self.role != role or not self.access_confirmed or not self.owner_account_confirmed:
            raise SafetyError("ACCOUNT_NOT_CONFIRMED")
        age = ((now or datetime.now(timezone.utc)) - timestamp(self.checked_at)).total_seconds()
        if age < 0 or age > 3600:
            raise SafetyError("ACCOUNT_CHECK_STALE")
        if hashlib.sha256(token.encode("utf-8")).hexdigest() != self.token_digest:
            raise SafetyError("KEY_CHANGED_RECHECK_REQUIRED")


@dataclass(frozen=True)
class PreparedMeta:
    attempt_id: str
    actor: str
    build: str
    expected_build_id: str
    purpose: str
    input: dict
    options: dict
    reserved_usd: str
    reviewed_at: str
    public_metadata_hash: str
    work_scope: str = "trial"

    def __post_init__(self):
        if self.work_scope not in ("trial", "production"):
            raise SafetyError("INVALID_WORK_SCOPE")
        identifier(self.attempt_id)
        if self.actor != "curious_coder~facebook-ads-library-scraper" or self.build != "2.7.26":
            raise SafetyError("UNREVIEWED_ACTOR_BUILD")
        if self.expected_build_id != "wYDFs3xTPOVCo9Wag" or self.purpose not in ("discovery", "validation"):
            raise SafetyError("UNREVIEWED_META_SCOPE")
        if usd(self.reserved_usd) <= 0 or usd(self.reserved_usd) > Decimal("0.03"):
            raise SafetyError("UNREVIEWED_ATTEMPT_CAP")
        timestamp(self.reviewed_at)
        if not isinstance(self.public_metadata_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", self.public_metadata_hash):
            raise SafetyError("INVALID_METADATA_HASH")
        expected_options = {"build": self.build, "timeout": 300,
                            "maxTotalChargeUsd": self.reserved_usd, "restartOnError": "false"}
        if not isinstance(self.options, dict) or self.options != expected_options or type(self.options.get("timeout")) is not int:
            raise SafetyError("UNSAFE_LAUNCH_OPTIONS")
        from urllib.parse import urlsplit, parse_qs
        body = self.input
        if not isinstance(body, dict) or set(body) != {"urls", "limitPerSource", "scrapeAdDetails", "runTag"}:
            raise SafetyError("UNREVIEWED_INPUT")
        if body["scrapeAdDetails"] is not False or body["runTag"] != self.attempt_id:
            raise SafetyError("UNREVIEWED_INPUT")
        target = 5 if self.purpose == "discovery" else 10
        if type(body["limitPerSource"]) is not int or body["limitPerSource"] != target:
            raise SafetyError("UNREVIEWED_INPUT")
        if not isinstance(body["urls"], list) or len(body["urls"]) != 1:
            raise SafetyError("UNREVIEWED_INPUT")
        source = body["urls"][0]
        if not isinstance(source, dict) or set(source) != {"url"} or not isinstance(source["url"], str):
            raise SafetyError("UNREVIEWED_INPUT")
        parsed = urlsplit(source["url"])
        if parsed.scheme != "https" or parsed.netloc != "www.facebook.com" or parsed.path != "/ads/library/" or parsed.fragment:
            raise SafetyError("UNSAFE_SEARCH_URL")
        try:
            query = parse_qs(parsed.query, strict_parsing=True)
        except ValueError:
            raise SafetyError("UNSAFE_SEARCH_SCOPE") from None
        required = {"active_status": ["active"], "ad_type": ["all"], "country": ["IN"],
                    "search_type": ["keyword_unordered"], "media_type": ["all"]}
        if set(query) != set(required) | {"q"} or any(query[k] != v for k, v in required.items()):
            raise SafetyError("UNSAFE_SEARCH_SCOPE")
        if len(query["q"]) != 1 or not 1 <= len(query["q"][0].strip()) <= 120:
            raise SafetyError("INVALID_SEARCH_TERM")

    def to_dict(self):
        value = asdict(self)
        if self.work_scope == "trial":
            value.pop("work_scope")
        return value

    @property
    def config_hash(self):
        return digest(self.to_dict())


@dataclass(frozen=True)
class PreparedResearch(PreparedMeta):
    """Same persisted request envelope, independent Amazon/Trends validation."""

    def __post_init__(self):
        if self.work_scope not in ("trial", "production"):
            raise SafetyError("INVALID_WORK_SCOPE")
        from .research_sources import validate_request
        validate_request(self)


def prepared_request(value):
    if not isinstance(value, dict):
        raise SafetyError("PREPARATION_INVALID")
    kind = PreparedMeta if value.get("actor") == "curious_coder~facebook-ads-library-scraper" else PreparedResearch
    try:
        return kind(**value)
    except TypeError:
        raise SafetyError("PREPARATION_INVALID") from None


def request_stage(prepared):
    if isinstance(prepared, PreparedResearch):
        from .research_sources import source_spec
        return source_spec(prepared.actor)[0]
    return "meta"


def as_production_request(prepared):
    """Explicit new scope/hash; provider payload, actor bounds and identity stay."""
    return replace(prepared, work_scope="production")
