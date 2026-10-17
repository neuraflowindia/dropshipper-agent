# R19 fixtures: trimmed real pages of 16–17 October 2026

Copied on 17 October 2026 (Plan R19, task 0.3) from the live research folders under
`/home/anan/neuraflow-dropshipper-data/data/research/captured/`, before the fresh start archived them.
They feed the new reader adapters (task 4.3), the store intake (task 2.1), the Dropdash full read (task 4.2) and
the points and ranking tests (task 2.3). Tests never open a live site.

**Scrubbing, the same for every file.** Only the files a parser reads were copied (no browser scripts, process
or profile files, request lists or transport logs). E-mail addresses became `user@example.invalid`; every Indian
mobile or landline number became a fake one starting with `5` or `05` (the same real number always gets the same
fake, so ids stay distinct); user folder names in saved paths became `user`; values of cookie, token, session and
CSRF fields (JSON keys, `<meta>`/`<input>` fields, hidden form values and Amazon's session id) were removed. The
research account's own name and e-mail had already been replaced by `redacted` when the pages were saved (Google
Trends and IndiaMART); none was found anywhere else. Pages over 150 KB are stored gzip-compressed (`*.gz`).

**Checked against the old parsers.** For every page fixture the old engine's own parser was run on the original
and on the scrubbed copy; the answers were identical in every case. That answer is saved beside the page as
`expected-*.json` (or, for Trends, the saved `interpretation.json` and `rising_queries.json`).

- `amazon/` — three Amazon.in search pages (`amazon-search`): `01-read` (memory foam kitchen mat, 53 cards, "over 1,000
  results"), `02-read` (baby care kit, 48 cards, "over 10,000 results") and `03-no_cards` (dining table runner, nothing
  readable). Each has `request.json`, `rendered.html` (scripts, styles and SVG removed), `snapshot.json`
  (the page text) and `expected-cards.json` (`marketplace_cards.parse_saved_cards`). Amazon was not signed in.
- `flipkart/` — three Flipkart search pages in the same layout: `01-read` (Baby Care Products Kit, 38 cards of 8,556), `02-read`
  (microfiber kitchen towel, 39 cards of 32,692) and `03-no_cards` (GWD cot mobile).
- `facebook/` — four Facebook Ad Library India searches: 30, 4 and 26 visible ads, and one "insufficient" page
  with none. Each has `request.json` (with the page's HTTP status), `snapshot.json` (cards and text) and
  `expected-records.json` (`meta_browser.parse_meta_snapshot`).
- `trends/` — three Google Trends explore reads (India, past 12 months): two with the downloaded chart
  (`multiTimeline.csv`, kept byte-for-byte so its hash matches) and a "Rising" related-searches list, and one with
  "not enough data" (with its trimmed `rendered.html`). `interpretation.json` is the saved CSV parse
  (`providers.trends_browser.parse_trends_csv`), `rising_queries.json` the saved Rising list
  (`trends_rising.parse_rising_queries` over `scope.json`); `receipt-summary.json` keeps the outcome only.
- `tradeindia/` — four TradeIndia searches: 10, 10 and 1 result cards, and one whose layout the parser did not
  recognise (`extraction_pending`). `receipt.json` is the saved page receipt (HTML trimmed of scripts),
  `request.json` adds the route and the request item, `expected-parsed.json` is
  `supplier_route_acquisition.parse_supplier_route_receipt`.
- `indiamart/` — four signed-in IndiaMART searches in the same layout: three with 10 result cards and one that
  hit the sign-in wall (`blocked`, `OWNER_SIGN_IN_REQUIRED`). Scripts were kept because the parser reads the
  page data in them; the visitor object was already `redacted` at save time.
- `quick-delivery/` — three Quick Commerce API replies for "compartment travel organizer" in Mumbai: Blinkit
  (5 products), Instamart (50) and Zepto (none). `capture.json` keeps the provider's reply (`raw_response`) and
  the old parse beside it (`products`, equal to `quickcommerce.map_response` on that reply); no API key, request id or
  headers.
- `dropdash-2026-10-17/` — a slice of the 17 October signed-in Dropdash full read: `categories.json` (all 21
  home-page categories and sales sections with their stated totals, unchanged), `products.json` (the 200
  products listed in five complete categories: Testing Products 32, Pet Care 52, Religious & Spirituals 58, Monsoon
  Arrivals 16, Jewellery 42 — each equal to its stated total) and `api-pages.json` (the app's own 18 product-list
  replies for those categories plus its category reply; product data only, never headers, cookies or tokens; 36
  seller "manufacturer details" that held a person's name now read `Seller`). No greeting or account name was in
  the capture.
- `stores/` — frozen 200-item slices of the 16 October store catalogues for task 2.1:
  `deodap-catalogue-slice.json` (of 13,527 items; 42 of 806 collections) and `baapstore-catalogue-slice.json`
  (of 6,053 items). Items were chosen by id order: up to 60 on a store list (best-sellers, trending), up to 40
  created since 17 August 2026 ("new in 60 days"), up to 20 out of stock, the rest evenly spread. The catalogue
  header fields are unchanged; only the collections the chosen items belong to are kept.
- `types-2026-10-16.json` — the 34 product types the old engine made on 16 October with their known facts
  (name, origin, store matches and lists, checklist answers, Facebook ads and dates, Trends charts, Amazon/Flipkart
  crowding and reviewed listings, quick-delivery listings, supplier leads, and the retired score for comparison),
  exported read-only from the old event store through the research trace loader. `_notes` lists the gaps;
  `_discussion_paper_examples` copies the 17 October discussion paper's three worked examples (duster 36.5, water
  bottle 40.5, baby lotion 10.5) verbatim.
