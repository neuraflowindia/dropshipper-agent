"""Merge the Dropdash capture (API page bodies + DOM cards) into products.json and categories.json."""
import glob, html, json, os, re, sys, collections

D = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else D + '/final'
os.makedirs(OUT, exist_ok=True)
SECTIONS = {282: 'Testing Products', 288: 'Profitable Products'}
PROJECT = '/mnt/d/Coding/Business/Ideas and Trials and Forked and Misc/dropshipping-product-and-supplier-finder'
disc = json.load(open(PROJECT + '/config/research/dropship_platforms.json'))['discovery']

home = json.load(open(D + '/out/home.json', encoding='utf-8-sig'))
dash = json.loads(home['bodies'][0]['body'])['data']
categories = []
for i, c in enumerate(dash):
    cid = c['categories']['category_Id']
    categories.append({'category_id': cid, 'name': c['categories']['category_Name'].strip(), 'home_position': i + 1,
                       'kind': 'sales_section' if cid in SECTIONS else 'category',
                       'url': f'https://app.dropdash.co/products?categoryId={cid}',
                       'total_products_stated': c['sideimage']['total_products_count_in_category']})
names = {c['category_id']: c['name'] for c in categories}


def clean(text, limit):
    if not isinstance(text, str):
        return None
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit] or None


def phone_free(text):
    return re.sub(r'(?<![0-9])(?:\+?91[\s-]?)?[6-9][0-9]{9}(?![0-9])', '[phone removed]', text) if isinstance(text, str) else text


products = {}
pages = collections.defaultdict(dict)
dom_cards = collections.defaultdict(dict)
runs = {}
for path in sorted(glob.glob(D + '/out/cat-*.json')):
    run = json.load(open(path, encoding='utf-8-sig'))
    cid = int(re.search(r'cat-(\d+)', path)[1])
    runs[cid] = run
    for b in run.get('bodies') or []:
        if not b.get('body') or 'getallproducts' not in b['url']:
            continue
        j = json.loads(b['body'])
        pages[cid][j['pageNumber']] = j
        for p in j['data']:
            pid = p['productID']
            rec = products.get(pid)
            if rec is None:
                rec = products[pid] = {'raw': p, 'listed_in': [], 'captured_at': b.get('t') or run.get('finished_at')}
            if cid not in rec['listed_in']:
                rec['listed_in'].append(cid)
    for card in (run.get('dom') or {}).get('cards') or []:
        if card.get('id'):
            dom_cards[cid][int(card['id'])] = card
# Home page cards (6 per section) are already in the category lists; note any that are not.
home_ids = {p['productID']: c['categories']['category_Id'] for c in dash for p in c['products']}

records = []
mismatch = collections.Counter()
for pid, rec in products.items():
    p = rec['raw']
    card = next((dom_cards[c][pid] for c in rec['listed_in'] if pid in dom_cards[c]), None)
    if card:
        for field, api in (('price', p.get('b_2_b_price')), ('mrp', p.get('mrp')), ('stock', p.get('stock')), ('sold', p.get('orderRangeLabel'))):
            shown = card.get(field)
            if shown is None or api is None:
                continue
            try:
                same = float(str(shown).replace(',', '')) == float(api)
            except ValueError:
                same = str(shown).strip() == str(api).strip()
            if not same:
                mismatch[field] += 1
    listed = sorted(rec['listed_in'], key=lambda c: [x['category_id'] for x in categories].index(c) if c in names else 999)
    own = p.get('category_id')
    records.append({
        'product_id': pid, 'dd_id': p.get('dd_id'), 'seller_id': p.get('sellerId'), 'brand_id': p.get('brand_id'),
        'variation_id': p.get('variation_id'), 'title': clean(p.get('title'), 300),
        'price': p.get('b_2_b_price'), 'mrp': p.get('mrp'), 'discount_amount': p.get('discount_amount'),
        'units_sold_range': p.get('orderRangeLabel'), 'order_count': p.get('orderCount'),
        'stock': p.get('stock'), 'stock_available_for_buyers': p.get('stock_available_for_buyers'),
        'in_stock': bool(p.get('stock')) and p.get('stock') > 0,
        'category_id': own, 'category_name': (p.get('category_name') or '').strip() or names.get(own),
        'listed_in_category_ids': listed, 'listed_in_category_names': [names.get(c) for c in listed],
        'sections': [SECTIONS[c] for c in listed if c in SECTIONS],
        'card_badges_shown': (card or {}).get('badges'),
        'card_price_shown': (card or {}).get('price'), 'card_sold_shown': (card or {}).get('sold'),
        'image_url': (p.get('product_image') or '').strip() or (card or {}).get('image'),
        'product_url': (card or {}).get('href') or f"https://app.dropdash.co/productDetails?id={pid}&sellerId={p.get('sellerId')}",
        'has_video': any((p.get(f'productVideo{i}URL') or '').strip() for i in range(1, 5)),
        'package_weight_kg': p.get('package_weight'),
        'package_dimensions_cm': [p.get('package_length'), p.get('package_breadth'), p.get('package_height')],
        'is_active': p.get('is_active'),
        'highlights': phone_free(clean(p.get('highlights'), 600)),
        'captured_at': rec['captured_at'],
    })
