"""
demo_dallas.py — The full pipeline with a REAL exposure denominator.

Dallas is the strongest US test case because two things line up in one state:

  * Dallas Police incidents (`qv6i-rri7`) publish an exact `incident_address`,
    a `premise` field with a Bar/NightClub value, AND a `mo` (modus operandi)
    free-text field carrying real police prose. That means the Tier-A text
    screen can actually fire, instead of falling back to structured proxies.
  * Texas Comptroller Mixed Beverage Gross Receipts (`naix-2893`) publish
    per-venue monthly liquor/wine/beer/cover-charge receipts keyed to a TABC
    permit and a street address — the only true per-venue exposure denominator
    published anywhere in the US.

Stage 1 runs the text screen citywide via SoQL so the server does the work.
Stage 2 pulls the nightlife-premise subset. Stage 3 builds the receipts
denominator and joins on a normalised address. Stage 4 scores.
"""
from __future__ import annotations

import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelex import screen_frame                                  # noqa: E402
from spikescore import (exposure_receipts, publication_gate,       # noqa: E402
                        score_places)
from spikefetch import socrata                                     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DOM, DSID = "www.dallasopendata.com", "qv6i-rri7"

# SoQL-side text screen. Kept deliberately broad; the Python screen then
# applies the precise regex, negations and tier logic. Pushing the coarse
# filter to the server avoids pulling millions of rows.
MO_LIKE_TERMS = [
    "SPIK", "DRUGGED", "ROOFIE", "GHB", "ROHYPNOL", "KETAMINE",
    "SLIPPED SOMETHING", "PUT SOMETHING IN", "DATE RAPE",
    "UNKNOWN SUBSTANCE IN", "LACED", "TAMPERED WITH",
]


def soql_text_screen() -> pd.DataFrame:
    """Citywide narrative screen, executed server-side."""
    clauses = " OR ".join(f"upper(mo) like '%{t}%'" for t in MO_LIKE_TERMS)
    fields = ("incidentnum,date1,time1,premise,incident_address,offincident,"
              "nibrs_crime,mo,weaponused,drug,victimtype,signal,zip_code")
    return socrata(DOM, DSID, select=fields, where=f"({clauses})",
                   limit=100_000, page=25_000)


def nightlife_subset() -> pd.DataFrame:
    """All incidents recorded at a bar / nightclub premise."""
    fields = ("incidentnum,date1,time1,premise,incident_address,offincident,"
              "nibrs_crime,mo,weaponused,drug,victimtype,signal,zip_code")
    where = "upper(premise) like '%BAR%' OR upper(premise) like '%NIGHT CLUB%'"
    return socrata(DOM, DSID, select=fields, where=where,
                   limit=200_000, page=25_000)


# ---------------------------------------------------------------------------
# Address normalisation for the incident <-> licence join
# ---------------------------------------------------------------------------
SUFFIX = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "ROAD": "RD",
    "DRIVE": "DR", "LANE": "LN", "PARKWAY": "PKWY", "PLACE": "PL",
    "COURT": "CT", "CIRCLE": "CIR", "HIGHWAY": "HWY", "EXPRESSWAY": "EXPY",
    "TRAIL": "TRL", "TERRACE": "TER", "SQUARE": "SQ", "FREEWAY": "FWY",
}
DIRS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
        "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE",
        "SOUTHWEST": "SW"}
UNIT_RX = re.compile(
    r"\b(?:STE|SUITE|APT|UNIT|BLDG|BUILDING|FL|FLOOR|RM|ROOM|#)\b.*$", re.I)


