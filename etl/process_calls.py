"""
ETL: Download call_history_master.csv from S3, process it,
     and upload aggregated JSON (no PHI) + private NC lists back to S3.

Run manually or add to the ECS task after the Spectrum export step.
Usage: python3 etl/process_calls.py
"""

import json, os, sys, re, boto3, tempfile
from pathlib import Path
from datetime import date
from collections import defaultdict

S3_BUCKET     = os.environ.get('S3_BUCKET', 'lenity-stratus-state')
S3_SRC_KEY    = 'stratus/call_history_master.csv'
S3_DASH_PREFIX = 'stratus/dashboard'
REGION        = os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')

# ── Inline processing logic (mirrors build_calls_data.py) ─────────

EXT_RE   = re.compile(r'^(\d+)(sp|wp)$', re.I)
VMAIL_RE = re.compile(r'^VMail \((\d+)\)$', re.I)
PHONE_RE = re.compile(r'^\(\d{3}\) \d{3}-\d{4}$')
EXT_NUM  = re.compile(r'^\d{3,4}$')


def shift(t):
    h = int(t.split(':')[0])
    if 7 <= h < 12:  return 'Morning'
    if 12 <= h < 17: return 'Afternoon'
    return 'After Hours'

def is_inbound(f):
    f = f.strip()
    return bool(PHONE_RE.match(f)) or f.lower() in ('anonymous', '', 'unavailable')

def is_outbound(f):
    return bool(EXT_NUM.match(f.strip()))

def classify_to(to):
    t = to.strip()
    if EXT_RE.match(t):                        return 'Answered'
    if VMAIL_RE.match(t):                      return 'VM'
    if t in ('SpeechCommand','SpeakAccount'):  return 'IVR'
    if t == 'Call-Queue':                      return 'Queue'
    if EXT_NUM.match(t):                       return 'Answered'
    return 'Other'

def lang_from_to(to):
    t = to.strip()
    m = EXT_RE.match(t)
    if m:  return 'Spanish' if m.group(2).lower() == 'sp' else 'English'
    if VMAIL_RE.match(t):                      return 'Spanish'
    if t in ('SpeechCommand','SpeakAccount'):  return 'Spanish'
    return 'Unknown'

def fmt_phone(s):
    d = re.sub(r'\D', '', s)
    if len(d) == 10:  return f'({d[:3]}) {d[3:6]}-{d[6:]}'
    if len(d) == 11 and d[0] == '1': return f'({d[1:4]}) {d[4:7]}-{d[7:]}'
    return s

def dominant_status(outcomes):
    for o in ('IVR', 'Queue', 'Other'):
        if o in outcomes: return o
    return outcomes[0] if outcomes else 'Unknown'

def is_business_day(d): return d.weekday() < 5

def load_csv(path):
    with open(path, encoding='utf-8-sig') as f:
        lines = f.read().strip().split('\n')
    headers = [h.strip() for h in lines[0].split(',')]
    return [dict(zip(headers, [p.strip() for p in l.split(',')])) for l in lines[1:] if l.strip()]