records.sort(key=lambda r: (-(r['order_count'] or 0), r['product_id']))

# Rough interest grouping: config keyword lists on the title first, then the Dropdash category.
CAT_MAP = {63: 'Home & Decor', 284: 'Home & Decor', 293: 'Home & Kitchen', 285: 'Home & Kitchen', 218: 'Home (quirky essentials)',
           239: 'Baby & Kids'}
GROUP = {'Home and Kitchen': 'Home & Kitchen', 'Home and Decor': 'Home & Decor', 'Baby Products': 'Baby & Kids', 'Travel and Outdoor': 'Travel & Outdoor'}
def words(text):
    return ' ' + re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()) + ' '
def group(r):
    t = words(r['title'])
    if any(' ' + x + ' ' in t for x in disc['exclude']):
        hit_ex = True
    else:
        hit_ex = False
    for name, kws in disc['categories'].items():
        if not hit_ex and any(' ' + k + ' ' in t for k in kws):
            return GROUP[name]
    for c in r['listed_in_category_ids']:
        if c in CAT_MAP:
            return CAT_MAP[c]
    return 'Other'
for r in records:
    r['rough_group'] = group(r)

for c in categories:
    cid = c['category_id']
    run = runs.get(cid)
    pg = pages.get(cid, {})
    c['captured_products'] = len({p['productID'] for j in pg.values() for p in j['data']})
    c['api_total_records'] = next(iter(pg.values()))['totalRecords'] if pg else None
    c['api_total_pages'] = next(iter(pg.values()))['totalPages'] if pg else None
    c['pages_loaded'] = sorted(pg)
    c['dom_cards'] = len(dom_cards.get(cid, {}))
    c['capture_state'] = run.get('state') if run else 'not_captured'
    c['stop_reason'] = run.get('stop_reason') if run else None
    c['navigated_at'] = run.get('navigated_at') if run else None
    c['captured_at'] = run.get('finished_at') if run else None
    c['complete'] = bool(pg) and c['captured_products'] >= (c['api_total_records'] or 0)

json.dump(records, open(OUT + '/products.json', 'w'), ensure_ascii=False, indent=1)
json.dump(categories, open(OUT + '/categories.json', 'w'), ensure_ascii=False, indent=1)
stats = {'unique_products': len(records), 'listings': sum(len(r['listed_in_category_ids']) for r in records),
         'home_cards_missing_from_lists': [pid for pid in home_ids if pid not in products],
         'dom_api_mismatch': dict(mismatch),
         'per_category': [(c['category_id'], c['name'], c['total_products_stated'], c['api_total_records'], c['captured_products'], c['dom_cards'], c['complete'], c['stop_reason']) for c in categories],
         'units_sold': collections.Counter(r['units_sold_range'] for r in records).most_common(),
         'in_stock': sum(r['in_stock'] for r in records),
         'rough_group': collections.Counter(r['rough_group'] for r in records).most_common(),
         'own_category': collections.Counter(r['category_name'] for r in records).most_common(),
         'multi_listed': sum(len(r['listed_in_category_ids']) > 1 for r in records),
         'with_video': sum(r['has_video'] for r in records)}
json.dump(stats, open(D + '/stats.json', 'w'), ensure_ascii=False, indent=1)
print(json.dumps(stats, ensure_ascii=False, indent=1))
