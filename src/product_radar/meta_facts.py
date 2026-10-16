"""Relevant Meta ads counted once, with the deeper facts a ranking can use.

The free Ad Library read and the paid deeper read (``meta_depth``) can both
see the same ad. Each library ID is one ad: a fact one read lacks is taken
from the other, so a deeper read fills gaps instead of contradicting the
free one. Two known values that disagree still exclude that ad (a conflict).

An advertiser may be known by its numeric page ID, by its page address, or
both. Any ad (or merged ad) showing both links them. Where a page address and
a page ID still cannot be linked they might be the same advertiser, so the
advertiser count is a lower bound: every distinct page ID, plus only as many
unlinked page addresses as outnumber the unlinked page IDs.

The website packet checker (``portal/functional-packet.mjs``) mirrors these
rules exactly.
"""

from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from .models import timestamp

FACTS_VERSION = "meta-depth-facts-v2"
RECENT_START_DAYS = 30
# The free read shows a calendar date and the deeper read an exact UTC time;
# a page shown in another time zone can differ by one day for the same ad.
START_TOLERANCE = timedelta(days=1)


def _page_address(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname in ("facebook.com", "www.facebook.com")
                and not url.username and not url.password and url.path.strip("/")
                and not url.path.startswith(("/ads/", "/login", "/help", "/privacy", "/policies"))):
            return "page-url:" + urlunsplit(("https", "www.facebook.com", url.path.rstrip("/").casefold(), url.query, ""))
    except ValueError:
        pass
    return None


def advertiser_identities(facts):
    """(page ID or None, page address or None). No name guess or invented ID."""
    return facts.get("advertiser_id"), _page_address(facts.get("advertiser_url"))


def advertiser_identity(facts):
    """The numeric page ID, else the visible official page address, else None."""
    number, address = advertiser_identities(facts)
    return number if number is not None else address


def _merge(old, new):
    """One ad seen twice: fill gaps, keep agreement; None marks a real conflict."""
    merged = dict(new, observed_at=max(old["observed_at"], new["observed_at"], key=timestamp))
    if old["page_id"] is not None and new["page_id"] is not None and old["page_id"] != new["page_id"]:
        return None
    if (old["page_id"] is None and new["page_id"] is None and old["page_address"] is not None
            and new["page_address"] is not None and old["page_address"] != new["page_address"]):
        return None  # two different addresses and no page ID to settle them
    for field in ("page_id", "page_address"):
        merged[field] = old[field] if old[field] is not None else new[field]
    merged["advertiser"] = merged["page_id"] if merged["page_id"] is not None else merged["page_address"]
    a, b = old["active"], new["active"]
    if a is not None and b is not None and a != b:
        # Active status changes over time: the later read governs.
        order = (timestamp(old["observed_at"]), timestamp(new["observed_at"]))
        if order[0] == order[1]:
            return None
        merged["active"] = a if order[0] > order[1] else b
    else:
        merged["active"] = a if b is None else b
    a, b = old["started_at"], new["started_at"]
    if a is not None and b is not None:
        if abs(timestamp(a) - timestamp(b)) > START_TOLERANCE:
            return None
        merged["started_at"] = a if timestamp(a) <= timestamp(b) else b
    else:
        merged["started_at"] = a if b is None else b
    return merged


def relevant_ads(observations):
    """(ads by library ID, conflicting IDs) from relevant Meta observations.

    ``observations`` are (observation_id, observation) pairs. The last
    observation of an ad is its representative evidence ID, as before.
    """
    ads, conflicts = {}, set()
    for obs_id, observation in observations:
        facts = observation["facts"]
        number, address = advertiser_identities(facts)
        value = {"id": obs_id, "advertiser": number if number is not None else address, "page_id": number,
                 "page_address": address, "active": facts["active"], "started_at": facts["started_at"],
                 "observed_at": observation["observed_at"]}
        ad_id = facts["ad_id"]
        if ad_id in ads and ad_id not in conflicts:
            merged = _merge(ads[ad_id], value)
            if merged is None:
                conflicts.add(ad_id)
            else:
                value = merged
        ads[ad_id] = value
    return ads, conflicts


def _address(name):
    return name.startswith("page-url:")