def norm_addr(a: str) -> str:
    """
    Collapse an address to a joinable key: drop unit/suite, standardise
    directionals and street suffixes, strip punctuation.

    Expect roughly 60-75% match rates on a naive key like this. Anything
    published at venue level should be built on a proper geocode-and-match
    workflow instead, held to the conventional >=85% match-rate standard.
    """
    if not isinstance(a, str) or not a.strip():
        return ""
    s = a.upper().strip()
    s = UNIT_RX.sub("", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    toks = [t for t in s.split() if t]
    toks = [DIRS.get(t, t) for t in toks]
    toks = [SUFFIX.get(t, t) for t in toks]
    return " ".join(toks).strip()


def main() -> None:
    # ---- Stage 1: citywide narrative screen ------------------------------
    c1 = os.path.join(HERE, "cache_dallas_text.csv")
    if os.path.exists(c1):
        txt = pd.read_csv(c1, low_memory=False)
    else:
        print("Stage 1: citywide SoQL narrative screen ...", flush=True)
        txt = soql_text_screen()
        txt.to_csv(c1, index=False)
    print(f"Stage 1: {len(txt):,} records whose `mo` text mentions a spiking term")

    c2 = os.path.join(HERE, "cache_dallas_bars.csv")
    if os.path.exists(c2):
        bars = pd.read_csv(c2, low_memory=False)
    else:
        print("Stage 2: nightlife-premise subset ...", flush=True)
        bars = nightlife_subset()
        bars.to_csv(c2, index=False)
    print(f"Stage 2: {len(bars):,} records at a bar / nightclub premise")

    pool = pd.concat([txt, bars], ignore_index=True).drop_duplicates("incidentnum")
    print(f"Combined analysis pool: {len(pool):,} unique incidents")

    pool["hour"] = pd.to_numeric(
        pool["time1"].astype(str).str.extract(r"^(\d{1,2})")[0], errors="coerce")

    screened = screen_frame(pool, colmap={
        "narrative": "mo", "call_type": "signal", "offense_desc": "offincident",
        "mo": "nibrs_crime", "premise": "premise", "address": "incident_address",
        "hour": "hour",
    })
    hits = screened[screened["spike_tier"].notna()].copy()
    print(f"\nScreen fired on {len(hits):,} records; "
          f"expected cases = {hits['spike_pi'].sum():.1f}")
    print(hits["spike_tier"].value_counts().to_string())

    print("\n--- Sample Tier-A narrative matches (the reason Dallas matters) ---")
    ta = hits[hits["spike_tier"].isin(["A1_explicit_text", "A2_agent_plus_cue"])]
    for _, r in ta.head(12).iterrows():
        print(f"  [{r['spike_tier']}] {str(r['premise'])[:28]:28s} | "
              f"{str(r['mo'])[:110]}")
    print(f"  (total Tier-A records: {len(ta):,})")

    print("\n--- Premise mix of Tier-A narrative-confirmed records ---")
    if len(ta):
        pm = ta["premise"].value_counts().head(12)
        print((100 * pm / len(ta)).round(1).to_string())

    screened.to_csv(os.path.join(HERE, "out_dallas_screened.csv"), index=False)

    # ---- Stage 3: receipts denominator -----------------------------------
    c3 = os.path.join(HERE, "cache_tx_receipts_dallas.csv")
    if os.path.exists(c3):
        rec = pd.read_csv(c3, low_memory=False)
    else:
        print("\nStage 3: Texas mixed-beverage receipts for Dallas ...", flush=True)
        rec = socrata(
            "data.texas.gov", "naix-2893",
            select=("location_name,location_address,location_city,tabc_permit_number,"
                    "obligation_end_date_yyyymmdd,liquor_receipts,wine_receipts,"
                    "beer_receipts,cover_charge_receipts,total_receipts"),
            where=("upper(location_city)='DALLAS' AND "
                   "obligation_end_date_yyyymmdd >= '2020-01-01T00:00:00.000'"),
            limit=400_000, page=25_000)
        rec.to_csv(c3, index=False)
    print(f"Stage 3: {len(rec):,} venue-month receipt rows in Dallas")

    for c in ("total_receipts", "cover_charge_receipts"):
        rec[c] = pd.to_numeric(rec[c], errors="coerce").fillna(0.0)
    rec["addr_key"] = rec["location_address"].map(norm_addr)

    ven = (rec.groupby("addr_key")
           .agg(location_name=("location_name", "first"),
                permits=("tabc_permit_number", "nunique"),
                months=("obligation_end_date_yyyymmdd", "nunique"),
                total_receipts=("total_receipts", "sum"),
                cover=("cover_charge_receipts", "sum"))
           .reset_index())
    ven = ven[ven["addr_key"] != ""]
    ven["X_exposure"] = [exposure_receipts(t, c) / 1000.0
                         for t, c in zip(ven["total_receipts"], ven["cover"])]
    print(f"  {len(ven):,} distinct licensed addresses; "
          f"${ven['total_receipts'].sum()/1e9:.2f}B total receipts; "
          f"{(ven['cover'] > 0).mean():.1%} of addresses report a cover charge")

    # ---- Stage 4: join and score -----------------------------------------
    hits["addr_key"] = hits["incident_address"].map(norm_addr)
    agg = (hits.groupby("addr_key")
           .agg(O_expected=("spike_pi", "sum"),
                n_records=("incidentnum", "size"),
                tiers=("spike_tier", lambda s: ",".join(sorted(set(s)))),
                provenance_ambiguous_share=("provenance_ambiguous", "mean"))
           .reset_index())
    agg = agg[agg["addr_key"] != ""]

    joined = agg.merge(ven, on="addr_key", how="inner")
    match_rate = len(joined) / max(len(agg), 1)
    print(f"\nStage 4: {len(joined):,} of {len(agg):,} screened addresses matched a "
          f"licensed premise ({match_rate:.1%} naive-key match rate)")
    print("A match rate in this range is exactly why venue-level publication "
          "needs a real geocode-and-match workflow, not a string key.")

    # Licensed premises with zero screened records still carry exposure and
    # must stay in the denominator, or every rate is biased upward.
    full = ven.merge(agg.drop(columns=["location_name"], errors="ignore"),
                     on="addr_key", how="left")
    full["O_expected"] = full["O_expected"].fillna(0.0)
    full["n_records"] = full["n_records"].fillna(0)
    full["provenance_ambiguous_share"] = full["provenance_ambiguous_share"].fillna(0.0)
    full = full[full["X_exposure"] > 0]
    print(f"Scoring {len(full):,} licensed premises "
          f"({int((full['O_expected'] > 0).sum())} with any screened signal)")

    scored = score_places(full, place_col="addr_key", min_exposure=10.0,
                          min_expected_cases=1.0)
    scored = publication_gate(scored, min_expected=3.0, small_cell=5)
    p = scored.attrs["eb_params"]
    print(f"\nEB prior: alpha={p.alpha:.3f} beta={p.beta:.3f} [{p.method}]; "
          f"theta={scored.attrs['theta_bar']:.6f} expected cases per $1k receipts")

    cols = ["location_name", "addr_key", "n_records", "O_expected", "total_receipts",
            "cover", "X_exposure", "RR_raw", "RR_eb", "RR_eb_lo95", "RR_eb_hi95",
            "SRI", "elevated_fdr05", "publishable", "tiers"]
    scored[cols].to_csv(os.path.join(HERE, "out_dallas_venues.csv"), index=False)

    n_elev = int(scored["elevated_fdr05"].sum())
    n_pub = int(scored["publishable"].sum())
    print(f"{n_elev} premises credibly elevated after FDR; {n_pub} pass the full "
          f"publication gate")

    print("\n--- Top 15 by SRI among premises with real signal ---")
    show = scored[scored["O_expected"] > 0].head(15)[
        ["location_name", "n_records", "O_expected", "total_receipts", "X_exposure",
         "RR_eb", "RR_eb_lo95", "SRI", "elevated_fdr05", "publishable"]]
    print(show.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    print("\nNOTE ON NAMES: the venue names above are printed to demonstrate that "
          "the join works. They are NOT an allegation about any business. Tier "
          "weights are calibrated precisions well below 1.0, an incident address "
          "may be where a victim was found rather than where they were drugged, "
          "and only rows with publishable=True cleared the suppression gate.")


if __name__ == "__main__":
    main()
