"""
demo_tempe.py — End-to-end worked example on live public data.

Tempe AZ Calls for Service (NIBRS reporting period 2022-present) is used
because it is the only large US feed with a per-record venue NAME field
(`PlaceName`), which lets the whole pipeline run without geocoding or a
licence join.

Produces:
  out_tempe_screened.csv     record-level screen output
  out_tempe_venues.csv       venue-level scores (the deliverable)
  out_tempe_provenance.csv   where sex-offence / overdose calls actually land
  out_tempe_concentration.csv concentration curve
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelex import screen_frame                      # noqa: E402
from spikescore import (concentration_curve, exposure_licence,  # noqa: E402
                        fit_gamma_prior, publication_gate, score_places)

LAYER = ("https://services.arcgis.com/lQySeXwbBg53XWDi/arcgis/rest/services/"
         "Calls_For_Service/FeatureServer/0")
UA = {"User-Agent": "spiking-research/1.0"}

# Call types that can plausibly carry a non-consensual drugging event.
TARGET_CALLTYPES = [
    "Rape Call", "Sexual Abuse Call", "Overdose Call", "Subject Down Call",
    "Robbery Call", "Drunk Disturbing Call", "Liquor Violation Call",
    "Injured/Sick Person Call", "Drugs Call", "Theft Call", "Fraud Call",
    "Unknown Trouble Call", "Check Welfare Call", "Fight Call", "Assault Call",
]


def _q(params: dict) -> dict:
    url = LAYER + "/query?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def fetch_records() -> pd.DataFrame:
    """Row-level pull of the target call types that carry a PlaceName."""
    quoted = ",".join("'" + t.replace("'", "''") + "'" for t in TARGET_CALLTYPES)
    where = (f"CallType IN ({quoted}) AND PlaceName IS NOT NULL "
             f"AND PlaceName <> ''")
    fields = ("PrimaryKey,OccurrenceDatetime,OccurrenceHour,OccurrenceWeekday,"
              "ObfuscatedAddress,PlaceName,CallType,FinalCaseTypeTrans,"
              "InitialCaseTypeTrans,CallCategory,Latitude,Longitude,"
              "CensusTractID,NeighborhoodName")
    frames, offset, page = [], 0, 2000
    while True:
        payload = _q({"where": where, "outFields": fields, "returnGeometry": "false",
                      "f": "json", "resultOffset": offset, "resultRecordCount": page,
                      "orderByFields": "PrimaryKey"})
        feats = payload.get("features", [])
        if not feats:
            break
        frames.append(pd.DataFrame([f["attributes"] for f in feats]))
        offset += len(feats)
        print(f"  fetched {offset:,}", flush=True)
        if len(feats) < page:
            break
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_place_volume() -> pd.DataFrame:
    """
    Total call volume per PlaceName across ALL call types, used as an ambient
    activity denominator.

    This is exposure tier (d) -- the weakest one -- and it is endogenous:
    a venue that generates many police calls of any kind will have a larger
    denominator, which biases its rate DOWNWARD. Where a real capacity or
    receipts denominator exists (Texas mixed-beverage receipts being the only
    true per-venue US example), use that instead.
    """
    frames, offset, page = [], 0, 2000
    while True:
        payload = _q({
            "where": "PlaceName IS NOT NULL AND PlaceName <> ''",
            "groupByFieldsForStatistics": "PlaceName",
            "outStatistics": json.dumps([{"statisticType": "count",
                                          "onStatisticField": "OBJECTID",
                                          "outStatisticFieldName": "total_calls"}]),
            "orderByFields": "PlaceName", "f": "json",
            "resultOffset": offset, "resultRecordCount": page,
        })
        feats = payload.get("features", [])
        if not feats:
            break
        frames.append(pd.DataFrame([f["attributes"] for f in feats]))
        offset += len(feats)
        if len(feats) < page:
            break
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# Venue-type classification from the PlaceName string
# ---------------------------------------------------------------------------
VENUE_RULES = [
    ("hospital_or_clinic", ("hospital", "medical center", "emergency", "clinic",
                            "urgent care", "health center", "banner ", "dignity health",
                            "mayo clinic", "abrazo")),
    ("police_or_justice", ("police", "public safety", "detention", "jail", "court",
                           "city of tempe police", "sheriff")),
    ("bar_or_nightclub", ("bar", "tavern", "nightclub", "night club", "lounge",
                          "pub", "cantina", "saloon", "brewing", "brewery",
                          "taproom", "beer garden", "cocktail", "club ")),
    ("campus_greek", ("fraternity", "sorority", "greek", " phi ", " sigma ",
                      " kappa ", " delta ", " alpha ", " omega ", " theta ")),
    ("campus", ("arizona state", "asu ", " asu", "university", "college",
                "residence hall", "dormitory", "student", "memorial union",
                "sun devil", "campus")),
    ("hotel_motel", ("hotel", "motel", "inn", "suites", "resort", "lodge",
                     "airbnb", "hostel")),
    ("apartment_residence", ("apartment", "apts", "residence", "villas", "commons",
                             "manor", "terrace", "mobile home", "trailer park",
                             "condominium", "housing")),
    ("restaurant_fastfood", ("restaurant", "grill", "pizza", "cafe", "coffee",
                             "diner", "kitchen", "taco", "burger", "sushi",
                             "steakhouse", "eatery", "starbucks", "mcdonald",
                             "chipotle", "subway ")),
    ("retail_grocery", ("walmart", "target", "circle k", "qt", "quiktrip", "cvs",
                        "walgreen", "fry's", "safeway", "grocery", "market",
                        "mall", "store", "shell", "chevron", "gas ", "7-eleven",
                        "dollar")),
    ("transit_transport", ("light rail", "station", "bus ", "transit", "airport",
                           "valley metro", "park and ride", "parking garage")),
    ("school_k12", ("elementary", "middle school", "high school", "academy",
                    "preparatory", "charter")),
    ("park_public", ("park", "lake", "beach", "trail", "plaza", "center",
                     "library", "community center", "stadium", "arena")),
]


def classify_venue(name: str) -> str:
    n = " " + (name or "").lower() + " "
    for label, needles in VENUE_RULES:
        if any(k in n for k in needles):
            return label
    return "other_unclassified"


def main() -> None:
    outdir = os.path.dirname(os.path.abspath(__file__))
    cache_r = os.path.join(outdir, "cache_tempe_records.csv")
    cache_v = os.path.join(outdir, "cache_tempe_volume.csv")

    if os.path.exists(cache_r):
        raw = pd.read_csv(cache_r, low_memory=False)
        print(f"Loaded {len(raw):,} cached records", flush=True)
    else:
        print("Fetching target-call-type records with a venue name ...", flush=True)
        raw = fetch_records()
        raw.to_csv(cache_r, index=False)
        print(f"  {len(raw):,} records", flush=True)

    if os.path.exists(cache_v):
        vol = pd.read_csv(cache_v, low_memory=False)
    else:
        print("Fetching per-venue call volume (ambient denominator) ...", flush=True)
        vol = fetch_place_volume()
        vol.to_csv(cache_v, index=False)
    print(f"  {len(vol):,} distinct venue names", flush=True)

    raw["OccurrenceHour"] = pd.to_numeric(raw["OccurrenceHour"], errors="coerce")
    raw["venue_type"] = raw["PlaceName"].map(classify_venue)

    # ---- Step 1: tiered ascertainment screen -----------------------------
    screened = screen_frame(raw, colmap={
        "call_type": "CallType",
        "offense_desc": "FinalCaseTypeTrans",
        "mo": "InitialCaseTypeTrans",
        "premise": "PlaceName",
        "address": "PlaceName",
        "hour": "OccurrenceHour",
    })
    hits = screened[screened["spike_tier"].notna()].copy()
    print(f"\nScreen fired on {len(hits):,} of {len(screened):,} records")
    print(hits["spike_tier"].value_counts().to_string())
    print(f"Total expected cases (sum of tier weights): {hits['spike_pi'].sum():.1f}")

    screened.to_csv(os.path.join(outdir, "out_tempe_screened.csv"), index=False)

    # ---- Provenance diagnostic: where do these calls actually land? -----
    prov = (screened.assign(is_hit=screened["spike_tier"].notna())
            .groupby("venue_type")
            .agg(records=("PrimaryKey", "size"),
                 screen_hits=("is_hit", "sum"),
                 expected_cases=("spike_pi", "sum"))
            .sort_values("expected_cases", ascending=False))
    prov["share_of_expected"] = prov["expected_cases"] / prov["expected_cases"].sum()
    prov.to_csv(os.path.join(outdir, "out_tempe_provenance.csv"))
    print("\n--- Where the signal lands, by venue type ---")
    print(prov.to_string(float_format=lambda v: f"{v:,.3f}"))

    # Sex-offence and overdose calls specifically: venue vs hospital
    for ct in ("Rape Call", "Sexual Abuse Call", "Overdose Call"):
        sub = screened[screened["CallType"] == ct]
        if len(sub):
            top = sub["venue_type"].value_counts(normalize=True).head(5)
            print(f"\n{ct} (n={len(sub):,}) venue-type mix:")
            print((top * 100).round(1).to_string())

    # ---- Steps 2-6: venue-level scoring ---------------------------------
    agg = (hits.groupby("PlaceName")
           .agg(O_expected=("spike_pi", "sum"),
                n_records=("PrimaryKey", "size"),
                tiers=("spike_tier", lambda s: ",".join(sorted(set(s)))),
                nightlife_share=("nightlife_time", "mean"),
                provenance_ambiguous_share=("provenance_ambiguous", "mean"))
           .reset_index())
    agg["venue_type"] = agg["PlaceName"].map(classify_venue)
    agg = agg.merge(vol, on="PlaceName", how="left")
    agg["total_calls"] = agg["total_calls"].fillna(agg["n_records"])
    # Ambient exposure: all-cause call volume minus the screened records
    # themselves, so the numerator is not inside its own denominator.
    agg["X_exposure"] = (agg["total_calls"] - agg["n_records"]).clip(lower=1.0)

    scored = score_places(agg, place_col="PlaceName",
                          min_exposure=20.0, min_expected_cases=1.0)
    scored = publication_gate(scored, min_expected=3.0, small_cell=5)

    p = scored.attrs["eb_params"]
    print(f"\nEB prior: alpha={p.alpha:.3f} beta={p.beta:.3f} "
          f"({p.method}); citywide rate theta={scored.attrs['theta_bar']:.5f} "
          f"expected cases per ambient call")

    cols = ["PlaceName", "venue_type", "n_records", "O_expected", "X_exposure",
            "RR_raw", "RR_eb", "RR_eb_lo95", "RR_eb_hi95", "SRI",
            "elevated_fdr05", "publishable", "nightlife_share", "tiers"]
    scored[cols].to_csv(os.path.join(outdir, "out_tempe_venues.csv"), index=False)

    pub = scored[scored["publishable"]]
    print(f"\n{len(scored):,} venues scored; {int(scored['elevated_fdr05'].sum())} "
          f"credibly elevated after FDR; {len(pub)} pass the full publication gate")
    print("\n--- Top 20 venues passing the publication gate ---")
    show = pub.head(20)[["PlaceName", "venue_type", "n_records", "O_expected",
                         "X_exposure", "RR_eb", "RR_eb_lo95", "SRI"]]
    print(show.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    print("\n--- Expected cases by venue type (share of total) ---")
    vt = (scored.groupby("venue_type")
          .agg(venues=("PlaceName", "size"), O=("O_expected", "sum"),
               X=("X_exposure", "sum")).sort_values("O", ascending=False))
    vt["rate_per_1k_ambient"] = 1000 * vt["O"] / vt["X"]
    vt["share_of_expected"] = vt["O"] / vt["O"].sum()
    print(vt.to_string(float_format=lambda v: f"{v:,.3f}"))

    # ---- Concentration test ---------------------------------------------
    cc = concentration_curve(scored["O_expected"].to_numpy())
    cc.to_csv(os.path.join(outdir, "out_tempe_concentration.csv"), index=False)
    for target in (0.25, 0.50, 0.80):
        row = cc[cc["pct_cases"] >= target].head(1)
        if len(row):
            print(f"{target:.0%} of expected cases sit on "
                  f"{row['pct_places'].iloc[0]:.2%} of venues")

    print("\nWrote out_tempe_screened.csv, out_tempe_venues.csv, "
          "out_tempe_provenance.csv, out_tempe_concentration.csv")


if __name__ == "__main__":
    main()
