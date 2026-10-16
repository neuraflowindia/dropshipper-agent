"""Sample data for the report redesign mock-up (16 October 2026).

EVERYTHING HERE IS INVENTED SAMPLE DATA FOR A DESIGN MOCK-UP. It is not
research. Supplier names are fictional. Images are NeuraFlow-owned
illustrative still lifes from the August 2026 preview, not product photos.

The numbers obey the real scoring rules in config/research/opportunity_score.json
(opportunity-score-settings-v1) so the visuals show believable combinations:
  Want    = Facebook ads {0,2,6,8} + Google searches {0,2,5} + platform lists {0,1,2,4,5} + quick commerce {0,1,2}
  Early   = rising searches {0,8,12} + new advertisers {0,3,6,9} + new arrivals {0,6,9}
  Room    = crowding Low 25 / Medium 12 / High 0
  Product = 10 + 3 x (Meets - 5)        (5 Meets = Decent fit, 6-10 = Good fit)
  Label   = High crowding -> Proven but crowded; Early >= 15 and Want < 10 -> Early bet; else Sweet spot
  Order   = Low/Medium first by score (then Early, then Product), High crowding last
"""

ISSUE = {
    "date": "2026-10-20", "weekday_en": "Tuesday", "weekday_hi": "Mangalvaar",
    "research": ("2026-10-12", "2026-10-19"),
    "counts": {"products": 12, "new": 8, "updates": 4},
    "title": {"en": "Products worth testing this week", "hi-Latn": "Is hafte test karne layak products"},
}

PARTS = ("want", "early", "room", "product")
MAXIMA = {"want": 20, "early": 30, "room": 25, "product": 25}
ITEMS = {"want": ("facebook_ads", "google_searches", "platform_lists", "quick_commerce"),
         "early": ("rising_searches", "new_advertisers", "new_arrivals"),
         "room": ("crowding",), "product": ("checklist",)}

# rank, key, title, category, status, item points, crowding, meets, compliance, photo
ROWS = [
    (1, "plant", "Self-watering plant spike set", "Home and Decor", "New",
     {"facebook_ads": 8, "google_searches": 5, "platform_lists": 4, "quick_commerce": 0,
      "rising_searches": 8, "new_advertisers": 3, "new_arrivals": 6}, "low", 8, False, "plant-spike-set"),
    (2, "strainer", "Clip-on sink strainer basket", "Home and Kitchen", "New",
     {"facebook_ads": 6, "google_searches": 5, "platform_lists": 4, "quick_commerce": 0,
      "rising_searches": 8, "new_advertisers": 6, "new_arrivals": 0}, "low", 9, False, "sink-strainer-basket"),
    (3, "stroller", "Stroller organiser bag", "Baby Products", "Updated",
     {"facebook_ads": 6, "google_searches": 5, "platform_lists": 2, "quick_commerce": 0,
      "rising_searches": 8, "new_advertisers": 3, "new_arrivals": 6}, "low", 7, True, "stroller-organiser-bag"),
    (4, "cablock", "Adjustable baby-proof cabinet lock set", "Baby Products", "New",
     {"facebook_ads": 6, "google_searches": 2, "platform_lists": 1, "quick_commerce": 0,
      "rising_searches": 12, "new_advertisers": 3, "new_arrivals": 0}, "low", 7, True, None),
    (5, "lamp", "Pleated mushroom table lamp", "Home and Decor", "New",
     {"facebook_ads": 6, "google_searches": 5, "platform_lists": 2, "quick_commerce": 0,
      "rising_searches": 8, "new_advertisers": 6, "new_arrivals": 0}, "low", 6, True, "mushroom-table-lamp"),
    (6, "oilspray", "Glass oil spray bottle", "Home and Kitchen", "Updated",
     {"facebook_ads": 8, "google_searches": 5, "platform_lists": 5, "quick_commerce": 2,
      "rising_searches": 8, "new_advertisers": 3, "new_arrivals": 0}, "medium", 9, False, "oil-spray-bottle"),
    (7, "cubes", "Compression packing cube set", "Travel and Outdoor", "New",
     {"facebook_ads": 6, "google_searches": 5, "platform_lists": 0, "quick_commerce": 0,
      "rising_searches": 8, "new_advertisers": 6, "new_arrivals": 6}, "medium", 8, False, None),
    (8, "piclight", "Rechargeable cordless picture light", "Home and Decor", "New",
     {"facebook_ads": 8, "google_searches": 5, "platform_lists": 5, "quick_commerce": 0,
      "rising_searches": 0, "new_advertisers": 3, "new_arrivals": 6}, "medium", 8, True, None),
    (9, "feeding", "Silicone suction feeding set", "Baby Products", "Updated",
     {"facebook_ads": 2, "google_searches": 5, "platform_lists": 1, "quick_commerce": 0,
      "rising_searches": 12, "new_advertisers": 3, "new_arrivals": 6}, "medium", 7, True, "suction-feeding-set"),
    (10, "pillow", "Hooded travel neck pillow", "Travel and Outdoor", "New",
     {"facebook_ads": 2, "google_searches": 5, "platform_lists": 0, "quick_commerce": 0,
      "rising_searches": 12, "new_advertisers": 0, "new_arrivals": 6}, "medium", 6, False, None),
    (11, "scale", "Digital kitchen weighing scale", "Home and Kitchen", "Updated",
     {"facebook_ads": 8, "google_searches": 5, "platform_lists": 4, "quick_commerce": 1,
      "rising_searches": 0, "new_advertisers": 6, "new_arrivals": 0}, "high", 6, True, None),
    (12, "chopper", "Manual pull-cord vegetable chopper", "Home and Kitchen", "New",
     {"facebook_ads": 8, "google_searches": 5, "platform_lists": 5, "quick_commerce": 2,
      "rising_searches": 0, "new_advertisers": 3, "new_arrivals": 0}, "high", 5, False, None),
]