def process(path):
    rows = load_csv(path)
    by_date = defaultdict(list)
    for r in rows:
        ds = r.get('Call Date','').strip()
        if not ds: continue
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            continue
        if is_business_day(d):
            by_date[ds].append(r)

    data, nc_by_date = {}, {}

    for ds in sorted(by_date):
        d = date.fromisoformat(ds)
        day = by_date[ds]

        inbound  = [r for r in day if is_inbound(r.get('From',''))]
        outbound = [r for r in day if is_outbound(r.get('From',''))]

        # unique callers
        callers = {}
        for r in inbound:
            ph = fmt_phone(r.get('From',''))
            to = r.get('To','')
            oc = classify_to(to)
            lg = lang_from_to(to)
            ts = r.get('Call Time','00:00:00')
            nm = r.get('From Name','').title()
            if ph not in callers:
                callers[ph] = {'phone':ph,'name':nm,'lang':lg,'first_seen':ts,'attempts':1,'outcomes':[oc]}
            else:
                callers[ph]['attempts'] += 1
                callers[ph]['outcomes'].append(oc)

        connected = {p for p,v in callers.items() if any(o in ('Answered','VM') for o in v['outcomes'])}
        nc_map    = {p:v for p,v in callers.items() if p not in connected}

        total = len(callers)
        n_con = len(connected)
        n_nc  = len(nc_map)
        rate  = round(n_con/total*100, 1) if total else 0

        sp_calls = sum(1 for v in callers.values() if v['lang'] == 'Spanish')
        en_calls = sum(1 for v in callers.values() if v['lang'] == 'English')
        sp_nc    = sum(1 for v in nc_map.values() if v['lang'] == 'Spanish')
        en_nc    = sum(1 for v in nc_map.values() if v['lang'] == 'English')

        shifts   = defaultdict(int)
        for v in nc_map.values():
            shifts[shift(v['first_seen'])] += 1

        outcomes = defaultdict(int)
        for r in inbound:
            outcomes[classify_to(r.get('To',''))] += 1

        ob_ext = [r for r in outbound if PHONE_RE.match(r.get('To','').strip()) or re.match(r'^\(\d{3}\)',r.get('To',''))]
        ob_uniq = len(set(r.get('To','').strip() for r in ob_ext))
        ob_conn = sum(1 for r in ob_ext if r.get('Duration','00:00:00') not in ('00:00:00','00:00:01','00:00:02'))
        ob_phns = set(fmt_phone(r.get('To','')) for r in ob_ext)
        callbacks = len(set(nc_map.keys()) & ob_phns)

        nc_list = sorted(nc_map.values(), key=lambda x: -x['attempts'])
        nc_list_out = [{'Phone':v['phone'],'Caller_Name':v['name'],'Language':v['lang'],
                        'First_Seen':v['first_seen'],'Total_Attempts':v['attempts'],
                        'Status':dominant_status(v['outcomes'])} for v in nc_list]

        nc_by_date[ds] = nc_list_out

        data[ds] = {
            'label':   d.strftime('%b %-d'),
            'weekday': d.strftime('%a'),
            'total':   total, 'connected':n_con, 'connection_rate':rate,
            'nc':n_nc, 'spanish_nc':sp_nc, 'english_nc':en_nc,
            'spanish_calls':sp_calls, 'english_calls':en_calls,
            'shifts':dict(shifts), 'outcomes':dict(outcomes),
            'ob_total':len(ob_ext), 'ob_unique':ob_uniq,
            'ob_connected':ob_conn, 'callbacks':callbacks,
        }

    # summary
    s = {k:0 for k in ('total','connected','nc','spanish_nc','english_nc','ob_total','ob_unique','ob_connected','callbacks')}
    for v in data.values():
        for k in s: s[k] += v.get(k,0)
    s['connection_rate'] = round(s['connected']/s['total']*100, 1) if s['total'] else 0
    s['callback_rate']   = round(s['callbacks']/s['nc']*100, 1) if s['nc'] else 0
    s['days'] = len(data)

    return data, s, nc_by_date

# ── Upload helpers ────────────────────────────────────────────────

def upload(s3, body, key, public=False):
    kwargs = dict(Bucket=S3_BUCKET, Key=key, Body=body,
                  ContentType='application/json')
    if public:
        kwargs['ACL'] = 'public-read'
    s3.put_object(**kwargs)
    print(f'  uploaded → s3://{S3_BUCKET}/{key}' + (' [public]' if public else ' [private]'))

# ── Main ─────────────────────────────────────────────────────────

def main():
    s3 = boto3.client('s3', region_name=REGION)

    print(f'Downloading s3://{S3_BUCKET}/{S3_SRC_KEY} ...')
    with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as tmp:
        s3.download_fileobj(S3_BUCKET, S3_SRC_KEY, tmp)
        tmp_path = tmp.name

    print('Processing ...')
    data, summary, nc_by_date = process(tmp_path)
    print(f'  {summary["days"]} business days  |  {summary["total"]} inbound  |  {summary["nc"]} NC  |  {summary["ob_total"]} outbound')

    # Public: aggregated data (no PHI)
    public_data = json.dumps(data, separators=(',', ':'))
    upload(s3, public_data, f'{S3_DASH_PREFIX}/calls_data.json', public=True)

    public_summary = json.dumps(summary, separators=(',', ':'))
    upload(s3, public_summary, f'{S3_DASH_PREFIX}/calls_summary.json', public=True)

    # Private: per-date NC lists (contain phone numbers / names)
    for ds, nc_list in nc_by_date.items():
        body = json.dumps(nc_list, separators=(',', ':'))
        upload(s3, body, f'{S3_DASH_PREFIX}/nc/nc_{ds}.json', public=False)

    print(f'\nDone. Public data URL:')
    print(f'  https://{S3_BUCKET}.s3.{REGION}.amazonaws.com/{S3_DASH_PREFIX}/calls_data.json')
    os.unlink(tmp_path)

if __name__ == '__main__':
    main()
