"""GB-STAMP typing v2 — probabilistic top-3 type assignment per label.

Combines the signals proven in the font-typing R&D (developer/plan-gb1900-typing.md §12):
 - tier-0 checked abbreviations (F.P.->footpath, B.M.->benchmark, ...) — high confidence;
 - numerals (spot-heights); road suffixes; antiquity terms (validated blackletter font, .96);
 - water / descriptive / named-place word-semantics (the serif upright/italic axis, .71-.86);
 - allcaps multiword -> admin/parish (spaced-caps font, .98); settlement-name gazetteer match.
Size/font-style are NOT available full-corpus (no bbox; crops only for sampled regions), so v2 types
from text + tier0_rule + case — the font work validated these mappings and is layered in as a targeted
refinement where crops exist.

`assign_types(text, tier0_rule, allcaps, settlement_names) -> [(type, prob), ...]` top-3, descending,
as a normalised distribution so consumers can pick types[0] as the best guess.
"""
from __future__ import annotations
import re
from collections import defaultdict

# checked abbreviations, matched on a NORMALISED form (dots/spaces/case/trailing-plural stripped)
# so "F.P", "F.P.", "FP", "F.Ps" all match. -> (type, confidence).
ABBREV_N = {
    "FP": ("footpath", .95), "FB": ("footbridge", .95), "BM": ("benchmark", .95),
    "SP": ("signpost", .9), "GP": ("guidepost", .9), "BS": ("boundary_stone", .9),
    "MS": ("milestone", .9), "MP": ("milepost", .9), "BP": ("boundary_post", .85),
    "LB": ("letter_box", .85), "PO": ("post_office", .9), "PH": ("public_house", .9),
    "PC": ("post_office", .6), "PP": ("pump", .7), "SB": ("signal_box", .6),
    "W": ("well", .85), "P": ("pump", .8), "CH": ("church", .8), "CHAP": ("chapel", .85),
    "SCH": ("school", .9), "SMY": ("smithy", .85), "SM": ("smithy", .8), "MON": ("monument", .85),
    "FM": ("farm", .85), "HO": ("house", .8), "STA": ("station", .7), "SPRS": ("spring", .8),
    "TP": ("signpost", .6), "GPO": ("guidepost", .7), "WS": ("well", .8), "PS": ("pump", .8),
    "VIC": ("vicarage", .85), "FS": ("flagstaff", .6), "SPR": ("spring", .8),
}
BOUNDARY = re.compile(r"\b(By\.?|Bdy|Boundary|Bound\.|Co\. ?Div|Div\.|R\.D\.|U\.D\.|C\.?P\.?|Par\.? ?Bdy|Ward Bdy|"
                      r"Detached|Union|Wapentake|Hundred|Liberty|Twp)\b")