def advertiser_groups(ads):
    """(linked identity groups, unlinked-address groups that might repeat a page ID, lower-bound count)."""
    parent = {}
    def find(name):
        while parent.setdefault(name, name) != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name
    for ad in ads:
        names = [name for name in (ad["page_id"], ad["page_address"]) if name is not None]
        for name in names:
            find(name)
        if len(names) == 2:
            parent[find(names[0])] = find(names[1])
    grouped = {}
    for name in sorted(parent):
        grouped.setdefault(find(name), set()).add(name)
    groups = [grouped[root] for root in sorted(grouped)]
    numbers = {name for group in groups for name in group if not _address(name)}
    addresses_only = sum(all(_address(name) for name in group) for group in groups)
    numbers_only = sum(not any(_address(name) for name in group) for group in groups)
    ambiguous = addresses_only > 0 and numbers_only > 0
    return groups, ambiguous, len(numbers) + max(0, addresses_only - numbers_only)


def advertiser_count(ads):
    """Distinct relevant advertisers, never counting one advertiser twice."""
    return advertiser_groups(ads)[2]


def meta_state(ads, conflicts=()):
    """The Meta market state shared by the market count and the planner."""
    active = [ad for ad in ads if ad["active"] is True]
    # Age is established at collection, not by ageing a cached ad on our disk.
    aged = any(ad["started_at"] is not None
               and timestamp(ad["observed_at"]) - timestamp(ad["started_at"]) >= timedelta(days=14) for ad in active)
    positive = advertiser_count(ads) >= 3 or (len(active) >= 5 and aged)
    complete = bool(ads) and not conflicts and all(
        ad[field] is not None for ad in ads for field in ("advertiser", "active", "started_at"))
    return "Positive" if positive else "Not-positive" if complete else "Insufficient"


def advertiser_firsts(ads):
    """Each counted advertiser's earliest relevant ad: its start, its observation
    and whether it started at most 30 days before that observation.

    These are exactly the advertisers ``depth_facts`` counts for
    ``recent_start_advertisers`` (the report's advertiser timeline draws them):
    an advertiser with any undated relevant ad is left out, and while unlinked
    page addresses might repeat unlinked page IDs only groups with a page ID count.
    """
    groups, ambiguous, _ = advertiser_groups(ads)
    firsts = []
    for group in groups:
        if ambiguous and all(_address(name) for name in group):
            continue
        own = [ad for ad in ads if {ad["page_id"], ad["page_address"]} & group]
        if any(ad["started_at"] is None for ad in own):
            continue
        # Ties (the same start) go to the earliest observation, then the id: the portal mirrors this.
        first = min(own, key=lambda ad: (timestamp(ad["started_at"]), timestamp(ad["observed_at"]), ad["id"]))
        age = timestamp(first["observed_at"]) - timestamp(first["started_at"])
        firsts.append({"started_at": first["started_at"], "observed_at": first["observed_at"],
                       "recent": timedelta(0) <= age <= timedelta(days=RECENT_START_DAYS)})
    return sorted(firsts, key=lambda row: (timestamp(row["started_at"]), timestamp(row["observed_at"])))


def depth_facts(ads):
    """Deeper Meta facts over valid relevant ads (no conflict).

    ``recent_start_advertisers``: advertisers whose earliest relevant ad had
    started at most 30 days before that ad was observed. It is measured at
    observation, so it does not drift day by day. An advertiser with any
    undated relevant ad is not counted, and while unlinked page addresses
    might repeat unlinked page IDs only groups with a page ID count.
    """
    _, _, advertisers = advertiser_groups(ads)
    firsts = advertiser_firsts(ads)
    recent = sum(row["recent"] for row in firsts) if firsts else None
    dated = [ad["started_at"] for ad in ads if ad["started_at"] is not None]
    return {"version": FACTS_VERSION, "relevant_ads": len(ads), "advertisers": advertisers,
            "active_ads": sum(ad["active"] is True for ad in ads),
            "oldest_started_at": min(dated, key=timestamp) if dated else None,
            "newest_started_at": max(dated, key=timestamp) if dated else None,
            "recent_start_advertisers": recent,
            "ads_without_advertiser": sum(ad["advertiser"] is None for ad in ads),
            "ads_without_start_date": sum(ad["started_at"] is None for ad in ads),
            "complete": bool(ads) and all(ad["advertiser"] is not None and ad["started_at"] is not None for ad in ads)}
