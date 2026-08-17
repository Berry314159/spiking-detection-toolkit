"""
spikelex.py — Lexicon and tiered case-ascertainment rules for non-consensual
drugging ("drink spiking") in US public police / CAD / EMS open data.

WHY THIS EXISTS
---------------
There is no offense code for non-consensual drugging in NIBRS. Adulterating
food or drink falls into `90Z All Other Offenses`. The only structured
primitives that carry signal are:

  * NIBRS Data Element 13 (Type Weapon/Force Involved):
        50 = Poison   70 = Drugs/Narcotics/Sleeping Pills
  * NIBRS Data Element 9  (Location Type):
        03 Bar/Nightclub, 21 Restaurant, 52 School-College/University,
        20 Residence/Home, 18 Parking Lot/Garage
  * NIBRS Data Element 8  (Offender Suspected of Using): A / C / D / N

Because no victim-level drug flag exists, ascertainment must combine
free-text (narrative, call type, modus operandi) with those structured
proxies. This module encodes that as an explicit, auditable, tiered screen
with per-tier precision weights, so downstream counts are *expected* case
counts rather than false-precision integers.

CALIBRATE BEFORE YOU TRUST. The default TIER_PRECISION values are priors
taken from the published regex-on-narrative benchmark (precision 0.94 /
recall 0.93 for hand-built regex on police narratives) and from the
proportion-of-DFSA-that-is-covert literature (surreptitious-only DFSA
4.2%, 95% CI 0.89-7.44; Beynon review ~2% proven covert). Replace them
with your own hand-labelled sample of >=300 records per tier per city.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 1. LEXICON
# ---------------------------------------------------------------------------
# Ordered most-specific -> least-specific. Each entry is a raw regex fragment
# applied case-insensitively with word-ish boundaries added by compile_terms().

# Tier A1: explicit spiking language. Near-unambiguous.
TERMS_EXPLICIT = [
    r"spik(?:e|ed|ing)\s+(?:my|her|his|their|the|a)?\s*(?:drink|beer|wine|cocktail|shot|water|bottle|cup|glass|soda|food)",
    r"(?:drink|beer|wine|cocktail|shot|water|bottle|cup|glass|soda|food)\s+(?:was\s+|were\s+|got\s+)?spiked",
    r"drink\s*spik\w*",
    r"food\s*spik\w*",
    r"needle\s*spik\w*",
    r"drugg(?:ed|ing)\s+(?:me|her|him|them|the\s+victim|without)",
    r"(?:was|been|got)\s+drugged",
    r"believes?\s+(?:she|he|they|victim)\s+(?:was|were)\s+drugged",
    r"suspects?\s+(?:she|he|they|victim)\s+(?:was|were)\s+drugged",
    r"slipped\s+(?:something|a\s+\w+|drugs?|a\s+pill|a\s+mickey)\s+(?:in|into)",
    r"put\s+something\s+in\s+(?:my|her|his|their|the)\s*(?:drink|beer|cup|glass|water)",
    r"something\s+(?:was\s+)?put\s+in\s+(?:my|her|his|their|the)\s*drink",
    r"unknowingly\s+ingested",
    r"involuntar\w*\s+(?:drugg\w+|intoxicat\w+)",
    r"adulterat\w+\s+(?:food|drink|beverage)",
    r"tamper\w*\s+with\s+(?:my|her|his|their|the)?\s*(?:drink|beverage|food)",
    r"roofie[ds]?",
    r"roofied",
    r"mickey\s+finn",
]

# Tier A2: named agents. Strong, but must co-occur with a victim/ingestion cue
# because narcotics-possession reports also mention these drugs constantly.
TERMS_AGENT = [
    r"\bghb\b",
    r"gamma[\s-]?hydroxybutyrate",
    r"\bgbl\b",
    r"rohypnol",
    r"flunitrazepam",
    r"ketamine",
    r"special\s+k\b",
    r"scopolamine",
    r"burundanga",
    r"devils?\s+breath",
    r"benzodiazepine",
    r"\bxanax\b",
    r"alprazolam",
    r"clonazepam",
    r"\bambien\b",
    r"zolpidem",
    r"chloral\s+hydrate",
    r"date[\s-]?rape\s+drug",
    r"knock[\s-]?out\s+(?:drug|pill|drop)",
]

# Ingestion / victimisation cue required to promote a Tier A2 agent hit.
TERMS_INGEST_CUE = [
    r"\bdrink\b", r"\bdrank\b", r"\bbeverage\b", r"\bcocktail\b", r"\bbeer\b",
    r"\bwine\b", r"\bshot\b", r"\bglass\b", r"\bcup\b", r"\bbottle\b",
    r"\bingest\w*\b", r"\bconsumed?\b", r"\bunknowing\w*\b", r"\bwithout\s+(?:her|his|their|my)\s+(?:knowledge|consent)\b",
    r"\bvictim\b", r"\bunconscious\b", r"\bblack(?:ed)?\s*out\b", r"\bpassed\s+out\b",
]

# Tier B: incapacitation phrasing. Suggestive, not diagnostic. Requires a
# nightlife/campus premise or a nightlife time window to count.
TERMS_INCAPACITATION = [
    r"black(?:ed)?\s*out",
    r"lost\s+(?:consciousness|time|all\s+memory)",
    r"(?:no|zero|little)\s+(?:memory|recollection)\s+of",
    r"cannot\s+remember\s+(?:anything|how|what)",
    r"woke\s+up\s+(?:in|at|next\s+to|and)",
    r"came\s+to\s+(?:in|at)\s+",
    r"found\s+(?:her|him|them)\s*(?:self|selves)?\s+(?:in|at)",
    r"unresponsive",
    r"unconscious",
    r"disoriented",
    r"altered\s+(?:mental\s+status|loc)\b",
    r"only\s+(?:had|drank)\s+(?:one|two|1|2)\s+drinks?",
    r"more\s+intoxicated\s+than",
    r"effects?\s+(?:did\s+not|didn'?t)\s+match",
    r"unable\s+to\s+consent",
    r"too\s+intoxicated\s+to\s+consent",
    r"incapacitat\w+",
]

# Tier C: drug-facilitated predatory-theft pattern (the NYC Manhattan cluster
# signature: victim drugged at a bar, phone/wallet/crypto emptied later).
TERMS_DFTHEFT = [
    r"(?:phone|wallet|purse|card|watch|iphone)\s+(?:was\s+)?(?:missing|gone|taken|stolen)",
    r"unauthoriz\w+\s+(?:charges?|transactions?|transfers?|zelle|venmo|cash\s*app)",
    r"face\s*id",
    r"crypto\w*\s+(?:wallet|account)",
    r"apple\s*pay",
]

# Negation / exclusion patterns. Applied to the WHOLE record text to kill
# obvious false positives before scoring.
TERMS_EXCLUDE = [
    r"\bno\s+(?:evidence|indication|signs?)\s+of\s+(?:drugging|spiking)",
    r"(?:denies|denied)\s+(?:being\s+)?drugged",
    r"unfounded",
    r"determined\s+to\s+be\s+false",
    r"false\s+report",
    r"training\s+(?:exercise|scenario)",
    r"spike\s+strips?",
    r"railroad\s+spike",
    r"volleyball",
    r"spiked\s+(?:heel|hair|fence|collar|club)",
    r"ketamine\s+(?:administered|given)\s+by\s+(?:ems|medic|paramedic|fire)",  # therapeutic
    r"prescri\w+\s+(?:as\s+)?(?:directed|xanax|ambien|clonazepam)",
]

# ---------------------------------------------------------------------------
# 2. STRUCTURED-FIELD VOCABULARIES
# ---------------------------------------------------------------------------
# CAD call types / offense descriptions that plausibly carry spiking cases.
# Keys are semantic buckets; values are lowercase substrings to match against
# whatever the local `call_type` / `offense_description` / `nibrs_desc` is.
CALLTYPE_BUCKETS: Dict[str, Tuple[str, ...]] = {
    "sexoffense": (
        "rape", "sexual assault", "sex offense", "sexual abuse", "sodomy",
        "sex asslt", "sexual battery", "aggravated sexual",
    ),
    "poisoning": (
        "overdose", "od ", "poison", "ingestion", "drug reaction",
        "unknown medical", "narcotics overdose", "opiate",
    ),
    "subject_down": (
        "subject down", "person down", "man down", "unconscious",
        "unresponsive", "check welfare", "welfare check", "intoxicated person",
        "drunk in public", "public intoxication", "sick person",
    ),
    "theft_person": (
        "robbery", "theft from person", "larceny from person", "pickpocket",
        "credit card abuse", "fraud", "unauthorized use",
    ),
    "assist_ems": ("ems assist", "medical assist", "ambulance", "aid response"),
    "liquor": ("liquor", "abc violation", "alcohol violation", "bar check"),
}

# Premise / location-type values that indicate a nightlife or campus setting.
# Matched as lowercase substrings against `premise` / `PREM_TYP_DESC` /
# `location_description`.
PREMISE_NIGHTLIFE = (
    "bar", "tavern", "night club", "nightclub", "night-club", "cocktail",
    "lounge", "brewery", "brewpub", "pub", "cabaret", "discotheque", "club",
)
PREMISE_RESTAURANT = ("restaurant", "eating", "diner", "cafe", "food")
PREMISE_CAMPUS = (
    "college", "university", "campus", "school - college", "dormitory", "dorm",
    "fraternity", "sorority", "greek", "student", "residence hall",
)
PREMISE_RESIDENCE = (
    "residence", "apartment", "single family", "multi family", "home",
    "condo", "house", "hotel", "motel", "airbnb", "short term rental",
)
PREMISE_TRANSIT = ("taxi", "rideshare", "uber", "lyft", "bus", "rail", "train", "station")

# NIBRS code shortcuts (for jurisdictions that publish raw NIBRS elements).
NIBRS_WEAPON_DRUG = {"50", "70", "50 ", "70 "}          # Poison / Drugs-Narcotics-Sleeping Pills
NIBRS_LOCTYPE_NIGHTLIFE = {"03"}                          # Bar/Nightclub
NIBRS_LOCTYPE_RESTAURANT = {"21"}
NIBRS_LOCTYPE_CAMPUS = {"52"}
NIBRS_LOCTYPE_AMBIGUOUS = {"09", "25"}                    # hospital/dr office; other-unknown
                                                           # -> geocode-displacement diagnostics

# Addresses that almost always mean "reported here", not "happened here".
# Extend per city. Hospital + police-station exclusion is mandatory: the
# reporting-pathway evidence (ED-recorded spiking cases outnumbering
# police-recorded ones roughly 25:1 in the Wrexham catchment study) means a
# large share of geocoded points land on the facility, not the venue.
PROVENANCE_AMBIGUOUS_HINTS = (
    "hospital", "medical center", "medical centre", "emergency room", " er ",
    "emergency dept", "health center", "clinic", "urgent care", "trauma",
    "police", "public safety", "sheriff", "precinct", "headquarters",
    "hq", "detention", "jail", "courthouse", "advocacy center", "sane",
    "sexual assault nurse",
)

# ---------------------------------------------------------------------------
# 3. TIME WINDOW
# ---------------------------------------------------------------------------
# 82% of ED-presenting suspected-spiking cases in the Wrexham series occurred
# on a weekend and 66% between 22:00 and 03:00. Use that as the nightlife
# window, but allow a wider "discovery" window because the mean delay from
# incident to ED presentation was ~5.9 hours (London series) and up to 20
# hours to sample collection.
NIGHTLIFE_HOURS = set(list(range(21, 24)) + list(range(0, 5)))   # 21:00-04:59
DISCOVERY_HOURS = set(list(range(21, 24)) + list(range(0, 12)))  # allows delayed report
WEEKEND_DAYS = {4, 5, 6}    # Fri, Sat, Sun (Python weekday(): Mon=0)


# ---------------------------------------------------------------------------
# 4. COMPILED MATCHERS
# ---------------------------------------------------------------------------
def compile_terms(terms: Sequence[str]) -> re.Pattern:
    """Compile a term list into one case-insensitive alternation pattern."""
    return re.compile("|".join(f"(?:{t})" for t in terms), re.IGNORECASE)


RX_EXPLICIT = compile_terms(TERMS_EXPLICIT)
RX_AGENT = compile_terms(TERMS_AGENT)
RX_INGEST = compile_terms(TERMS_INGEST_CUE)
RX_INCAP = compile_terms(TERMS_INCAPACITATION)
RX_DFTHEFT = compile_terms(TERMS_DFTHEFT)
RX_EXCLUDE = compile_terms(TERMS_EXCLUDE)


def _has(rx: re.Pattern, text: str) -> bool:
    return bool(text) and rx.search(text) is not None


def _bucket_of(text: str) -> Optional[str]:
    t = (text or "").lower()
    for bucket, needles in CALLTYPE_BUCKETS.items():
        if any(n in t for n in needles):
            return bucket
    return None


def _premise_class(text: str) -> str:
    t = (text or "").lower()
    if any(n in t for n in PREMISE_NIGHTLIFE):
        return "nightlife"
    if any(n in t for n in PREMISE_CAMPUS):
        return "campus"
    if any(n in t for n in PREMISE_RESTAURANT):
        return "restaurant"
    if any(n in t for n in PREMISE_TRANSIT):
        return "transit"
    if any(n in t for n in PREMISE_RESIDENCE):
        return "residence"
    return "other"


# ---------------------------------------------------------------------------
# 5. TIER DEFINITIONS AND PRECISION PRIORS
# ---------------------------------------------------------------------------
# pi_t = P(record is a true non-consensual drugging | record fired tier t).
# These are PRIORS. Overwrite from your labelled sample.
TIER_PRECISION: Dict[str, float] = {
    "A1_explicit_text":        0.90,
    "A2_agent_plus_cue":       0.70,
    "B1_nibrs_drug_weapon":    0.55,
    "B2_incap_nightlife":      0.22,
    "B3_dftheft_nightlife":    0.30,
    "C1_poisoning_nightlife":  0.10,
    "C2_subjectdown_nightlife": 0.04,
}

# Tier ordering: a record is assigned to the single highest tier it satisfies,
# so expected counts never double-count one incident.
TIER_ORDER: List[str] = [
    "A1_explicit_text",
    "A2_agent_plus_cue",
    "B1_nibrs_drug_weapon",
    "B3_dftheft_nightlife",
    "B2_incap_nightlife",
    "C1_poisoning_nightlife",
    "C2_subjectdown_nightlife",
]


@dataclass
class ScreenResult:
    """Outcome of applying the tiered screen to one record."""
    tier: Optional[str]
    pi: float                       # expected-case weight in [0, 1]
    premise_class: str
    call_bucket: Optional[str]
    nightlife_time: bool
    provenance_ambiguous: bool
    matched: List[str] = field(default_factory=list)

    @property
    def is_case(self) -> bool:
        return self.tier is not None


def screen_record(
    *,
    narrative: str = "",
    call_type: str = "",
    offense_desc: str = "",
    mo: str = "",
    premise: str = "",
    address: str = "",
    hour: Optional[int] = None,
    weekday: Optional[int] = None,
    nibrs_weapon: str = "",
    nibrs_loctype: str = "",
    tier_precision: Optional[Dict[str, float]] = None,
) -> ScreenResult:
    """
    Apply the tiered spiking screen to a single record.

    All text arguments are optional -- pass whatever the jurisdiction actually
    publishes. Very few US agencies publish narratives (Boulder's
    `auto_narrative` is the notable exception), so in most cities the screen
    will run on call_type + offense_desc + mo + premise alone and will
    therefore only reach tiers B and C. That is expected: the resulting
    expected-case counts are small but the tier weights keep them honest.

    Returns a ScreenResult with the single highest tier satisfied.
    """
    pi_map = tier_precision or TIER_PRECISION
    text_all = " || ".join(x for x in (narrative, call_type, offense_desc, mo) if x)
    text_lower = text_all.lower()

    prem = _premise_class(premise or address)
    bucket = _bucket_of(f"{call_type} {offense_desc}")
    nl_time = hour is not None and hour in NIGHTLIFE_HOURS
    prov_amb = any(h in (address or "").lower() for h in PROVENANCE_AMBIGUOUS_HINTS) or (
        str(nibrs_loctype).strip() in NIBRS_LOCTYPE_AMBIGUOUS
    )
    nightlife_ctx = prem in ("nightlife", "campus", "restaurant") or nl_time

    matched: List[str] = []

    # Hard exclusions first.
    if _has(RX_EXCLUDE, text_lower):
        return ScreenResult(None, 0.0, prem, bucket, nl_time, prov_amb, ["excluded"])

    fired: List[str] = []

    if _has(RX_EXPLICIT, text_all):
        fired.append("A1_explicit_text")
        matched.append("explicit")

    if _has(RX_AGENT, text_all) and _has(RX_INGEST, text_all):
        fired.append("A2_agent_plus_cue")
        matched.append("agent+cue")

    if str(nibrs_weapon).strip() in NIBRS_WEAPON_DRUG:
        fired.append("B1_nibrs_drug_weapon")
        matched.append(f"weapon={nibrs_weapon}")

    if _has(RX_DFTHEFT, text_all) and (
        bucket == "theft_person" or prem == "nightlife"
    ) and nightlife_ctx:
        fired.append("B3_dftheft_nightlife")
        matched.append("df-theft")

    if _has(RX_INCAP, text_all) and nightlife_ctx:
        fired.append("B2_incap_nightlife")
        matched.append("incapacitation")

    if bucket == "poisoning" and nightlife_ctx:
        fired.append("C1_poisoning_nightlife")
        matched.append("poisoning@nightlife")

    if bucket in ("subject_down", "assist_ems") and prem in ("nightlife", "campus") and nl_time:
        fired.append("C2_subjectdown_nightlife")
        matched.append("down@nightlife")

    # Sex-offence records at nightlife/campus premises are promoted one notch:
    # they are the highest-consequence subset and the covert-DFSA base rate
    # applies to them specifically.
    if bucket == "sexoffense" and nightlife_ctx and not fired:
        fired.append("C1_poisoning_nightlife")
        matched.append("sexoffense@nightlife")

    tier = next((t for t in TIER_ORDER if t in fired), None)
    pi = pi_map.get(tier, 0.0) if tier else 0.0

    # Provenance-ambiguous addresses keep their case weight but are flagged;
    # the geography step must decide whether to drop or reallocate them.
    return ScreenResult(tier, pi, prem, bucket, nl_time, prov_amb, matched)


def screen_frame(df, colmap: Dict[str, str], tier_precision: Optional[Dict] = None):
    """
    Vectorised-ish wrapper: apply screen_record row-wise to a pandas DataFrame.

    `colmap` maps screen_record kwarg -> dataframe column name, e.g.
        {"call_type": "CallType", "address": "Address", "hour": "_hour"}
    Missing keys are simply not passed.
    """
    import pandas as pd  # local import so the lexicon stays dependency-free

    def _row(r):
        kw = {}
        for arg, col in colmap.items():
            if col in r.index:
                v = r[col]
                kw[arg] = v if (arg in ("hour", "weekday")) else ("" if pd.isna(v) else str(v))
        if kw.get("hour") is not None and not isinstance(kw.get("hour"), (int, type(None))):
            try:
                kw["hour"] = int(kw["hour"])
            except (TypeError, ValueError):
                kw["hour"] = None
        res = screen_record(tier_precision=tier_precision, **kw)
        return pd.Series({
            "spike_tier": res.tier,
            "spike_pi": res.pi,
            "premise_class": res.premise_class,
            "call_bucket": res.call_bucket,
            "nightlife_time": res.nightlife_time,
            "provenance_ambiguous": res.provenance_ambiguous,
            "spike_matched": ",".join(res.matched),
        })

    return df.join(df.apply(_row, axis=1))