TIDAL = re.compile(r"\b(H\.?W\.?M|L\.?W\.?M|High Water|Low Water|Mean High|Ordinary Tides|Saltings|Foreshore|Mud|Sand)\b", re.I)
ABBREV = {}  # legacy name kept; see ABBREV_N
KEYWORD = {  # substring keyword (tier0 keyword rule) -> (type, conf)
    "church": ("church", .88), "chapel": ("chapel", .85), "chap.": ("chapel", .82),
    "mission": ("chapel", .78), "school": ("school", .88),
    "mill": ("mill", .82), "farm": ("farm", .82), "bridge": ("bridge", .85), "quarry": ("quarry", .85),
    "colliery": ("colliery", .88), "works": ("works", .78), "smithy": ("smithy", .82),
    "brewery": ("brewery", .85), "inn": ("public_house", .8), "hotel": ("hotel", .82),
    "station": ("station", .8), "reservoir": ("water_feature", .82), "cemetery": ("cemetery", .88),
    "hospital": ("hospital", .85), "barracks": ("barracks", .88), "wharf": ("wharf", .82),
    "vicarage": ("vicarage", .88), "rectory": ("rectory", .88), "manse": ("clergy_house", .82),
    "parsonage": ("clergy_house", .85), "sheepfold": ("fold", .85), "tramway": ("tramway", .85),
    "kennel": ("kennels", .82), "limekiln": ("limekiln", .85), "lime kiln": ("limekiln", .85),
    "ice house": ("icehouse", .82), "icehouse": ("icehouse", .85), "cistern": ("water_feature", .8),
    "windpump": ("water_feature", .8), "aqueduct": ("aqueduct", .85), "viaduct": ("viaduct", .88),
    "tunnel": ("tunnel", .85), "pier": ("pier", .82), "cave": ("cave", .85), "pavilion": ("pavilion", .8),
    "rifle range": ("rifle_range", .88), "cricket": ("recreation_ground", .8),
    "recreation": ("recreation_ground", .82), "pheasantry": ("pheasantry", .85),
    "almshouse": ("almshouses", .85), "goods shed": ("railway_building", .82),
    "towing path": ("towpath", .88), "tow path": ("towpath", .88), "stepping stones": ("ford", .82),
    "flag staff": ("flagstaff", .82), "flagstaff": ("flagstaff", .85), "boat house": ("boathouse", .82),
    "lifeboat": ("lifeboat_station", .85), "coastguard": ("coastguard", .85),
    "liable to flood": ("flood_land", .8), "waterfall": ("water_feature", .82),
    "jetty": ("jetty", .85), "breakwater": ("breakwater", .85), "landing stage": ("landing_stage", .85),
    "ferry": ("ferry", .82), "filter bed": ("waterworks", .82), "hydraulic ram": ("waterworks", .82),
    "gasometer": ("gasworks", .85), "gas works": ("gasworks", .85), "malthouse": ("works", .8),
    "malt house": ("works", .8), "brick field": ("brickworks", .82), "brickworks": ("brickworks", .85),
    "brick works": ("brickworks", .85), "reading room": ("reading_room", .82), "pound": ("pound", .72),
    "boat ho": ("boathouse", .82), "shake hole": ("natural_feature", .8), "swallow hole": ("natural_feature", .82),
    "golf": ("recreation_ground", .82), "football": ("recreation_ground", .82), "beacon": ("beacon", .78),
    "sunday sch": ("school", .85), "sun. sch": ("school", .85), "board sch": ("school", .85),
    "gravel pit": ("quarry_or_mine", .85), "sand pit": ("quarry_or_mine", .85),
}
TREE = {"oak", "ash", "elm", "beech", "yew", "tree", "trees", "poplar", "sycamore", "chestnut", "thorn"}
STONE = {"stone", "stones"}
ANTIQ = re.compile(r"\b(Tumulus|Tumuli|Cairn|Cairns|Camp|Earthwork|Earthworks|Barrow|Barrows|Motte|"
                   r"Cist|Enclosure|Entrenchment|Cross|Castle|Abbey|Priory|Roman|British|Saxon|Cromlech|"
                   r"Dolmen|Menhir|Cromlechs|Rath|Dun|Fogou|Souterrain|Hillfort|Chambered|Tumular|"
                   r"Standing Stone|Stone Circle|Hut Circle|Hut Circles|Stone Row|Kistvaen|Fort|Forts|"
                   r"\(Site of\)|\(Remains of\)|Battlefield|Moat|Antiquities|Sepulchral|Currick|Beacon Hill|"
                   r"Old Church|Chambered Cairn|Long Barrow|Round Barrow|Cup Marked|Inscribed Stone)\b", re.I)
ROAD = re.compile(r"\b(ROAD|STREET|LANE|TERRACE|AVENUE|WAY|WALK|ROW)\b", re.I)
WATER = {"well", "spring", "springs", "ford", "weir", "brook", "pond", "ponds", "pool", "marsh", "moss",
         "river", "canal", "reservoir", "drain", "sluice", "lake", "mere", "burn", "beck", "dam",
         "waterfall", "tank", "sinks", "rises", "issues", "culvert", "fountain", "trough", "lock"}
MINE = {"quarry", "quarries", "shaft", "shafts", "pit", "pits", "spoil", "level", "adit", "mine",
        "mines", "colliery", "gravel", "clay", "sandpit"}