def score_parts(items, crowd, meets):
    room = {"low": 25, "medium": 12, "high": 0}[crowd]
    product = 10 + 3 * (meets - 5)
    parts = {"want": sum(items[k] for k in ITEMS["want"]), "early": sum(items[k] for k in ITEMS["early"]),
             "room": room, "product": product}
    return parts


def label_for(parts, crowd):
    if crowd == "high":
        return "proven_but_crowded"
    if parts["early"] >= 15 and parts["want"] < 10:
        return "early_bet"
    return "sweet_spot"


def products():
    rows = []
    for rank, key, title, category, status, items, crowd, meets, compliance, photo in ROWS:
        parts = score_parts(items, crowd, meets)
        assert parts["want"] >= 5, title
        rows.append({"rank": rank, "key": key, "title": title, "category": category, "status": status,
                     "items": dict(items, crowding={"low": 25, "medium": 12, "high": 0}[crowd],
                                   checklist=parts["product"]),
                     "parts": parts, "score": sum(parts.values()), "label": label_for(parts, crowd),
                     "crowd": crowd, "meets": meets, "compliance": compliance, "photo": photo})
    order = sorted(rows, key=lambda r: (r["crowd"] == "high", -r["score"], -r["parts"]["early"], -r["parts"]["product"]))
    assert [r["rank"] for r in order] == list(range(1, 13)), [(r["rank"], r["score"]) for r in order]
    return rows


def _series(values):
    """52 weekly points, rescaled the way Google Trends does it: the busiest week = 100."""
    assert len(values) == 52, len(values)
    top = max(values)
    return [round(v * 100 / top) for v in values]


def window_bounds(series):
    """Previous and recent four-week averages as the small ranges the report shows."""
    def bounds(chunk):
        mean = sum(chunk) / 4
        return (int(mean) - 2, int(mean) + 3)
    return bounds(series[-8:-4]), bounds(series[-4:])


# --- Detailed spreads (three products) -----------------------------------------------------------

CRITERIA = {1: "Small in size", 2: "Easy to ship", 5: "Improves confidence", 6: "Improves convenience",
            7: "Saves people time", 8: "Solves a problem or fills a gap", 9: "Saves people money",
            11: "Improves quality of life", 12: "High perceived value", 13: "Woman-dominated audience"}

