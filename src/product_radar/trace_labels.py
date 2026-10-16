"""Plain words for the research trace, with the programme's own code in brackets.

Every label is for an owner who does not read code. Unknown codes are still
shown (never hidden), as "Other reason" with the code in brackets, so a new
code in the records is visible without a change here.
"""

import re


ROUTES = {
    "meta-ads": "Meta Ad Library (Facebook/Instagram ads)",
    "google-trends": "Google Trends",
    "amazon-search": "Amazon search",
    "amazon-bestsellers": "Amazon bestseller lists",
    "flipkart-search": "Flipkart search",
    "google-maps": "Google Maps (supplier search)",
    "indiamart": "IndiaMART",
    "tradeindia": "TradeIndia",
    "company-pages": "Supplier company websites",
    "deodap": "DeoDap (dropshipping platform)",
    "baapstore": "BaapStore (dropshipping platform)",
    "dropdash": "Dropdash (dropshipping platform)",
    "blinkit": "Blinkit (quick delivery)",
    "zepto": "Zepto (quick delivery)",
    "instamart": "Instamart (quick delivery)",
    "public_image_http": "Product photo downloads",
}

# Names the saved raw results use for where they came from.
SOURCES = {"meta": "Meta Ad Library", "amazon": "Amazon", "flipkart": "Flipkart", "supplier": "Supplier sites",
           "marketplace": "Amazon/Flipkart", "dropship-platforms": "Dropshipping platforms",
           "quickcommerce": "Quick-delivery apps", "trends": "Google Trends"}

STATES = {
    # A website question's own status.
    "prepared": "Not asked yet",
    "dispatching": "Being asked right now (or stopped mid-ask)",
    "acquired": "Answered with results",
    "completed_no_results": "Answered: the site said there are no results",
    "insufficient": "Answered, but the answer could not be used",
    "blocked": "Held back or refused by the site",
    "failed": "Failed",
    "reused": "Answered from an earlier saved read (no new website visit)",
    "waiting": "Waiting",
    "not_attempted": "Not tried this round",
    "deferred": "Put off for later",
    "skipped": "Skipped",
    # One website read.
    "reserved": "Planned, not sent yet",
    "response_saved": "Answer saved",
    "completed_empty": "Answer saved: nothing found",
    "failed_access": "The site refused access",
    "execution_unknown": "Unknown: the read stopped before its answer was saved",
    "set_aside": "Set aside (saved answer damaged or changed)",
    "read": "Read",
    "not_started": "Not started",
    "uncertain": "Uncertain: sent, answer not confirmed",
    "unreadable": "Saved file could not be read",
    "partial": "Answer saved (partly usable)",
    "captured": "Answer saved (still to be matched to products)",
    "unavailable": "The page was not available (missing or unreadable)",
    # AI judgement of an answer.
    "interpretation_pending": "Answer saved; the AI has not judged it yet",
    "completed_relevant": "Answer judged: relevant to this product",
    "usable": "Usable",
    "empty": "Nothing found",
    "in_progress": "In progress",
}

