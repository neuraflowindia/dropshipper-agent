"""Plain factual supplier displays; all numbers stay attached to their offer.

This formats already validated frozen facts. It neither interprets a page nor
turns a company policy, pack quote or minimum order into a product-wide claim.
"""

from .models import SafetyError


CONTRACT = "supplier-scoped-terms-v2"
_LABELS = {
    "single_piece_purchase": ("Single-piece purchase", "Single-piece kharid"),
    "direct_customer_dispatch": ("Dispatch to the customer", "Seedha customer ko dispatch"),
    "purchase_moq": ("Minimum purchase", "Minimum kharid"),
    "subscription_requirement": ("Subscription", "Subscription"),
    "recurring_fee_frequency": ("Recurring fee schedule", "Baar-baar lagne wali fee kab deni hai"),
    "monthly_fee": ("Monthly fee", "Har mahine ki fee"),
    "bulk_stock_prerequisite": ("Bulk stock required first", "Pehle bulk stock kharidna zaroori hai"),
    "cod": ("Cash on delivery", "Delivery par payment"),
    "india_coverage": ("Delivery area", "Delivery kahan tak hai"),
    "white_label": ("Product under your brand", "Aapke brand ke naam se product"),
    "white_label_shipping": ("Shipping under your brand", "Aapke brand ke naam se shipping"),
    "neutral_packaging": ("Plain packaging", "Saada packing"),
    "ecommerce_support": ("Online-selling support", "Online bechne mein madad"),
    "order_integration": ("Order-system connection", "Order system se connection"),
    "setup_fee": ("Setup fee", "Shuru karne ki fee"),
    "deposit": ("Deposit", "Jama karne ki rakam"),
    "per_order_fee": ("Fee per order", "Har order ki fee"),
}


def is_scoped_supplier(supplier):
    return supplier.get("scoped_supplier_contract") == CONTRACT


def _dated(row, hindi):
    value = row.get("observed_at", row.get("source_capture_at"))
    label = "Source date " if not hindi else "Source ki tareekh "
    result = label + value[:10] if value else ("Source date not stated" if not hindi else "Source ki tareekh nahi di gayi")
    if row.get("current") is not True:
        result += "; older information; check again" if not hindi else "; purani jankari; dobara check karein"
    return result


def _terms(rows, hindi):
    values = []
    for row in rows:
        if row.get("basis") == "not_stated":
            continue
        label = _LABELS.get(row["field"])
        if label is None:
            raise SafetyError("LIVE_SCOPED_SUPPLIER_FIELD_INVALID")
        text = label[1 if hindi else 0] + ": " + row["value"]
        if row.get("basis") == "inferred":
            text += " (estimated)" if not hindi else " (andaza)"
        elif row.get("basis") == "conflicting":
            text += " (statements conflict)" if not hindi else " (shartein aapas mein nahi milti hain)"
        if row.get("qualified") is not True:
            text += "; not confirmed for this offer" if not hindi else "; is offer ke liye confirm nahi hai"
        values.append(text + "; " + _dated(row, hindi))
    return values


def scoped_supplier_lines(supplier, language="en"):
    if not is_scoped_supplier(supplier) or language not in {"en", "hi-Latn"}:
        raise SafetyError("LIVE_SCOPED_SUPPLIER_DISPLAY_INVALID")
    hindi = language == "hi-Latn"
    result = ["Company information and each offer's terms are separate." if not hindi else
              "Company ki jankari aur har offer ki shartein alag hain."]
    for row in supplier["company_workflows"]:
        result.append(("Company information: " if not hindi else "Company ki jankari: ") + row["scope_label"] + "; " + _dated(row, hindi))
        result.extend(_terms(row["terms"], hindi))
        for fee in row["fees"]:
            result.append(_price(fee, hindi, fee=True))
    for number, offer in enumerate(supplier["product_offers"], 1):
        result.append(("Offer " if not hindi else "Offer ") + str(number) + ": " + offer["scope_label"] + "; " + _dated(offer, hindi))
        # Retain every literal offer-local restriction, including an older
        # restriction which cannot inherit a new generic company statement.
        result.extend(_terms(offer["terms"], hindi))
        inherited = [row for row in offer.get("effective_terms", [])
                     if row.get("application_id") != offer["application_id"]]
        if inherited:
            result.append("Applicable company terms:" if not hindi else "Is offer par lagu company ki shartein:")
            result.extend(_terms(inherited, hindi))
        result.extend(_price(price, hindi) for price in offer["prices"])
        result.extend(_price(fee, hindi, fee=True) for fee in offer["fees"])
        if not offer["prices"]:
            result.append("Offer price not stated." if not hindi else "Offer ki keemat nahi di gayi.")
    if not supplier["product_offers"]:
        result.append("A supplier lead is not a confirmed offer for this product type." if not hindi else
                      "Supplier ka lead milna is product type ke offer ka confirmation nahi hai.")
    return result


def _price(price, hindi, *, fee=False):
    label = ("Fee" if fee else "Offer price") if not hindi else ("Fee" if fee else "Offer ki keemat")
    text = label + ": " + price["currency"] + " " + price["amount"] + "; " + price["unit"]
    for field in ("quantity_condition", "qualifier"):
        if price.get(field):
            text += "; " + price[field]
    if price.get("qualified") is not True:
        text += "; needs confirmation" if not hindi else "; confirmation chahiye"
    return text + "; " + _dated(price, hindi)


def scoped_supplier_limitation(suppliers, language):
    """Fixed caveats for G; the model cannot flatten qualified offer facts."""
    hindi = language == "hi-Latn"
    groups = [row for row in suppliers if is_scoped_supplier(row)]
    if not groups:
        return []
    clauses = ["Company details and each offer's purchase and dispatch terms are shown separately." if not hindi else
               "Company ki jankari aur har offer ki kharid aur dispatch ki shartein alag dikhayi gayi hain."]
    offers = [offer for group in groups for offer in group["product_offers"]]
    if not offers:
        clauses.append("A supplier lead is not a confirmed offer for this product type." if not hindi else
                       "Supplier ka lead is product type ke offer ka confirmation nahi hai.")
    elif any(not all(any(term["field"] == field and term["value"] == "Yes" and term["qualified"]
                        for term in offer.get("effective_terms", []))
                    for field in ("single_piece_purchase", "direct_customer_dispatch")) for offer in offers):
        clauses.append("Single-piece purchase and dispatch are not confirmed for every offer." if not hindi else
                       "Har offer ke liye single-piece kharid aur dispatch confirm nahi hai.")
    if any(offer.get("min_order") is not None and offer["min_order"] > 1 for offer in offers):
        clauses.append("A bulk quote is not a single-piece sourcing offer." if not hindi else
                       "Bulk quote ko single-piece sourcing ka offer na maanein.")
    if any(not row["current"] for group in groups for row in group["company_workflows"] + group["product_offers"]):
        clauses.append("Some supplier information is older and needs checking." if not hindi else
                       "Supplier ki kuch jankari purani hai aur dobara check karni hai.")
    return clauses