SPREADS = {
    "plant": {
        "criteria": [
            (1, "Meets", False, "Each spike is palm-sized and a set packs into a small box.",
             "Har spike hatheli jitna hai aur poora set chhote dibbe mein aa jata hai."),
            (2, "Meets", False, "Light plastic parts, with no liquid or glass in the box.",
             "Halke plastic ke hisse, dibbe mein na liquid na kaanch."),
            (5, "Unclear", False, "No clear link to how the buyer feels about themselves.",
             "Kharidar ke aatmavishwas se seedha rishta saaf nahi hai."),
            (6, "Meets", False, "Plants get water for days without daily watering.",
             "Roz paani diye bina paudhon ko kai din tak paani milta hai."),
            (7, "Meets", False, "Cuts the daily watering round, especially for balcony pots.",
             "Roz ka paani dene ka kaam kam hota hai, khaaskar balcony ke gamlon mein."),
            (8, "Meets", False, "Keeps plants alive during short trips away from home.",
             "Ghar se chhoti yatra ke dauran bhi paudhe zinda rehte hain."),
            (9, "Meets", True, "May avoid replacing plants that dry out; not measured.",
             "Sookhe paudhe dobara khareedne se bacha sakta hai; naapa nahi gaya."),
            (11, "Meets", False, "Makes keeping indoor plants easier for busy households.",
             "Vyast gharon ke liye indoor paudhe rakhna aasaan banata hai."),
            (12, "Meets", False, "Looks like a clever gadget for a modest price.",
             "Kam keemat mein ek samajhdaar gadget jaisa lagta hai."),
            (13, "Does not meet", False, "Home-plant buyers are mixed; no clear lean in the evidence.",
             "Ghar ke paudhon ke kharidar mile-jule hain; saboot mein koi saaf jhukaav nahi."),
        ],
        "why": {"en": "Self-watering spikes let an upturned plastic bottle feed a pot slowly for several days. They suit balcony and indoor gardeners in cities who travel or forget to water. Amazon and Flipkart carry only a few similar listings, which leaves room for a fresh listing, while steady Meta ads show sellers are already testing it.",
                "hi-Latn": "Self-watering spike ulte rakhe plastic bottle se gamle ko kai din tak dheere-dheere paani dete hain. Yeh shehar ke balcony aur indoor gardeners ke liye theek hain jo yatra karte hain ya paani dena bhool jaate hain. Amazon aur Flipkart par milti-julti listings kam hain, isliye nayi listing ke liye jagah hai, aur Meta par lagaataar ads dikhate hain ki seller ise pehle se test kar rahe hain."},
        "points": {"en": ["Several advertisers run Meta ads for it, and one started within the last month.",
                          "Google searches in India have risen over recent weeks.",
                          "Dropdash shows it on a trending list and DeoDap lists it as a new arrival."],
                   "hi-Latn": ["Kai advertisers Meta par iske ads chala rahe hain, aur ek ne pichhle mahine shuru kiya.",
                               "India mein Google search haal ke hafton mein badhi hai.",
                               "Dropdash ise trending list mein dikhata hai aur DeoDap ise new arrival mein."]},
        "limitation": {"en": "Results depend on soil and pot size, so buyers may need clear instructions. Single-piece dispatch is not confirmed for every supplier lead.",
                       "hi-Latn": "Nateeja mitti aur gamle ke size par nirbhar karta hai, isliye kharidar ko saaf nirdesh chahiye ho sakte hain. Har supplier lead ke liye ek-piece dispatch pakka nahi hai."},
        "update": None,
        "compliance": None,
        "crowd_facts": {"amazon": 6, "flipkart": 3, "brands": 4, "no_brand": 1, "max_ratings": 212, "bestseller": 0},
        "meta": {"observed": "2026-10-15", "advertisers": 7, "relevant": 19, "active": 12,
                 "starts": ["2026-03-14", "2026-05-02", "2026-06-20", "2026-07-08", "2026-08-27", "2026-09-11", "2026-10-02"]},
        "trends": {"state": "rising", "observed": "2026-10-14",
                   "series": _series([30, 28, 31, 33, 29, 27, 30, 32, 34, 31, 29, 33, 35, 32, 30, 34, 36, 33, 31, 35,
                                      37, 34, 36, 38, 35, 33, 37, 39, 41, 38, 36, 40, 42, 39, 41, 43, 40, 44, 42, 45,
                                      43, 46, 44, 47, 45, 46, 48, 45, 61, 64, 68, 70]),
                   "previous": None, "recent": None},
        "platforms": [("DeoDap", "listed", ["New arrival"], 119), ("BaapStore", "not_found", [], None),
                      ("Dropdash", "listed", ["Trending"], 134)],
        "qc_cities": ("Delhi NCR", "Mumbai", "Bengaluru"),
        "qc": {"Blinkit": ("no", "no", "no"), "Zepto": ("no", "no", "no"), "Instamart": ("no", "no", "unchecked")},
        "prices": {"retail": [(299, "pack of 4", "Amazon India", "16 Oct"), (349, "pack of 6", "Flipkart", "17 Oct")],
                   "supplier": [(134, "per set of 4, single piece", "Dropdash dropship price", "15 Oct"),
                                (38, "per piece, minimum order 100 pieces", "IndiaMART quote", "14 Oct")]},
        "bulk": {"en": "The IndiaMART quote is a bulk quote (minimum 100 pieces). It is not a one-piece dispatch offer.",
                 "hi-Latn": "IndiaMART ka quote thok (bulk) ka hai: kam se kam 100 piece. Yeh ek-piece dispatch offer nahi hai."},
        "suppliers": [
            {"name": "Hariyali Loop Garden Supplies", "place": "Pune, Maharashtra", "via": "IndiaMART",
             "moq": "100 pieces", "terms": {"dispatch": "unknown", "cod": "unknown", "plain": "yes", "label": "yes", "fees": "no"}},
            {"name": "Dropdash catalogue", "place": "City not stated", "via": "Dropdash",
             "moq": "1 piece", "terms": {"dispatch": "yes", "cod": "yes", "plain": "unknown", "label": "no", "fees": "yes"}},
            {"name": "Nimbus Polycraft", "place": "Ahmedabad, Gujarat", "via": "Google Maps",
             "moq": "Not stated", "terms": {"dispatch": "unknown", "cod": "unknown", "plain": "unknown", "label": "yes", "fees": "unknown"}},
        ],
        "examples": [("Plant watering spikes, set of 4", "Amazon India", "Seller not stated", "Pack of 4", "₹299", "16 Oct"),
                     ("Self-watering spike for bottles", "Flipkart", "GreenRoot Store", "Pack of 6", "₹349", "17 Oct"),
                     ("Ceramic-tip drip waterer", "DeoDap", "DeoDap", "Pack of 2", "₹119", "13 Oct")],
        "sources": [("Facebook Ads Library", "15 Oct"), ("Google Trends India", "14 Oct"), ("Amazon India search", "16 Oct"),
                    ("Flipkart search", "17 Oct"), ("DeoDap", "13 Oct"), ("BaapStore", "13 Oct"), ("Dropdash", "15 Oct"),
                    ("Blinkit, Zepto, Instamart", "18 Oct"), ("IndiaMART", "14 Oct"), ("Google Maps supplier search", "14 Oct")],
    },
    "feeding": {
        "criteria": [
            (1, "Meets", False, "Bowl, plate and spoon are small and light.",
             "Bowl, plate aur chammach chhote aur halke hain."),
            (2, "Meets", False, "Silicone does not break, so shipping is simple.",
             "Silicone tootta nahi, isliye shipping aasaan hai."),
            (5, "Unclear", False, "No clear link to how the buyer feels about themselves.",
             "Kharidar ke aatmavishwas se seedha rishta saaf nahi hai."),
            (6, "Meets", False, "The suction base keeps the bowl fixed on the table.",
             "Suction base bowl ko table par tika kar rakhta hai."),
            (7, "Meets", False, "Less cleaning up after spills.",
             "Girne aur phailne ke baad safai kam karni padti hai."),
            (8, "Meets", False, "Toddlers cannot easily push the bowl off the table.",
             "Chhote bachche bowl ko aasani se table se gira nahi paate."),
            (9, "Does not meet", False, "No clear saving compared with an ordinary bowl.",
             "Saadharan bowl ke mukable koi saaf bachat nahi dikhti."),
            (11, "Unclear", False, "Helpful, but the difference is small.",
             "Madad karta hai, par fark chhota hai."),
            (12, "Meets", False, "A bowl, plate and spoon set looks good value.",
             "Bowl, plate aur chammach ka set achhi keemat jaisa lagta hai."),
            (13, "Meets", True, "Baby feeding products are mostly bought by mothers; judged from listing photos and reviews.",
             "Baby feeding ke products zyaadatar maayein khareedti hain; listing ki tasveeron aur reviews se andaaza."),
        ],
        "why": {"en": "A silicone feeding set with a suction base that grips the table, so toddlers cannot easily push the bowl away. Interest is new rather than proven: Google searches in India have climbed sharply and BaapStore has just added it as a new arrival, but only a few advertisers run Meta ads so far.",
                "hi-Latn": "Silicone ka feeding set jiska suction base table ko pakad leta hai, taaki chhote bachche bowl ko aasani se door na dhakel sakein. Dilchaspi nayi hai, abhi saabit nahi: India mein Google search tezi se badhi hai aur BaapStore ne ise abhi new arrival mein joda hai, lekin abhi kuch hi advertisers Meta par iske ads chala rahe hain."},
        "points": {"en": ["Searches for it in India rose strongly over recent weeks.",
                          "BaapStore shows it on its new-arrivals list.",
                          "Amazon and Flipkart have a moderate number of similar listings, so some room remains."],
                   "hi-Latn": ["India mein iski search haal ke hafton mein tezi se badhi hai.",
                               "BaapStore ise apni new-arrivals list mein dikha raha hai.",
                               "Amazon aur Flipkart par milti-julti listings madhyam sankhya mein hain, isliye kuch jagah bachi hai."]},
        "limitation": {"en": "Few advertisers so far, so demand is early and unproven. Baby feeding items need the client’s own check of material and safety claims before any listing.",
                       "hi-Latn": "Abhi advertisers kam hain, isliye maang shuruaati hai aur saabit nahi hui. Baby ke khane wale items ke material aur safety daavon ko listing se pehle client khud check kare."},
        "update": {"en": "BaapStore now lists it as a new arrival, and Google searches have started rising.",
                   "hi-Latn": "BaapStore ab ise new arrival ke roop mein dikha raha hai, aur Google search badhne lagi hai."},
        "compliance": {"en": "Baby feeding item that touches food: check food-grade material and child-safety labelling rules before selling.",
                       "hi-Latn": "Baby ke khane se judi cheez hai: bechne se pehle food-grade material aur child-safety labelling ke niyam khud check karein."},
        "crowd_facts": {"amazon": 11, "flipkart": 7, "brands": 9, "no_brand": 0, "max_ratings": 1846, "bestseller": 1},
        "meta": {"observed": "2026-10-15", "advertisers": 2, "relevant": 3, "active": 2,
                 "starts": ["2026-08-05", "2026-09-29"]},
        "trends": {"state": "rising_strongly", "observed": "2026-10-14",
                   "series": _series([12, 10, 0, 8, 11, 9, 0, 10, 13, 12, 9, 11, 14, 12, 10, 0, 13, 15, 12, 11,
                                      14, 16, 13, 12, 15, 17, 14, 13, 16, 18, 15, 17, 19, 16, 18, 20, 17, 19, 21, 18,
                                      22, 20, 23, 21, 24, 22, 25, 27, 44, 49, 55, 58]),
                   "previous": None, "recent": None},
        "platforms": [("DeoDap", "not_found", [], None), ("BaapStore", "listed", ["New arrival"], 289),
                      ("Dropdash", "not_found", [], None)],
        "qc_cities": ("Delhi NCR", "Mumbai", "Bengaluru"),
        "qc": {"Blinkit": ("no", "no", "no"), "Zepto": ("no", "no", "unchecked"), "Instamart": ("no", "unchecked", "no")},
        "prices": {"retail": [(449, "set of 3 pieces", "Amazon India", "16 Oct"), (399, "set of 3 pieces", "Flipkart", "17 Oct")],
                   "supplier": [(289, "per set, single piece", "BaapStore dropship price", "13 Oct"),
                                (165, "per set, minimum order 50 sets", "IndiaMART quote", "14 Oct")]},
        "bulk": {"en": "The IndiaMART quote is a bulk quote (minimum 50 sets). It is not a one-piece dispatch offer.",
                 "hi-Latn": "IndiaMART ka quote thok (bulk) ka hai: kam se kam 50 set. Yeh ek-piece dispatch offer nahi hai."},
        "suppliers": [
            {"name": "Little Spoon Silicone Works", "place": "Mumbai, Maharashtra", "via": "IndiaMART",
             "moq": "50 sets", "terms": {"dispatch": "no", "cod": "unknown", "plain": "unknown", "label": "yes", "fees": "no"}},
            {"name": "BaapStore catalogue", "place": "City not stated", "via": "BaapStore",
             "moq": "1 set", "terms": {"dispatch": "yes", "cod": "unknown", "plain": "unknown", "label": "unknown", "fees": "yes"}},
        ],
        "examples": [("Suction bowl, plate and spoon set", "Amazon India", "Seller not stated", "Set of 3", "₹449", "16 Oct"),
                     ("Silicone toddler feeding set", "Flipkart", "TinyNest", "Set of 3", "₹399", "17 Oct")],
        "sources": [("Facebook Ads Library", "15 Oct"), ("Google Trends India", "14 Oct"), ("Amazon India search", "16 Oct"),
                    ("Flipkart search", "17 Oct"), ("DeoDap", "13 Oct"), ("BaapStore", "13 Oct"), ("Dropdash", "15 Oct"),
                    ("Blinkit, Zepto, Instamart", "18 Oct"), ("IndiaMART", "14 Oct")],
    },
    "chopper": {
        "criteria": [
            (1, "Meets", False, "Fits in a kitchen drawer.", "Kitchen ki daraaz mein aa jata hai."),
            (2, "Meets", False, "Plastic body with covered blades; no glass.", "Plastic body aur dhake hue blade; kaanch nahi."),
            (5, "Does not meet", False, "A routine kitchen tool with no link to confidence.",
             "Roz ka kitchen tool, aatmavishwas se koi rishta nahi."),
            (6, "Meets", False, "Chops onion, garlic and herbs without electricity.",
             "Bina bijli ke pyaaz, lahsun aur hara dhaniya kaat-ta hai."),
            (7, "Meets", False, "Faster than chopping by knife for small amounts.",
             "Thodi matra ke liye chaaku se kaatne se tez."),
            (8, "Meets", False, "Avoids teary eyes and uneven chopping.",
             "Aankhon mein aansu aur ulti-seedhi kataai se bachata hai."),
            (9, "Does not meet", False, "No clear saving compared with a knife.", "Chaaku ke mukable koi saaf bachat nahi."),
            (11, "Unclear", False, "Small everyday help; the gain is modest.", "Roz ki chhoti madad; fayda thoda hai."),
            (12, "Does not meet", False, "Many similar choppers at low prices make it look ordinary.",
             "Kam keemat wale bahut se milte-julte chopper ise aam bana dete hain."),
            (13, "Unclear", True, "Kitchen tools lean towards women buyers, but the evidence is thin.",
             "Kitchen tools ki khareed mahilaon ki taraf jhukti hai, par saboot kam hai."),
        ],
        "why": {"en": "A pull-cord chopper that minces onion, garlic and herbs by hand, without electricity. Demand is clearly proven: many advertisers, steady searches and bestseller places on two dropshipping platforms. The catch is crowding: Amazon and Flipkart already carry a very large number of similar listings from many brands.",
                "hi-Latn": "Pull-cord chopper jo bina bijli ke haath se pyaaz, lahsun aur hara dhaniya baareek kaat-ta hai. Maang saaf saabit hai: kai advertisers, lagaataar search aur do dropshipping platforms par bestseller jagah. Mushkil bheed ki hai: Amazon aur Flipkart par pehle se bahut saare brands ki milti-julti listings hain."},
        "points": {"en": ["Many advertisers run Meta ads for it, and have done so for months.",
                          "DeoDap and Dropdash both show it on bestseller lists.",
                          "It is in stock on quick-commerce apps in every city checked."],
                   "hi-Latn": ["Kai advertisers mahino se Meta par iske ads chala rahe hain.",
                               "DeoDap aur Dropdash dono ise bestseller list mein dikhate hain.",
                               "Check kiye gaye har shehar mein yeh quick-commerce apps par stock mein hai."]},
        "limitation": {"en": "Very crowded on Amazon and Flipkart, so a new listing would compete with many established sellers on price and reviews.",
                       "hi-Latn": "Amazon aur Flipkart par bahut bheed hai, isliye nayi listing ko keemat aur reviews par kai jame hue sellers se muqabla karna hoga."},
        "update": None,
        "compliance": None,
        "crowd_facts": {"amazon": 48, "flipkart": 31, "brands": 26, "no_brand": 5, "max_ratings": 38412, "bestseller": 4},
        "meta": {"observed": "2026-10-15", "advertisers": 12, "relevant": 41, "active": 29,
                 "starts": ["2025-11-08", "2025-12-19", "2026-01-22", "2026-02-03", "2026-03-15", "2026-04-09",
                            "2026-05-21", "2026-06-02", "2026-07-14", "2026-08-08", "2026-09-03", "2026-10-06"]},
        "trends": {"state": "steady", "observed": "2026-10-14",
                   "series": _series([72, 70, 74, 71, 69, 73, 75, 72, 70, 68, 71, 74, 76, 73, 71, 69, 72, 75, 77, 74,
                                      72, 70, 73, 76, 78, 75, 73, 71, 74, 77, 79, 76, 74, 72, 75, 78, 76, 73, 71, 74,
                                      72, 70, 73, 71, 72, 74, 71, 73, 70, 72, 69, 71]),
                   "previous": None, "recent": None},
        "platforms": [("DeoDap", "listed", ["Bestseller"], 139), ("BaapStore", "listed", [], 149),
                      ("Dropdash", "listed", ["Bestseller"], 145)],
        "qc_cities": ("Delhi NCR", "Mumbai", "Bengaluru"),
        "qc": {"Blinkit": ("yes", "yes", "yes"), "Zepto": ("no", "yes", "yes"), "Instamart": ("unchecked", "unchecked", "yes")},
        "prices": {"retail": [(199, "1 piece", "Amazon India", "16 Oct"), (179, "1 piece", "Flipkart", "17 Oct"),
                              (249, "1 piece", "Blinkit, Mumbai", "18 Oct")],
                   "supplier": [(139, "per piece, single piece", "DeoDap dropship price", "13 Oct"),
                                (62, "per piece, minimum order 200 pieces", "IndiaMART quote", "14 Oct")]},
        "bulk": {"en": "The IndiaMART quote is a bulk quote (minimum 200 pieces). It is not a one-piece dispatch offer.",
                 "hi-Latn": "IndiaMART ka quote thok (bulk) ka hai: kam se kam 200 piece. Yeh ek-piece dispatch offer nahi hai."},
        "suppliers": [
            {"name": "Rasoi Tools Wholesale", "place": "Rajkot, Gujarat", "via": "IndiaMART",
             "moq": "200 pieces", "terms": {"dispatch": "unknown", "cod": "unknown", "plain": "yes", "label": "yes", "fees": "no"}},
        ],
        "examples": [("Pull chopper, 500 ml", "Amazon India", "Seller not stated", "1 piece", "₹199", "16 Oct"),
                     ("Handy mini chopper with 3 blades", "Flipkart", "KitchenKart", "1 piece", "₹179", "17 Oct"),
                     ("Manual chopper", "Blinkit (Mumbai)", "Seller not stated", "1 piece", "₹249", "18 Oct")],
        "sources": [("Facebook Ads Library", "15 Oct"), ("Google Trends India", "14 Oct"), ("Amazon India search", "16 Oct"),
                    ("Amazon India Bestsellers", "16 Oct"), ("Flipkart search", "17 Oct"), ("DeoDap", "13 Oct"),
                    ("BaapStore", "13 Oct"), ("Dropdash", "15 Oct"), ("Blinkit, Zepto, Instamart", "18 Oct"), ("IndiaMART", "14 Oct")],
    },
}


for _spread in SPREADS.values():
    _spread["trends"]["previous"], _spread["trends"]["recent"] = window_bounds(_spread["trends"]["series"])