REASONS = {
    "WORK_TURN_SOURCE_LIMIT": "Waiting: this round's website-read limit was used up",
    "WORK_TURN_MODEL_LIMIT": "Waiting: this round's AI-use limit was used up",
    "WORK_TURN_PURPOSE_LIMIT": "Waiting: this round's limit for this kind of AI work was used up",
    "OWNER_SIGN_IN_WINDOW_OPEN": "Waiting: the owner's sign-in window was open on the computer",
    "OWNER_SIGN_IN_REQUIRED": "Waiting: the site needs the owner to sign in again",
    "SIGNED_IN_READ_SPACING": "Waiting because the site asked us to slow down (polite gap between reads)",
    "SIGNED_IN_ROUND_READS_USED": "Waiting: this round's signed-in reading time was used up",
    "SOURCE_ACCESS_PROBE_PENDING": "Waiting: one test read must succeed after a refusal before others go",
    "TRENDS_RENDERING_PAUSED": "Waiting: Google Trends page drawing was paused",
    "TRENDS_MINIMISED_NOT_RENDERED": "Google Trends did not draw its chart (browser window minimised)",
    "QUICKCOMMERCE_RESERVED_CROWDING_NOT_CHECKED": "Waiting: quick-delivery check (costs a credit) waits for the Amazon/Flipkart crowding check",
    "SUPPLIER_COMPANY_ORIGINAL_PENDING": "Waiting: an earlier read of this company page must be finished first",
    "SUPPLIER_COMPANY_LEGACY_ORIGINAL_PENDING": "Waiting: an older read of this company page must be finished first",
    "SOURCE_ACTION_TIMEOUT": "The website took too long to answer",
    "SourceActionDeadline": "The website took too long to answer",
    "CompanyPagePending": "Company page still waiting for an earlier read",
    "SOURCE_ACQUISITION_ARTIFACT_CHANGED": "A saved answer file changed after saving, so it was not trusted",
    "SOURCE_ACQUISITION_ARTIFACT_DAMAGED": "A saved answer file was damaged, so it was set aside",
    "PROGRAMME_ACQUISITION_COUNTS_INVALID": "The program stopped a step because its own read counts did not add up",
    "SUPPLIER_NATIVE_READ_FAILED": "The browser read of the supplier site failed",
    "PUBLIC_TRANSPORT_UNAVAILABLE": "The internet connection to the site was not available",
    "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED": "The supplier site's page layout was not recognised",
    "MARKETPLACE_SCOPE_UNVERIFIED": "Results page did not prove it searched the right thing",
    "MARKETPLACE_CARDS_UNRECOGNISED": "Product cards on the page were not recognised",
    "MARKETPLACE_CARDS_OBSERVED": "Product cards were read",
    "MARKETPLACE_CARDS_PARTIALLY_USABLE": "Product cards were read (some details missing)",
    "META_ORIGINAL_SCOPE_MISMATCH": "Meta answered for a different search than asked",
    "DIRECT_TRENDS_CHART_EXPORT_VERIFIED": "Google Trends chart was read",
    "DIRECT_TRENDS_EXPLICIT_NO_DATA": "Google Trends said there is not enough search data",
    "WIDER_DISCOVERY_BROWSER_READ_NEXT": "Waiting for its turn in the browser",
    "SOURCE_ORIGINAL_RECEIPT_PENDING": "Sent, but the answer receipt was not saved yet",
    "WIDER_DISCOVERY_RESULT_NOT_SAVED": "The read stopped before its result was saved",
    "AI_RUNNER_NOT_ENABLED": "AI step could not run: AI was switched off for this run",
    "AI_PLAN_LIMIT_CREDITS_PROTECTED": "AI waiting: the weekly AI allowance is used up (paid credits protected)",
    "AI_FIT_CRITERION_MISSING": "AI answer missed a checklist point, so it was not accepted",
    "AI_PRODUCT_ENVELOPE_INVALID": "AI answer had the wrong shape, so it was not accepted",
    "AI_RESULT_MISSING_OR_DUPLICATED": "AI answer skipped or repeated an item, so it was not accepted",
    "AI_EVIDENCE_REFERENCE_INVALID": "AI answer pointed to evidence it was not given, so it was not accepted",
    "AI_MATCH_REFERENCE_INVALID": "AI answer pointed to a product it was not given, so it was not accepted",
    "AI_INTERRUPTED_REVIEW_SAVED_JOB": "AI job interrupted (program stopped); saved answer must be checked",
    "PRODUCT_IMAGE_TRANSPORT_FAILED": "The photo download failed",
    "RESEARCH_INDEX_SIZE_LIMIT": "Speed copy of the records was too large to save (no data lost)",
    "KeyboardInterrupt": "Stopped by the owner (Stop button)",
    "AttributeError": "Program error (a bug)",
    "SafetyError": "Program safety check stopped this step",
    # Pool decisions (why a product type is not report-ready).
    "crowding_pending": "Waiting for the Amazon/Flipkart crowding check",
    "demand_too_quiet": "Too quiet: fewer than 5 of 20 'want' points (ads, searches, lists, stock)",
    "below_checklist_threshold": "Checklist below the pass mark (fewer than 5 Meets)",
    "fit_information_pending": "Checklist still has points without a useful answer",
    "supplier_search_required": "Supplier search not done yet",
    "identity_pending": "AI has not settled what this product type is yet",
    "out_of_scope": "Outside the client's categories",
    "established_brand": "A big established brand (not allowed)",
    "checklist_review_required": "Checklist not filled yet",
    "market_coverage_required": "Needs answers from at least two kinds of market sources",
    "weak_market_signal": "Market sources answered but none was positive",
    "saved_facts_invalid": "Saved records for this type need correction",
    "saved_supplier_or_source_facts_invalid": "Saved supplier or website records need correction",
    "useful_update_review_required": "Already published once; the update needs review",
    "market_completion_optional": "Report-ready; more market answers are optional",
    "opportunity_scored": "Scored",
    # Association (matching listings to product types).
    "no_known_match": "New product type created (no existing type matched)",
    "llm_accepted": "AI compared it with existing product types (answer accepted)",
    "llm_new_type": "AI compared it with existing types: it is a new product type",
    "llm_same_type": "AI matched it to an existing product type",
}

