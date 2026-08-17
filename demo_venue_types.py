"""
demo_venue_types.py — Aggregate to the level where the Tempe data can actually
support inference, and test the temporal signature.

The venue-level pass in demo_tempe.py finds zero individually identifiable
venues: with only structured proxies (no narrative text), expected case counts
per venue are a fraction of one case and the EB prior correctly shrinks
everything to the citywide mean. That is the right answer, not a failure.

Aggregating to venue TYPE moves the counts into a range where the same
estimator has power. Reads the cache written by demo_tempe.py.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from demo_tempe import classify_venue                    # noqa: E402
from spikelex import NIGHTLIFE_HOURS, screen_frame        # noqa: E402
from spikescore import inflate_for_underreporting, score_places  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
raw = pd.read_csv(os.path.join(HERE, "cache_tempe_records.csv"), low_memory=False)
vol = pd.read_csv(os.path.join(HERE, "cache_tempe_volume.csv"), low_memory=False)

raw["OccurrenceHour"] = pd.to_numeric(raw["OccurrenceHour"], errors="coerce")
raw["venue_type"] = raw["PlaceName"].map(classify_venue)
vol["venue_type"] = vol["PlaceName"].map(classify_venue)

screened = screen_frame(raw, colmap={
    "call_type": "CallType", "offense_desc": "FinalCaseTypeTrans",
    "mo": "InitialCaseTypeTrans", "premise": "PlaceName",
    "address": "PlaceName", "hour": "OccurrenceHour",
})
hits = screened[screened["spike_tier"].notna()]

print("=" * 78)
print("A. VENUE-TYPE RELATIVE RISK, EB-SHRUNK, WITH CREDIBLE INTERVALS")
print("=" * 78)
num = hits.groupby("venue_type").agg(O_expected=("spike_pi", "sum"),
                                     n_records=("PrimaryKey", "size"))
den = vol.groupby("venue_type")["total_calls"].sum().rename("total_calls")
tab = num.join(den, how="left").reset_index()
tab["X_exposure"] = (tab["total_calls"] - tab["n_records"]).clip(lower=1)

scored = score_places(tab, place_col="venue_type", min_exposure=100.0,
                      min_expected_cases=1.0)
p = scored.attrs["eb_params"]
print(f"prior alpha={p.alpha:.3f} beta={p.beta:.3f} [{p.method}]; "
      f"citywide theta={scored.attrs['theta_bar']:.6f} expected cases per ambient call\n")
cols = ["venue_type", "n_records", "O_expected", "X_exposure", "RR_raw", "RR_eb",
        "RR_eb_lo95", "RR_eb_hi95", "SRI", "elevated_fdr05"]
print(scored[cols].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

print("\nRatio of the highest to the lowest venue-type EB rate: "
      f"{scored['RR_eb'].max() / scored['RR_eb'].min():.1f}x")

print()
print("=" * 78)
print("B. TEMPORAL SIGNATURE — does the US structured signal reproduce the")
print("   22:00-03:00 / weekend concentration seen in ED case series?")
print("=" * 78)
hh = hits.copy()
hh["h"] = hh["OccurrenceHour"]
prof = hh.groupby("h")["spike_pi"].sum()
allprof = screened.groupby("OccurrenceHour").size()
comp = pd.DataFrame({"expected_cases": prof,
                     "all_target_records": allprof}).fillna(0)
comp["case_share"] = comp["expected_cases"] / comp["expected_cases"].sum()
comp["baseline_share"] = comp["all_target_records"] / comp["all_target_records"].sum()
comp["ratio"] = comp["case_share"] / comp["baseline_share"].replace(0, np.nan)
print(comp.to_string(float_format=lambda v: f"{v:,.4f}"))

night = comp.loc[comp.index.isin(NIGHTLIFE_HOURS), "expected_cases"].sum()
print(f"\nShare of expected cases in the 21:00-04:59 window: "
      f"{night / comp['expected_cases'].sum():.1%}")
print("DO NOT REPORT THAT NUMBER AS A FINDING. It is mechanical: tier C2")
print("requires a nightlife hour, so the screen manufactures its own temporal")
print("concentration. The only unbiased test is the subset where the hour was")
print("NOT part of the firing condition — tier C1 at a nightlife premise,")
print("which qualifies on premise alone.")

bars = hh[hh["venue_type"] == "bar_or_nightclub"]
all_bars = screened[screened["venue_type"] == "bar_or_nightclub"]
unforced = bars[bars["spike_tier"] == "C1_poisoning_nightlife"]

case_night = unforced["h"].isin(NIGHTLIFE_HOURS).mean() if len(unforced) else float("nan")
base_night = all_bars["OccurrenceHour"].isin(NIGHTLIFE_HOURS).mean()
print(f"\nUnforced subset (bar/nightclub, tier C1 only): n={len(unforced)}")
print(f"  share of those in 21:00-04:59:                 {case_night:.1%}")
print(f"  share of ALL bar target records in that window: {base_night:.1%}")
print(f"  case-to-baseline concentration ratio:          {case_night/base_night:.2f}")
print("\nHONEST READING: the ratio is at or below 1.0, so this US structured-data")
print("signal does NOT independently reproduce the strong 22:00-03:00 clustering")
print("reported in hospital case series. With n=%d the test is also badly" % len(unforced))
print("underpowered. Two readings are consistent with this: (a) the structured")
print("proxies are mostly capturing ordinary alcohol overdose and sexual-offence")
print("calls rather than spiking, or (b) the recorded timestamp is the CALL time,")
print("which lags the incident by hours — the same delay that makes toxicology")
print("fail. Either way, do not use hour-of-day as a scoring input until it has")
print("been validated against narrative-confirmed cases.")
print("\nBar/nightclub screened records by hour (all tiers, hour partly forced):")
print(bars.groupby("h").size().to_string())

print()
print("=" * 78)
print("C. ABSOLUTE BURDEN AFTER UNDER-REPORTING ADJUSTMENT")
print("=" * 78)
obs = hits["spike_pi"].sum()
lo, mid, hi = inflate_for_underreporting(obs)
print(f"Observed expected cases in the Tempe police feed, 2022-present: {obs:.1f}")
print(f"Implied true incidents at p_report = 0.22 / 0.14 / 0.08: "
      f"{lo:.0f} / {mid:.0f} / {hi:.0f}")
print("This scaling is deliberately ranking-neutral: one global reporting "
      "probability multiplies every venue identically and therefore cannot "
      "reorder them. It converts a ranking into a burden statement, nothing more.")

scored.to_csv(os.path.join(HERE, "out_tempe_venue_types.csv"), index=False)
comp.to_csv(os.path.join(HERE, "out_tempe_hour_profile.csv"))
print("\nWrote out_tempe_venue_types.csv, out_tempe_hour_profile.csv")
