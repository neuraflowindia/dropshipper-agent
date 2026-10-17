var PICK = {card: '.dd_id_card', card_title: '.card-price', card_price: '.new-price, .new-price-details',
  card_mrp: '.old-price, .old-price-details', card_stock: '.instock-badge', card_sold: '.inventory-badge',
  section: '.category-section', section_title: '.category-card h6', category: '#category-carousel .img-body'};
var PRODUCT = /productDetails\?id=([1-9][0-9]{0,8})(?:&sellerId=([1-9][0-9]{0,8}))?/;
function ddNorm(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
function ddAll(root, sel) { try { return root && sel ? Array.from(root.querySelectorAll(sel)) : []; } catch (e) { return []; } }
function ddField(root, sel, limit) { var n = ddAll(root, sel)[0]; var v = n ? ddNorm(n.innerText) : ''; return v && v.length <= limit ? v : null; }
function ddState() {
  var text = (document.body && document.body.innerText) || '';
  var head = text.slice(0, 600);
  var m = head.match(/(?:^|\n)[ \t ]*Welcome,[ \t ]*\n?[ \t ]*([^\n]{1,80})/);
  var path = location.pathname.toLowerCase();
  return {url: location.href, title: document.title.slice(0, 200), greeted: !!m, who: m ? m[1].trim() : null,
    signin: !!document.querySelector('input[type="password"]') || /\/(login|signin|sign-in|auth)\b/.test(path) ||
      /\b(?:log ?in|sign ?in)\b[\s\S]{0,80}\b(?:password|otp|continue with google)\b/i.test(text.slice(0, 3000)),
    blocked: /too many requests|captcha|unusual activity|access denied|rate limit/i.test(text.slice(0, 4000)),
    flags: {cards: document.querySelectorAll(PICK.card).length, links: document.querySelectorAll('a[href*="productDetails"]').length,
      height: document.documentElement.scrollHeight}};
}
function ddCard(a) {
  var box = null; try { box = a.closest(PICK.card); } catch (e) {}
  box = box || a.closest('[class*="card"]') || a;
  var img = box.querySelector('img[src^="http"]');
  var heading = ddAll(box, PICK.card_title)[0];
  var section = null; try { section = box.closest(PICK.section); } catch (e) {}
  var m = a.href.match(PRODUCT);
  return {href: a.href, id: m ? m[1] : null, seller_id: m && m[2] ? m[2] : null,
    title: heading ? ddNorm(heading.innerText).slice(0, 300) : null,
    price: ddField(box, PICK.card_price, 40), mrp: ddField(box, PICK.card_mrp, 40),
    stock: ddField(box, PICK.card_stock, 40), sold: ddField(box, PICK.card_sold, 40),
    badges: ddAll(box, '[class*="badge"],[class*="Badge"],[class*="ribbon"],[class*="tag"],[class*="label"]')
      .map(function (n) { return ddNorm(n.innerText); }).filter(function (t) { return t && t.length <= 40; }).slice(0, 8),
    section: section ? ddField(section, PICK.section_title, 120) : null,
    image: img ? img.src : null, text: ddNorm(box.innerText).slice(0, 400)};
}
function ddCollect() {
  window.__dd = window.__dd || {order: [], map: {}};
  var acc = window.__dd;
  ddAll(document, 'a[href*="productDetails"]').forEach(function (a) {
    var c = ddCard(a);
    var key = c.id || c.href;
    if (!acc.map[key]) { acc.order.push(key); acc.map[key] = c; }
    else if (!acc.map[key].title && c.title) { acc.map[key] = c; }
  });
  return {total: acc.order.length};
}
function ddScroll() {
  var links = ddAll(document, 'a[href*="productDetails"]');
  var last = links[links.length - 1];
  if (last) {
    try { last.scrollIntoView({block: 'end'}); } catch (e) {}
    var p = last.parentElement;
    while (p && p !== document.body) {
      var s = getComputedStyle(p);
      if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && p.scrollHeight > p.clientHeight) { p.scrollTop = p.scrollHeight; }
      p = p.parentElement;
    }
  }
  // Nudge up, then back to the bottom a moment later, so a "load more on view"
  // watcher sees the end of the list come into view again.
  window.scrollBy(0, -900);
  return new Promise(function (done) {
    setTimeout(function () {
      if (last) { try { last.scrollIntoView({block: 'end'}); } catch (e) {} }
      window.scrollTo(0, document.documentElement.scrollHeight);
      window.dispatchEvent(new Event('scroll'));
      done(true);
    }, 700);
  });
}
function ddFinal() {
  ddCollect();
  var acc = window.__dd;
  var cats = ddAll(document, PICK.category).map(function (n) {
    var a = n.matches('a[href]') ? n : (n.querySelector('a[href]') || n.closest('a[href]'));
    var img = n.querySelector('img');
    return {href: a ? a.href : null, text: ddNorm(n.innerText).slice(0, 120), image: img ? img.src : null};
  });
  var catLinks = ddAll(document, 'a[href*="categoryId"]').map(function (a) {
    var sec = null; try { sec = a.closest(PICK.section); } catch (e) {}
    return {href: a.href, text: ddNorm(a.innerText).slice(0, 120), in_section: sec ? ddField(sec, PICK.section_title, 120) : null};
  });
  var sections = ddAll(document, PICK.section).map(function (s) {
    return {title: ddField(s, PICK.section_title, 120), links: ddAll(s, 'a[href]').map(function (a) { return a.href; })
      .filter(function (h) { return !PRODUCT.test(h); }).slice(0, 10), cards: ddAll(s, 'a[href*="productDetails"]').length};
  });
  var controls = ddAll(document, 'button, a, [role="button"], .pagination, nav')
    .map(function (n) { return ddNorm(n.innerText); })
    .filter(function (t) { return t && t.length <= 30 && /load more|view more|show more|see more|view all|next|previous|^\d+$/i.test(t); })
    .slice(0, 40);
  return {url: location.href, cards: acc.order.map(function (k) { return acc.map[k]; }), categories: cats,
    category_links: catLinks, sections: sections, controls: controls,
    has_pagination: !!document.querySelector('.pagination, [class*="pagination"], [class*="Pagination"]')};
}
