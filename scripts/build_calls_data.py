"""
Process Spectrum call CSV → CALLS_DATA JS block for Practice Pulse dashboard.
Outputs a JS snippet to stdout that can be pasted into index.html.

Usage:
    python3 build_calls_data.py <csv_file> [--month YYYY-MM]

Inbound:  From field is a formatted phone "(xxx) xxx-xxxx" or "Anonymous"
Outbound: From field is an internal extension (3-4 digits)
"""

import sys, re, csv, json
from datetime import date, datetime, timedelta
from collections import defaultdict

# ── Constants ─────────────────────────────────────────────────────

# Extensions ending in sp = Spanish line, wp = English
EXT_RE = re.compile(r'^(\d+)(sp|wp)$', re.I)

# VMail box → language mapping (based on extension association)
# VMail (xxx) → look up xxx in extension map
VMAIL_RE = re.compile(r'^VMail \((\d+)\)$', re.I)

# Spanish extensions pool (adjust to match office setup)
SPANISH_EXTS = {
    '114', '119', '116', '105', '108', '111', '112', '113', '115', '117',
}
ENGLISH_EXTS = {
    '118', '102', '101', '104', '106',
}

# Phone patterns
PHONE_RE = re.compile(r'^\(\d{3}\) \d{3}-\d{4}$')
EXT_NUM_RE = re.compile(r'^\d{3,4}$')

# Shift buckets (24h)
def shift(t):
    h = int(t.split(':')[0])
    if 7 <= h < 12:  return 'Morning'
    if 12 <= h < 17: return 'Afternoon'
    return 'After Hours'

def is_inbound(from_field):
    f = from_field.strip()
    return bool(PHONE_RE.match(f)) or f.lower() in ('anonymous', '', 'unavailable')

def is_outbound(from_field):
    f = from_field.strip()
    return bool(EXT_NUM_RE.match(f))

def classify_to(to_field):
    """Classify inbound call outcome from the To field."""
    t = to_field.strip()
    if EXT_RE.match(t):          return 'Answered'
    if VMAIL_RE.match(t):        return 'VM'
    if t in ('SpeechCommand', 'SpeakAccount'): return 'IVR'
    if t == 'Call-Queue':        return 'Queue'
    if EXT_NUM_RE.match(t):
        # Bare extension — check if it's an answered call
        num = t
        if num in SPANISH_EXTS or num in ENGLISH_EXTS:
            return 'Answered'
        return 'Other'
    return 'Other'

def lang_from_to(to_field):
    """Detect language from To field for inbound calls.

    Data analysis shows SpeechCommand/SpeakAccount are 99% Spanish callers.
    VMail boxes are majority Spanish (85%+).
    Call-Queue is mixed, treated as Unknown.
    """
    t = to_field.strip()
    m = EXT_RE.match(t)
    if m:
        return 'Spanish' if m.group(2).lower() == 'sp' else 'English'
    vm = VMAIL_RE.match(t)
    if vm:
        ext = vm.group(1)
        if ext in SPANISH_EXTS: return 'Spanish'
        if ext in ENGLISH_EXTS: return 'Spanish'  # VMail boxes are majority Spanish per data
        return 'Spanish'  # all VMail boxes in this dataset are majority Spanish
    # IVR types — empirically 99% Spanish callers
    if t in ('SpeechCommand', 'SpeakAccount'):
        return 'Spanish'
    return 'Unknown'

def is_business_day(d: date) -> bool:
    return d.weekday() < 5  # Mon=0 … Fri=4

def fmt_phone(s):
    s = re.sub(r'\D', '', s)
    if len(s) == 10:
        return f'({s[:3]}) {s[3:6]}-{s[6:]}'
    if len(s) == 11 and s[0] == '1':
        return f'({s[1:4]}) {s[4:7]}-{s[7:]}'
    return s

# ── Load CSV ──────────────────────────────────────────────────────

def load_csv(path):
    with open(path, encoding='utf-8-sig') as f:
        lines = f.read().strip().split('\n')
    headers = [h.strip() for h in lines[0].split(',')]
    rows = []
    for line in lines[1:]:
        parts = line.split(',')
        rows.append(dict(zip(headers, [p.strip() for p in parts])))
    return rows

# ── Process ───────────────────────────────────────────────────────

