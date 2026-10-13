// Read-only owner page. The existing bridge remains the sole identity/storage
// authority; this page never accepts uploads, approvals or publication actions.
import { handleAdminRequest } from './release-admin.mjs';

const SECURITY = {
  'Cache-Control': 'private, no-store, max-age=0', Vary: 'Cookie',
  'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
  'Content-Security-Policy': "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
};

function escape(value) {
  return String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}

function reportState(issue, now) {
  if (issue.remote_state === 'withdrawn') return 'Withdrawn';
  if (!issue.approved || !issue.complete) return 'Waiting for approval or completion';
  return Math.max(Date.parse(issue.scheduled_at), Date.parse(issue.server_activated_at)) <= now
    ? 'Available now' : 'Scheduled for later';
}

function page(snapshot, logo, drafts = null) {
  const now = Date.parse(snapshot.server_observed_at);
  const paged = snapshot.index.schema_version === 2, issues = paged ? snapshot.issue_page?.rows.map(row => row.value) ?? [] : snapshot.index.issues;
  const counts = { 'Saved report versions': paged ? snapshot.index.pages.archive?.count ?? 0 : issues.length,
    ...(paged ? { 'Versions checked on this page': issues.length } : {}), 'Available now': 0,
    'Scheduled for later': 0, 'Withdrawn': 0, 'Waiting for approval or completion': 0 };
  for (const issue of issues) counts[reportState(issue, now)]++;
  const summary = Object.entries(counts).map(([label, count]) => `<tr><th scope="row">${escape(label)}</th><td>${count}</td></tr>`).join('');
  const reports = [...issues].sort((a, b) => Date.parse(b.scheduled_at) - Date.parse(a.scheduled_at) || b.revision - a.revision)
    .map(issue => `<tr><th scope="row">${escape(issue.title)}</th><td>${escape(reportState(issue, now))}</td><td>${issue.product_count}</td></tr>`).join('');
  const status = paged ? 'Private storage was read successfully. The status counts below cover only the versions checked on this page; the saved-version total covers all retained history.' : issues.length ? 'Private storage was read successfully. The saved reports are listed below.'
    : snapshot.etag === null ? 'Private storage is reachable. No report list is saved on this website yet.'
    : 'Private storage was read successfully. The saved report list is empty.';
  const checked = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata' }).format(new Date(now));
  const privateContent = drafts === null ? '' : `<h2>Private drafts</h2><p>These files are saved for your review. They have not been released to the client.</p>${drafts.drafts.map(draft =>
    `<article><h3>${escape(draft.issue_id)} · version ${draft.revision}</h3><p>${draft.product_count} products. Not approved for the client.</p><p>${draft.files.map(file =>
      `<a href="${escape(file.url)}">Download ${file.name === 'shortlist.xlsx' ? 'Excel' : file.name === 'report-en.pdf' ? 'English PDF' : 'Hinglish PDF'}</a>`).join(' · ')}</p></article>`).join('') || '<p>No complete private draft files were found on this page.</p>'}${drafts.skipped.length ? `<p>${drafts.skipped.length} saved draft versions could not be checked. The other drafts remain downloadable.</p>` : ''}${drafts.scan_limited ? '<p>This is a limited check of saved versions, not a complete count of all stored reports.</p>' : ''}${drafts.next_cursor ? `<p><a href="/admin/drafts?cursor=${escape(drafts.next_cursor)}">More saved drafts</a></p>` : ''}`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Owner report status · NeuraFlow</title><style>
  :root{color-scheme:light;--cream:#F9F3EC;--maroon:#6B1F2B;--ink:#253540;--muted:#636862;--line:#dcd3c8;--sage:#e5e9df}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:17px/1.6 Arial,sans-serif}header,main,footer{max-width:820px;margin:auto;padding:28px}header{display:flex;align-items:center;justify-content:space-between;gap:24px;border-bottom:1px solid var(--line)}.brand{display:block;width:172px;height:48px;overflow:hidden}.brand img{display:block;width:172px;height:86px;margin-top:-14px}.owner{font-size:15px;color:var(--muted)}main{padding-top:48px;padding-bottom:64px;min-height:65vh}h1,h2{font:normal 40px/1.2 Georgia,serif;letter-spacing:-.7px;margin:12px 0 22px}h2{font-size:28px;margin-top:36px}p{max-width:64ch}.note,.checked,footer{font-size:14px;color:var(--muted)}a{color:var(--maroon)}.refresh{display:inline-block;font:inherit;color:var(--maroon);background:var(--sage);padding:10px 16px;text-decoration:none}a:focus-visible{outline:3px solid var(--maroon);outline-offset:5px}table{width:100%;border-collapse:collapse;margin:24px 0}th,td{text-align:left;vertical-align:top;padding:12px 8px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}thead th{font-size:14px;color:var(--muted)}tbody th{font-weight:normal}.summary td{width:80px;text-align:right}.table-wrap{overflow-x:auto}footer{border-top:1px solid var(--line)}@media(max-width:520px){header,main,footer{padding-left:20px;padding-right:20px}header{align-items:flex-start;flex-direction:column;gap:14px}main{padding-top:36px}h1{font-size:34px}h2{font-size:27px}th,td{padding-left:4px;padding-right:8px}}
  </style></head><body><header><a class="brand" href="/admin" aria-label="NeuraFlow owner report status"><img src="${escape(logo)}" alt="NeuraFlow" width="172" height="86"></a><span class="owner">Owner view</span></header><main><h1>Report status</h1><p>This page shows the reports listed on this website. Research and drafts that are not yet listed are not counted here.</p><p role="status">${escape(status)}</p><a class="refresh" href="/admin">Refresh status</a><p><a href="/admin/drafts">View private drafts and download files</a></p><p class="checked">Checked at ${escape(checked)} India time.</p><table class="summary" aria-label="Saved report status"><tbody>${summary}</tbody></table><h2>Saved reports</h2><div class="table-wrap"><table><thead><tr><th scope="col">Report</th><th scope="col">Status</th><th scope="col">Products</th></tr></thead><tbody>${reports}</tbody></table></div>${privateContent}<p class="note">This page cannot upload, edit, approve or publish a report. A successful storage check does not mean that a report is ready for the client.</p></main><footer>Product research, made easier.</footer></body></html>`;
}

export async function handleAdminStatus(request, env, ctx, logo, { clock = () => new Date() } = {}) {
  // One guarded bridge read preserves its exact configuration, verified
  // audience and owner-email checks. No policy is copied or broadened here.
  const url = new URL('/admin/index', request.url);
  const verified = await handleAdminRequest(new Request(url, { method: 'GET',
    headers: { 'X-NeuraFlow-Release-Action': 'v1' } }), env, ctx, { clock });
  const head = request.method === 'HEAD';
  function reply(body, status = 200, type = 'text/plain; charset=utf-8', extra = {}) {
    return new Response(head ? null : body, { status, headers: { ...SECURITY, 'Content-Type': type, ...extra } });
  }
  if (!verified.ok) return reply(verified.status === 403 ? 'Owner access required.'
    : 'Owner report status is temporarily unavailable. No report was changed.', verified.status === 403 ? 403 : 503);
  if (!['GET', 'HEAD'].includes(request.method)) return reply('Method not allowed.', 405, undefined, { Allow: 'GET, HEAD' });
  try {
    const snapshot = await verified.json();
    if (snapshot.index.schema_version === 2) {
      const source = new URL(request.url);
      if ([...source.searchParams.keys()].some(key => key !== 'cursor') || source.searchParams.getAll('cursor').length > 1) return reply('Invalid report page.', 400);
      const query = '/admin/index-page?kind=archive' + (source.searchParams.has('cursor') ? '&cursor=' + encodeURIComponent(source.searchParams.get('cursor')) : '');
      const result = await handleAdminRequest(new Request(new URL(query, request.url), { headers: { 'X-NeuraFlow-Release-Action': 'v1' } }), env, ctx, { clock });
      if (!result.ok) return reply(result.status === 409 ? 'The report list changed. Please refresh the owner page.' : 'Owner report status is temporarily unavailable. No report was changed.', result.status === 409 ? 409 : 503);
      const selected = await result.json(); selected.issue_page = { rows: selected.rows, next_cursor: selected.next_cursor };
      const html = page(selected, logo).replace('</main>', `${selected.next_cursor ? `<p><a href="/admin?cursor=${escape(selected.next_cursor)}">More saved report versions</a></p>` : ''}</main>`);
      return reply(html, 200, 'text/html; charset=utf-8');
    }
    return reply(page(snapshot, logo), 200, 'text/html; charset=utf-8');
  } catch { return reply('Owner report status is temporarily unavailable. No report was changed.', 503); }
}

export async function handleDraftStatus(request, env, ctx, logo, { clock = () => new Date() } = {}) {
  const internal = path => handleAdminRequest(new Request(new URL(path, request.url), { method: 'GET',
    headers: { 'X-NeuraFlow-Release-Action': 'v1' } }), env, ctx, { clock });
  const head = request.method === 'HEAD';
  const reply = (body, status, type = 'text/plain; charset=utf-8', extra = {}) => new Response(head ? null : body,
    { status, headers: { ...SECURITY, 'Content-Type': type, ...extra } });
  // Both reads pass the original verified-audience/owner guard. Client emails
  // and unverified claims cannot obtain any list or file, even via HEAD.
  const index = await internal('/admin/index');
  if (!index.ok) return reply(index.status === 403 ? 'Owner access required.' : 'Private drafts could not be checked.', index.status === 403 ? 403 : 503);
  if (!['GET', 'HEAD'].includes(request.method)) return reply('Method not allowed.', 405, undefined, { Allow: 'GET, HEAD' });
  const url=new URL(request.url);
  if([...url.searchParams.keys()].some(key=>key!=='cursor')||url.searchParams.getAll('cursor').length>1)return reply('This page does not accept these search options.',400);
  const drafts = await internal('/admin/draft-page'+(url.searchParams.has('cursor')?'?cursor='+encodeURIComponent(url.searchParams.get('cursor')):''));
  if (!drafts.ok) return reply(drafts.status===409?'The saved draft list changed. Open the private drafts page again to continue. No report was changed.':'Private drafts could not be checked. No report was changed.',drafts.status===409?409:503);
  try { return reply(page(await index.json(), logo, await drafts.json()), 200, 'text/html; charset=utf-8'); }
  catch { return reply('Private drafts could not be checked. No report was changed.', 503); }
}

export async function handleDraftDownload(request, env, ctx) {
  const url = new URL(request.url), head = request.method === 'HEAD';
  const match = /^\/admin\/download\/([A-Za-z0-9_-]{1,80})\/([1-9]\d*)\/(shortlist\.xlsx|report-en\.pdf|report-hi-Latn\.pdf)$/.exec(url.pathname);
  const bridgePath = match && ['GET', 'HEAD'].includes(request.method) && !url.search
    ? `/admin/draft-files/${match[1]}/${match[2]}/${match[3]}` : '/admin/index';
  const verified = await handleAdminRequest(new Request(new URL(bridgePath, url), { method: 'GET',
    headers: { 'X-NeuraFlow-Release-Action': 'v1' } }), env, ctx);
  if (!verified.ok) return new Response(head ? null : 'Private download could not be checked.', { status: verified.status, headers: SECURITY });
  if (!['GET', 'HEAD'].includes(request.method)) return new Response('Method not allowed.', { status: 405, headers: { ...SECURITY, Allow: 'GET, HEAD' } });
  if (!match || url.search) return new Response(head ? null : 'Not found.', { status: 404, headers: SECURITY });
  return new Response(head ? null : verified.body, { status: verified.status, headers: { ...Object.fromEntries(verified.headers), ...SECURITY } });
}
