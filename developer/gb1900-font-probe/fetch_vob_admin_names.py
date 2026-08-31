"""Fetch historic-admin name gazetteers from Vision of Britain (visionofbritain.org.uk), for the OS
Characteristic-Sheet admin FACES that Wikidata could not supply cleanly — the hundred-tier (hundreds,
wapentakes, wards, rapes, lathes, liberties, sokes, baronies, lordships), historic boroughs/towns, and
townships/chapelries. These are exactly the ALLCAPS admin labels the OS six-inch sheets set in distinct
fonts; a genuine name list per face lets build_alphabet_multi seed those faces by LOOKING UP real labels
(all-caps) instead of a single ornate mark-letter (which attracts by letter-content, not font).

VoB's "administrative units" search (/expertsearch#tab02) is session-stateful: a POST to units/matches with
kind=<code>&pname=*&sdx=N establishes the result set in the session, then GET match_any.jsp?start=N pages
through it 15 rows at a time (the cookie carries the result set). We enumerate per kind, page to the total
(or a per-kind cap, LOGGED when hit), strip the trailing unit-type word, and dedupe case-insensitively.

MUST run from a host that can reach visionofbritain.org.uk with a relaxed TLS check (the site's chain fails
strict verification; pitt/CRC cannot reach it at all). Hence urllib + an unverified SSL context, run LOCALLY:

    python3 fetch_vob_admin_names.py            # -> labels/vob_admin_names.json
"""
import http.cookiejar, json, os, re, ssl, sys, time, urllib.parse, urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "labels", "vob_admin_names.json")
BASE = "https://www.visionofbritain.org.uk/units"
UA = "GB-STAMP/1.0 (World Historical Gazetteer; stephen@docuracy.co.uk)"
CAP = 2500                                    # per-kind name cap (bounds request count; logged when hit)
PAGE = 15                                     # server-fixed page size
SLEEP = 0.4

# OS Characteristic-Sheet FACE (font_taxonomy.json "key") -> VoB kind codes (GB only; drop Ireland, and
# registration/sanitary districts + parliamentary constituencies — not six-inch topographic furniture).
# Keyed by TAXONOMY FACE so the lists drop straight into build_alphabet_multi's name2face. The sheet sets
# hundreds (upright), liberties (italic) and wards in DISTINCT fonts, so they are kept as distinct faces
# rather than lumped. Wapentakes/rapes/lathes/sokes/baronies/lordships are hundred-equivalents (same tier,
# same "Hundreds" font) -> hundreds. Townships/tythings share the "Civil Parishes or Townships" face.
FACE_KINDS = {
    "county_names":   ["T_ANC_CNTY", "T_SCO_CNTY", "T_ADM_CNTY"],
    "hundreds":       ["S_Hundred", "S_Wap", "S_Rape", "S_Lathe", "S_Soke",
                       "S_Bny", "S_Ldsp", "S_Parts", "S_CntyOfIt", "S_Fh", "S_Div"],
    "liberties":      ["S_Liberty"],
    "wards":          ["S_Wd"],
    "boroughs_munic": ["S_MB", "S_Borough"],
    "county_boroughs":["S_CB"],
    "civil_parishes": ["S_Tn", "S_Tg"],
}
# Trailing unit-type tokens VoB appends to a unit's display name — both spelled-out words AND the short
# status CODES (e.g. "AB LENCH Tn", "BARNSLEY CB", "ABERGAVENNY Ldsp", "ABERDEEN CITY ScoCofC"). Stripped
# (case-sensitively for the codes, case-insensitively for the words) and LOOPED so double suffixes reduce
# to the bare place name. NB URBAN/RURAL are kept — they are part of district names, not status codes.
TYPE_WORDS = ["Hundred", "Wapentake", "Ward", "Rape", "Lathe", "Liberty", "Liberties", "Soke", "Barony",
              "Lordship", "Division", "Borough", "Municipal Borough", "County Borough", "City", "County",
              "Township", "Chapelry", "Hamlet", "Tything", "Tithing", "Parish", "Franchise"]
TYPE_CODES = ["ScoCofC", "Ldsp", "Wap", "Bny", "CntyOfIt", "AncTn", "Cantref", "Cmt", "CnqPt", "Met.C",
              "Srom", "PaBu", "RoBu", "PoBu", "BuBa", "BuRe", "LBu", "SBu", "UBu", "Wrd", "MB", "CB",
              "UA", "Tn", "Tg", "Wd", "Fh", "Div"]
_STRIP_W = re.compile(r"\s+(?:" + "|".join(sorted(map(re.escape, TYPE_WORDS), key=len, reverse=True)) + r")$", re.I)
_STRIP_C = re.compile(r"\s+(?:" + "|".join(sorted(map(re.escape, TYPE_CODES), key=len, reverse=True)) + r")$")
def _strip_suffix(s):
    prev = None
    while s != prev:
        prev = s
        s = _STRIP_C.sub("", s); s = _STRIP_W.sub("", s)
    return s

def _opener():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                     urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    op.addheaders = [("User-Agent", UA)]
    return op

def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

def _total(html):
    m = re.search(r"There (?:are|is) (\d+)", _text(html))
    return int(m.group(1)) if m else 0

def _names(html):
    return [re.sub(r"&amp;", "&", n).strip()
            for _id, n in re.findall(r'/unit/(\d+)"[^>]*>\s*([^<]+?)\s*</a>', html)]

def _clean(name):
    base = _strip_suffix(name).strip()
    base = base.split("(")[0].strip()
    return base if 2 <= len(base) <= 48 and any(c.isalpha() for c in base) else None

def fetch_kind(op, kind):
    # POST establishes the session result set; returns (total, first-page html)
    data = urllib.parse.urlencode({"kind": kind, "pname": "*", "sdx": "N", "region": "NULL"}).encode()
    req = urllib.request.Request(BASE + "/matches", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    html = op.open(req, timeout=90).read().decode("utf-8", "replace")
    total = _total(html)
    got = list(_names(html)); start = PAGE
    capped = False
    while start < total:
        if len(got) >= CAP:
            capped = True; break
        time.sleep(SLEEP)
        url = BASE + f"/match_any.jsp?flag=1&pname=*&sdx=N&start={start}"
        html = op.open(urllib.request.Request(url), timeout=90).read().decode("utf-8", "replace")
        page = _names(html)
        if not page: break
        got += page; start += PAGE
    return total, got, capped

def main():
    res = {}
    for face, kinds in FACE_KINDS.items():
        seen = {}                                    # lower -> original (first wins)
        for kind in kinds:
            op = _opener()                           # fresh session per kind (result set is per-session)
            try:
                total, names, capped = fetch_kind(op, kind)
            except Exception as e:
                print(f"  {face:14} {kind:12} ERROR {type(e).__name__} {str(e)[:70]}", flush=True); continue
            added = 0
            for n in names:
                c = _clean(n)
                if c and c.lower() not in seen:
                    seen[c.lower()] = c; added += 1
            flag = "  [CAPPED at %d — %d of %d fetched]" % (CAP, len(names), total) if capped else ""
            print(f"  {face:14} {kind:12} total={total:<5} kept+{added:<5} run={len(seen)}{flag}", flush=True)
            time.sleep(SLEEP)
        res[face] = sorted(seen.values())
        print(f"{face}: {len(res[face])} unique names\n", flush=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(res, open(OUT, "w"), ensure_ascii=False, indent=0)
    print(f"wrote {OUT}: {sum(len(v) for v in res.values())} names across {len(res)} faces", flush=True)

if __name__ == "__main__":
    main()