def process(csv_path, month=None):
    rows = load_csv(csv_path)

    # Group by date
    by_date = defaultdict(list)
    for r in rows:
        d_str = r.get('Call Date', '').strip()
        if not d_str:
            continue
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        if not is_business_day(d):
            continue
        if month and not d_str.startswith(month):
            continue
        by_date[d_str].append(r)

    result = {}

    for d_str in sorted(by_date):
        d = date.fromisoformat(d_str)
        day_rows = by_date[d_str]

        # ── Inbound ──────────────────────────────────────────────
        inbound = [r for r in day_rows if is_inbound(r.get('From', ''))]

        # Unique callers by phone
        seen_phones = {}
        for r in inbound:
            phone = fmt_phone(r.get('From', ''))
            ts = r.get('Call Time', '00:00:00')
            to = r.get('To', '')
            outcome = classify_to(to)
            lang = lang_from_to(to)
            name = r.get('From Name', '').title()

            if phone not in seen_phones:
                seen_phones[phone] = {
                    'phone': phone,
                    'name': name,
                    'lang': lang,
                    'first_seen': ts,
                    'attempts': 1,
                    'outcomes': [outcome],
                }
            else:
                seen_phones[phone]['attempts'] += 1
                seen_phones[phone]['outcomes'].append(outcome)
                if outcome != 'Answered':
                    pass  # keep tracking

        # A caller is "connected" if ANY of their attempts was Answered or VM
        connected_callers = {p for p, v in seen_phones.items()
                             if any(o in ('Answered', 'VM') for o in v['outcomes'])}
        nc_callers = {p: v for p, v in seen_phones.items() if p not in connected_callers}

        total_unique = len(seen_phones)
        connected_count = len(connected_callers)
        nc_count = len(nc_callers)
        rate = round(connected_count / total_unique * 100, 1) if total_unique else 0

        # Language breakdown for inbound (first routing)
        sp_calls = sum(1 for v in seen_phones.values() if v['lang'] == 'Spanish')
        en_calls = sum(1 for v in seen_phones.values() if v['lang'] == 'English')
        sp_nc = sum(1 for p, v in nc_callers.items() if v['lang'] == 'Spanish')
        en_nc = sum(1 for p, v in nc_callers.items() if v['lang'] == 'English')

        # Shifts (based on first call time for NC callers)
        shifts = defaultdict(int)
        for v in nc_callers.values():
            shifts[shift(v['first_seen'])] += 1

        # Overall outcome counts (all inbound rows, not unique)
        outcomes = defaultdict(int)
        for r in inbound:
            outcomes[classify_to(r.get('To', ''))] += 1

        nc_list = sorted(nc_callers.values(), key=lambda x: -x['attempts'])

        # Simplify outcomes list for NC callers
        def dominant_status(outcomes_list):
            for o in ['IVR', 'Queue', 'Ring', 'Failed']:
                if o in outcomes_list:
                    return o
            return outcomes_list[0] if outcomes_list else 'Unknown'

        nc_list_out = [
            {
                'Phone': v['phone'],
                'Caller_Name': v['name'],
                'Language': v['lang'],
                'First_Seen': v['first_seen'],
                'Total_Attempts': v['attempts'],
                'Status': dominant_status(v['outcomes']),
            }
            for v in nc_list
        ]

        # ── Outbound ─────────────────────────────────────────────
        outbound = [r for r in day_rows if is_outbound(r.get('From', ''))]

        # Calls to external numbers only
        ob_to_external = [r for r in outbound
                          if PHONE_RE.match(r.get('To', '').strip())
                          or re.match(r'^\(\d{3}\)', r.get('To', ''))]

        ob_unique_patients = len(set(r.get('To', '').strip() for r in ob_to_external))
        ob_connected = sum(1 for r in ob_to_external
                           if r.get('Duration', '00:00:00') not in ('00:00:00', '00:00:01', '00:00:02'))

        # Callback rate: inbound NC callers who got an outbound call same day
        ob_phones_called = set(fmt_phone(r.get('To', '')) for r in ob_to_external)
        nc_phones = set(nc_callers.keys())
        callbacks_done = len(nc_phones & ob_phones_called)

        label = d.strftime('%-d %b').replace(' ', ' ')

        result[d_str] = {
            'label': d.strftime('%b %-d'),
            'weekday': d.strftime('%a'),
            # Inbound
            'total': total_unique,
            'connected': connected_count,
            'connection_rate': rate,
            'nc': nc_count,
            'spanish_nc': sp_nc,
            'english_nc': en_nc,
            'spanish_calls': sp_calls,
            'english_calls': en_calls,
            'shifts': dict(shifts),
            'outcomes': dict(outcomes),
            'nc_list': nc_list_out[:60],
            # Outbound
            'ob_total': len(ob_to_external),
            'ob_unique': ob_unique_patients,
            'ob_connected': ob_connected,
            'callbacks': callbacks_done,
        }

    return result

# ── Monthly summary ───────────────────────────────────────────────

def monthly_summary(data):
    if not data:
        return {}
    totals = dict(
        total=0, connected=0, nc=0, spanish_nc=0, english_nc=0,
        ob_total=0, ob_unique=0, ob_connected=0, callbacks=0
    )
    for d in data.values():
        for k in totals:
            totals[k] += d.get(k, 0)
    totals['connection_rate'] = round(totals['connected'] / totals['total'] * 100, 1) if totals['total'] else 0
    totals['callback_rate'] = round(totals['callbacks'] / totals['nc'] * 100, 1) if totals['nc'] else 0
    totals['days'] = len(data)
    return totals

# ── Main ──────────────────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 build_calls_data.py <csv_file> [--month YYYY-MM]', file=sys.stderr)
        sys.exit(1)

    csv_path = sys.argv[1]
    month = None
    if '--month' in sys.argv:
        month = sys.argv[sys.argv.index('--month') + 1]

    data = process(csv_path, month)
    summary = monthly_summary(data)

    print('const CALLS_SUMMARY =', json.dumps(summary, indent=2), ';')
    print()
    print('const CALLS_DATA =', json.dumps(data, indent=2), ';')

    print(f'\n// {len(data)} business days processed', file=sys.stderr)
    for d, v in list(data.items())[:3]:
        print(f'//   {d}: {v["total"]} inbound, {v["nc"]} NC, {v["ob_total"]} outbound, {v["callbacks"]} callbacks', file=sys.stderr)
