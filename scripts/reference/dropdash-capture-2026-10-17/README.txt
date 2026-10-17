Reference only (not part of the program): the read-only scripts that captured Dropdash's full
product list on 17 October 2026 (5,189 products, 21 categories, signed in throughout).
Plan R19 task 4.2 ports their approach into platform_signed_in.py.

cdp.ps1   Windows PowerShell: launch the research Chrome profile with remote debugging, run one
          page job (navigate, wait, scroll the list's own scroll box, collect cards) and keep the
          response bodies of Dropdash's product-list requests (api.dropdash.co getallproducts,
          get_categories_new_dashboard). Never reads cookies, storage, headers or tokens; strips
          the greeting name and phone-like numbers before writing.
dd.js     Page functions: signed-in/blocked state, card collection, scroll with an up-then-down
          nudge (needed when the first 18 cards fit the screen), final DOM snapshot.
driver.py Paced loop over the categories (>= 35 s between pages, 4-8 s between scroll loads).
merge.py  Joins API bodies and cards into products.json and categories.json.