DESCRIPTIVE = {"house", "cottage", "cottages", "hall", "lodge", "grange", "barn", "villa", "pit",
               "shaft", "works", "smithy", "brewery", "kiln", "forge", "foundry", "yard", "green"}
NAMED_PLACE = {"coppice", "plantation", "nursery", "nurseries", "firs", "covert", "gorse", "belt",
               "spinney", "wood", "grove", "common", "moor", "heath", "park"}

def _abbrev_lookup(t):
    x = re.sub(r"[.\s]", "", t).upper()
    if x in ABBREV_N: return ABBREV_N[x]                 # full form first (BS=boundary_stone, MS=milestone)
    if len(x) > 2 and x.endswith("S") and x[:-1] in ABBREV_N:  # then plural (FPs->FP)
        return ABBREV_N[x[:-1]]
    return None

def assign_types(text, tier0_rule=None, allcaps=False, settlement_names=None):
    t = (text or "").strip(); low = t.lower(); al = [c for c in t if c.isalpha()]
    s = defaultdict(float)

    if not t or tier0_rule == "illegible":
        return [("illegible", 1.0)]
    if tier0_rule == "numeric" or (t and re.fullmatch(r"[\d.,\-]+", t)):
        s["spot_height_or_value"] += .92
    if len(al) <= 4:                                   # short marks only (avoid matching words)
        hit = _abbrev_lookup(t)
        if hit: s[hit[0]] += hit[1]
    if TIDAL.search(t): s["tidal_coastal"] += .85
    if BOUNDARY.search(t) and not ROAD.search(t): s["boundary"] += .82
    if ROAD.search(t):
        s["road"] += .9
    if ANTIQ.search(t) and not ROAD.search(t):
        s["antiquity"] += .88
    for kw, (ty, c) in KEYWORD.items():
        if kw in low: s[ty] += c
    words = re.findall(r"[a-z]+", low); head = words[-1] if words else ""   # head noun = last word
    def hn(S):   # plural-aware head-noun membership (fords->ford, weirs->weir, lodges->lodge)
        return head in S or (len(head) > 3 and head.endswith("s") and head[:-1] in S)
    if hn(WATER): s["water_feature"] += .8
    elif any(w in WATER for w in words): s["water_feature"] += .6
    if hn(MINE): s["quarry_or_mine"] += .82
    elif any(w in MINE for w in words): s["quarry_or_mine"] += .6
    if hn(DESCRIPTIVE): s["building_or_feature"] += .72                     # "Bankfield House" -> house
    elif any(w in DESCRIPTIVE for w in words): s["building_or_feature"] += .58
    if hn(NAMED_PLACE): s["named_landcover"] += .72                         # "Oak Coppice" -> coppice
    elif any(w in NAMED_PLACE for w in words): s["named_landcover"] += .58
    if hn(TREE) and len(words) <= 2: s["tree"] += .7                        # "Oak", "Ash Tree", "Oaks"
    if hn(STONE): s["stone_marker"] += .62                                  # Stone/Stones
    if head in ("post", "posts"): s["signpost"] += .6
    if head in ("mound", "mounds", "moat"): s["antiquity"] += .7
    if allcaps and " " in t and len(al) >= 4 and not ROAD.search(t): s["admin_or_parish"] += .78  # spaced caps (font .98)
    elif allcaps and len(al) >= 3: s["settlement"] += .55
    if settlement_names and low in settlement_names:
        s["settlement"] += .6

    if not s:                                          # proper name, no keyword -> serif residual (~.71)
        if al and t[:1].isupper():
            s["settlement"] += .45; s["building_or_feature"] += .35
        else:
            s["unknown"] += .7

    tot = sum(s.values())
    if tot > 1.0:                                      # renormalise co-firing signals
        s = {k: v / tot for k, v in s.items()}; tot = 1.0
    resid = max(0.0, 1.0 - tot)
    if resid > 1e-6:
        s["unknown"] = s.get("unknown", 0.0) + resid
    ranked = sorted(s.items(), key=lambda kv: -kv[1])
    return [(k, round(v, 3)) for k, v in ranked[:3]]
