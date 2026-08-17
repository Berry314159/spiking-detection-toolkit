"""
test_spikescore.py — Validation of the scoring engine.

Three checks:

  1. RECOVERY. Simulate venues with a KNOWN Gamma-distributed relative risk
     and Poisson counts, then confirm fit_gamma_prior recovers the prior and
     that the EB posterior means are closer to the truth than the raw SMRs.
  2. SHRINKAGE DIRECTION. Confirm a low-exposure venue with a freak single
     case is pulled toward 1.0 while a high-exposure venue with a real excess
     is not.
  3. NUMERICS. Confirm the hand-rolled Gamma CDF/quantile agree with SciPy
     (when available) and are self-consistent (ppf is the inverse of cdf).

Run: python test_spikescore.py
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikescore import (_gamma_cdf, _gamma_ppf, bh_fdr, fit_gamma_prior,  # noqa: E402
                        poisson_glm_offset, score_places)

rng = np.random.default_rng(20260817)
FAILS = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


# ---------------------------------------------------------------------------
print("=" * 72)
print("1. PRIOR RECOVERY AND SHRINKAGE ACCURACY")
print("=" * 72)

N = 800
ALPHA_TRUE, BETA_TRUE = 4.0, 4.0          # prior mean 1.0, sd 0.5
theta_true = rng.gamma(ALPHA_TRUE, 1.0 / BETA_TRUE, N)
# Exposure spread over 3 orders of magnitude, like real licensed premises.
X = rng.lognormal(mean=math.log(400), sigma=1.2, size=N)
theta_bar_true = 0.02                      # cases per exposure unit
O = rng.poisson(theta_bar_true * X * theta_true)

df = pd.DataFrame({"place_id": [f"v{i}" for i in range(N)],
                   "O_expected": O.astype(float), "X_exposure": X})
scored = score_places(df, min_exposure=1.0, min_expected_cases=0.0)
p = scored.attrs["eb_params"]
print(f"  true prior:      alpha={ALPHA_TRUE:.2f} beta={BETA_TRUE:.2f}  "
      f"(mean {ALPHA_TRUE/BETA_TRUE:.2f}, sd {math.sqrt(ALPHA_TRUE)/BETA_TRUE:.3f})")
print(f"  estimated prior: alpha={p.alpha:.2f} beta={p.beta:.2f}  "
      f"(mean {p.prior_mean:.2f}, sd {math.sqrt(p.alpha)/p.beta:.3f})  [{p.method}]")
print(f"  citywide theta:  true {theta_bar_true:.4f}  est {scored.attrs['theta_bar']:.4f}")

check("prior method is non-degenerate", p.method == "moments", p.method)
check("prior mean within 20% of truth",
      abs(p.prior_mean - ALPHA_TRUE / BETA_TRUE) < 0.20,
      f"{p.prior_mean:.3f} vs {ALPHA_TRUE/BETA_TRUE:.3f}")
prior_sd_est = math.sqrt(p.alpha) / p.beta
prior_sd_true = math.sqrt(ALPHA_TRUE) / BETA_TRUE
check("prior sd within a factor of 2 of truth",
      0.5 < prior_sd_est / prior_sd_true < 2.0,
      f"{prior_sd_est:.3f} vs {prior_sd_true:.3f}")

truth = pd.Series(theta_true, index=[f"v{i}" for i in range(N)])
s = scored.set_index("place_id")
t = truth.reindex(s.index)
mse_raw = float(np.nanmean((s["RR_raw"] - t) ** 2))
mse_eb = float(np.nanmean((s["RR_eb"] - t) ** 2))
print(f"  MSE vs true RR:  raw SMR {mse_raw:.4f}   EB {mse_eb:.4f}   "
      f"({100*(1-mse_eb/mse_raw):.1f}% reduction)")
check("EB beats raw SMR on MSE", mse_eb < mse_raw, f"{mse_eb:.4f} < {mse_raw:.4f}")

cover = ((t >= s["RR_eb_lo95"]) & (t <= s["RR_eb_hi95"])).mean()
print(f"  95% credible-interval coverage of true RR: {cover:.1%}")
check("interval coverage in 88-99%", 0.88 <= cover <= 0.99, f"{cover:.1%}")

hi = s["elevated_fdr05"]
if hi.sum():
    fdp = float((t[hi] <= 1.0).mean())
    print(f"  flagged elevated: {int(hi.sum())} venues; "
          f"share whose true RR<=1 (false discoveries): {fdp:.1%}")
    check("realised false-discovery proportion <= 0.15", fdp <= 0.15, f"{fdp:.1%}")
else:
    check("some venues flagged elevated", False, "none flagged")

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("2. SHRINKAGE DIRECTION ON HAND-BUILT CASES")
print("=" * 72)

hand = pd.DataFrame({
    "place_id": ["tiny_freak", "big_real_excess", "big_average", "tiny_zero"],
    "O_expected": [1.0, 40.0, 8.0, 0.0],
    "X_exposure": [10.0, 1000.0, 1000.0, 10.0],
})
# Pad with a comparison set that has REALISTIC between-venue variation. Padding
# with identically-behaved venues would make the estimated prior artificially
# tight (var ~ 0), which correctly but unhelpfully shrinks even a genuine 5x
# venue most of the way back to 1. Real licensed-premise data is strongly
# overdispersed, so the prior is given sd ~ 0.5 on the RR scale.
pad_X = rng.lognormal(math.log(600), 1.0, 400)
pad = pd.DataFrame({"place_id": [f"pad{i}" for i in range(400)],
                    "O_expected": rng.poisson(
                        0.008 * pad_X * rng.gamma(4.0, 0.25, 400)).astype(float),
                    "X_exposure": pad_X})
h = score_places(pd.concat([hand, pad], ignore_index=True),
                 min_exposure=1.0, min_expected_cases=0.0)
view = h[h["place_id"].isin(hand["place_id"])][
    ["place_id", "O_expected", "X_exposure", "RR_raw", "RR_eb",
     "RR_eb_lo95", "RR_eb_hi95", "shrinkage_weight", "elevated_fdr05"]]
print(view.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))

g = h.set_index("place_id")
check("tiny venue with 1 freak case is shrunk toward 1",
      g.loc["tiny_freak", "RR_eb"] < 0.5 * g.loc["tiny_freak", "RR_raw"],
      f"raw {g.loc['tiny_freak','RR_raw']:.2f} -> EB {g.loc['tiny_freak','RR_eb']:.2f}")
check("tiny venue is NOT flagged elevated",
      not bool(g.loc["tiny_freak", "elevated_fdr05"]))
check("large venue with real 5x excess stays clearly elevated",
      g.loc["big_real_excess", "RR_eb"] > 3.0,
      f"EB {g.loc['big_real_excess','RR_eb']:.2f}")
check("large venue with real excess IS flagged",
      bool(g.loc["big_real_excess", "elevated_fdr05"]))
check("average large venue lands near 1",
      0.75 < g.loc["big_average", "RR_eb"] < 1.35,
      f"EB {g.loc['big_average','RR_eb']:.2f}")
check("shrinkage weight is larger for the high-exposure venue",
      g.loc["big_average", "shrinkage_weight"] > g.loc["tiny_freak", "shrinkage_weight"],
      f"{g.loc['big_average','shrinkage_weight']:.3f} vs "
      f"{g.loc['tiny_freak','shrinkage_weight']:.3f}")

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("3. NUMERICS: GAMMA CDF / QUANTILE, FDR, POISSON GLM")
print("=" * 72)

try:
    from scipy import stats  # type: ignore
    errs = []
    for shape in (0.5, 1.0, 4.0, 50.0, 500.0):
        for x in (0.1, 1.0, 5.0, 50.0, 500.0):
            errs.append(abs(_gamma_cdf(x, shape) - stats.gamma.cdf(x, shape)))
    check("gamma CDF matches SciPy to 1e-8", max(errs) < 1e-8, f"max err {max(errs):.2e}")
except ImportError:
    print("  (SciPy unavailable -- self-consistency check only)")

rt = []
for shape in (1.0, 4.0, 50.0):
    for q in (0.025, 0.5, 0.975):
        x = _gamma_ppf(q, shape, 1.0)
        rt.append(abs(_gamma_cdf(x, shape) - q))
check("gamma ppf inverts cdf to 1e-6", max(rt) < 1e-6, f"max err {max(rt):.2e}")

pv = np.concatenate([rng.uniform(0, 1, 950), rng.uniform(0, 1e-6, 50)])
rej = bh_fdr(pv, q=0.05)
check("BH rejects the planted small p-values",
      rej.sum() >= 45 and rej[-50:].sum() >= 45, f"{int(rej.sum())} rejections")
check("BH is monotone (no rejection above the largest rejected p)",
      pv[rej].max() <= pv[~rej].min() if (~rej).any() else True)

# Poisson GLM: recover a known rate ratio.
n = 2000
cap = rng.lognormal(math.log(200), 0.8, n)
is_club = rng.binomial(1, 0.3, n).astype(float)
late = rng.uniform(0, 6, n)
true_rr_club, true_rr_late = 2.5, 1.15
mu = 0.004 * cap * (true_rr_club ** is_club) * (true_rr_late ** late)
y = rng.poisson(mu)
gdf = pd.DataFrame({"y": y.astype(float), "cap": cap, "is_club": is_club, "late": late})
fit = poisson_glm_offset(gdf, "y", "cap", ["is_club", "late"])
print(fit.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
rr = fit.set_index("term")["rate_ratio"]
check("GLM recovers the club rate ratio within 15%",
      abs(rr["is_club"] - true_rr_club) / true_rr_club < 0.15,
      f"{rr['is_club']:.3f} vs {true_rr_club}")
check("GLM recovers the per-late-hour rate ratio within 10%",
      abs(rr["late"] - true_rr_late) / true_rr_late < 0.10,
      f"{rr['late']:.3f} vs {true_rr_late}")

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
print("=" * 72)
sys.exit(1 if FAILS else 0)
