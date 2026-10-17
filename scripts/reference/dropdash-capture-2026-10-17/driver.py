import json, math, os, subprocess, sys, time, datetime
D = os.path.dirname(os.path.abspath(__file__))
w = lambda p: subprocess.check_output(['wslpath', '-w', p]).decode().strip()
home = json.load(open(D + '/out/home.json'))
counts = {c['sideimage']['category_Id']: c['sideimage']['total_products_count_in_category'] for c in json.loads(home['bodies'][0]['body'])['data']}
order = [63, 293, 218, 239, 285, 284, 288, 295, 227, 233, 224, 296, 106, 25, 1, 292, 221, 236, 39, 214]
log = open(D + '/out/driver.log', 'a')
def say(s):
    log.write(datetime.datetime.utcnow().isoformat() + 'Z ' + s + '\n'); log.flush()
last_nav = time.time() - 40
say('driver start')
for cid in order:
    out = D + f'/out/cat-{cid}.json'
    if os.path.exists(out):
        try:
            if json.load(open(out, encoding='utf-8-sig')).get('state') == 'captured': continue
        except Exception: pass
    gap = 35 - (time.time() - last_nav)
    if gap > 0: time.sleep(gap)
    job = {"name": f"cat-{cid}", "url": f"https://app.dropdash.co/products?categoryId={cid}", "scroll": True, "flat_stop": 2,
           "max_loads": math.ceil(counts[cid] / 18) + 6, "expect_cards": True, "js_file": w(D + "/dd.js"), "output": w(out)}
    json.dump(job, open(D + f'/jobs/cat-{cid}.json', 'w'))
    last_nav = time.time()
    say(f'nav {cid} expect {counts[cid]}')
    try:
        r = subprocess.run([D + '/ps.sh', 'run', w(D + f'/jobs/cat-{cid}.json')], capture_output=True, text=True, timeout=2400)
        say(f'ps {cid}: {r.stdout.strip()[-200:]} {r.stderr.strip()[-300:]}')
    except subprocess.TimeoutExpired:
        say(f'timeout {cid}'); continue
    try:
        d = json.load(open(out, encoding='utf-8-sig'))
    except Exception as e:
        say(f'no output {cid} {e}'); continue
    say(f"done {cid} state={d.get('state')} stop={d.get('stop_reason')} cards={len((d.get('dom') or {}).get('cards') or [])} loads={len(d.get('loads') or [])} err={d.get('error')}")
    if d.get('signed_out') or d.get('stop_reason') in ('SIGNED_OUT', 'BLOCKED'):
        say(f"STOP {d.get('stop_reason')} at {d.get('signed_out_at') or d.get('finished_at')}"); break
say('driver end')