LABELS = {
    "too_quiet": "Too quiet",
    "proven_but_crowded": "Proven but crowded",
    "early_bet": "Early bet",
    "sweet_spot": "Sweet spot",
    None: "Not labelled yet",
}

DESCRIPTION_KINDS = {
    "product": "A product (named by the AI)",
    "mixed": "Several products in one listing",
    "service": "A service, not a product",
    "uninterpretable": "Not understandable as a product",
}

AI_STEPS = {
    "research-abe-v1": "Name the products in raw listings",
    "research-b-functional-v2": "Match a listing to an existing product type",
    "research-b-functional-definition-v1": "Write the product type's definition",
    "research-b-functional-alias-v1": "Check for duplicate product types",
    "research-b-functional-example-v1": "Check example products for a type",
    "research-b-catalogue-scope-v1": "Check which map heading a type belongs to",
    "research-c-query-profile-v1": "Write search words for a product type",
    "research-c-query-profile-v2": "Write search words for a product type",
    "research-c-query-profile-v3": "Write search words for a product type",
    "research-c-catalogue-seed-v2": "Add new headings to the product map",
    "research-c-catalogue-topup-v1": "Top up the product map with new headings",
    "research-c-v2": "Suggest product-map headings from listings",
    "research-e-fit10-v1": "Fill the 10-point checklist",
    "research-e-fit10-v2": "Fill the 10-point checklist",
    "research-e-fit10-description-v3": "Fill the 10-point checklist (description)",
    "research-e-fit10-functional-v5": "Fill the 10-point checklist",
    "research-e-source-scope-v5": "Judge whether website results match the product type",
    "research-e-source-support-v4": "Judge whether website results match the product type",
    "research-e-source-relevance-v2": "Judge whether website results are relevant",
    "research-e-material-update-v1": "Review an update to a published product",
    "research-f-v3": "Read supplier pages (prices and terms)",
    "research-f-v4": "Read supplier pages (prices and terms)",
    "research-g-v4": "Write report text",
}

ROLE_FALLBACK = {"A": "Name products", "B": "Product type identity", "C": "Search words and product map",
                 "D": "Prepared research", "E": "Judging (checklist and relevance)", "F": "Supplier pages",
                 "G": "Report text"}

_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_:.-]{0,119}")


def _with_code(text, code):
    return f"{text} [{code}]"


def route(code):
    return _with_code(ROUTES.get(code, "Other website"), code) if code else "Unknown website"


def source(code):
    """Where a saved result came from (a website route or a source family)."""
    if code in ROUTES:
        return route(code)
    if code in SOURCES:
        return _with_code(SOURCES[code], code)
    return str(code)


def state(code):
    if code is None:
        return "No status saved"
    return _with_code(STATES.get(code, "Other status"), code)


def reason(code):
    """A reason code in plain words; free text (already plain) is shown as is."""
    if code is None:
        return "No reason saved"
    code = str(code)
    if code in REASONS:
        return _with_code(REASONS[code], code)
    if code in STATES:
        return _with_code(STATES[code], code)
    if _CODE.fullmatch(code) and (code.isupper() or "_" in code):
        return _with_code("Other reason", code)
    return code[:200]


def label(code):
    return _with_code(LABELS.get(code, "Other label"), code) if code is not None else LABELS[None]


def ai_step(prompt_version, role):
    text = AI_STEPS.get(prompt_version) or ROLE_FALLBACK.get(role) or "Other AI step"
    return _with_code(text, prompt_version or role or "unknown")


def description_kind(code):
    return _with_code(DESCRIPTION_KINDS.get(code, "Other answer"), code)
