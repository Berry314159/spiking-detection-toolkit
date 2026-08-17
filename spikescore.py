"""
spikescore.py — Venue-level risk scoring for non-consensual drugging.

THE FORMULA, IN ONE PLACE
-------------------------
For each candidate place i (a licensed premise, a campus building, a street
segment, or a block group) over an analysis window of length T:

  STEP 1  Expected case count (not raw count)
          O_i = sum over records r at place i of  pi(tier(r))
          where pi is the calibrated precision of the ascertainment tier.
          This is a probabilistic count: 12 tier-C hits at pi=0.04 contribute
          0.48 expected cases, not 12.

  STEP 2  Exposure / denominator
          E_i = theta_bar * X_i
          X_i is the exposure measure, in preference order:
            (a) capacity_i * high_alcohol_hours_i * open_days   [best]
            (b) alcohol_receipts_i (TX mixed-beverage, incl. cover_charge)
            (c) licence class weight * years_licensed            [fallback]
            (d) residential or ambient population                [worst]
          theta_bar = sum(O) / sum(X) is the citywide rate per exposure unit.

  STEP 3  Raw relative risk (SMR-style)
          r_i = O_i / E_i

  STEP 4  Empirical-Bayes / Poisson-gamma shrinkage
          O_i ~ Poisson(E_i * theta_i),  theta_i ~ Gamma(alpha, beta)
          => posterior  theta_i | O_i ~ Gamma(alpha + O_i, beta + E_i)
          RR_i^EB = (alpha + O_i) / (beta + E_i)
          shrinkage weight w_i = E_i / (E_i + beta)
          alpha, beta estimated by method of moments across all places.
          This is the standard rate-smoothing estimator; it stops a bar with
          1 case and 40 exposure-units from outranking a bar with 9 cases and
          900 exposure-units.

  STEP 5  Uncertainty and a decision rule
          95% credible interval from the Gamma posterior.
          Flag a place as elevated only if the LOWER bound of the interval
          exceeds 1.0 (i.e. worse than citywide even in the pessimistic case),
          after Benjamini-Hochberg FDR control at q=0.05 on the one-sided
          exceedance probability P(theta_i <= 1).

  STEP 6  Reporting index
          SRI_i = 100 * RR_i^EB      (100 = citywide average exposure-adjusted
                                      rate; 250 = 2.5x citywide)

  STEP 7  Under-reporting adjustment (ranking-neutral by design)
          Applying one global reporting probability p does not change the
          ranking. Only place-varying p does. So p is applied ONLY as a
          scale-up for absolute burden estimates, and place-varying p is
          modelled explicitly where an ED-vs-police ratio is available.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not name individual venues as offenders. Screened records are
suspicion signals with tier precisions well below 1.0 for everything except
explicit narrative text, and the address attached to a record may be where
the victim was found or where they reported, not where they were drugged.
Use `provenance_ambiguous` and `min_expected_cases` gates before publishing
any venue-identifying output.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Exposure construction
# ---------------------------------------------------------------------------
# Relative risk multipliers by licensed capacity band, from the licensed-premise
# assault-risk literature (capacity 501-1000 carried ~6.1x the risk of the
# smallest band; each additional hour of high-alcohol trading added ~72%).
CAPACITY_BAND_WEIGHT: Dict[str, float] = {
    "0-50": 1.0,
    "51-100": 1.6,
    "101-250": 2.4,
    "251-500": 3.8,
    "501-1000": 6.1,
    "1000+": 7.5,
}

# Licence-class weights for the fallback exposure path. On-premise
# late-trading spirits licences carry the exposure; off-premise beer/wine
# retail effectively does not.
LICENCE_CLASS_WEIGHT: Dict[str, float] = {
    "on_premise_spirits_late": 1.00,
    "on_premise_spirits": 0.70,
    "on_premise_beer_wine": 0.35,
    "restaurant_incidental": 0.15,
    "brewpub_taproom": 0.30,
    "private_club": 0.25,
    "off_premise": 0.02,
    "unknown": 0.20,
}

HIGH_ALCOHOL_HOUR_ELASTICITY = 0.72   # +72% risk per extra late trading hour


def capacity_band(capacity: Optional[float]) -> str:
    if capacity is None or (isinstance(capacity, float) and math.isnan(capacity)):
        return "101-250"        # neutral-ish default
    c = float(capacity)
    if c <= 50:
        return "0-50"
    if c <= 100:
        return "51-100"
    if c <= 250:
        return "101-250"
    if c <= 500:
        return "251-500"
    if c <= 1000:
        return "501-1000"
    return "1000+"


def exposure_capacity_hours(
    capacity: Optional[float],
    late_hours_per_week: float,
    open_weeks: float = 52.0,
) -> float:
    """
    Preferred exposure: capacity x late trading hours, with the empirical
    capacity-band risk multiplier and the per-late-hour elasticity folded in.
    Returns exposure units comparable across venues in one city.
    """
    base = CAPACITY_BAND_WEIGHT[capacity_band(capacity)]
    hours = max(late_hours_per_week, 0.0)
    hour_factor = (1.0 + HIGH_ALCOHOL_HOUR_ELASTICITY) ** (hours / 7.0)
    cap = float(capacity) if capacity and not math.isnan(float(capacity)) else 150.0
    return cap * base * hour_factor * (open_weeks / 52.0)


def exposure_receipts(total_receipts: float, cover_charge: float = 0.0,
                      cover_weight: float = 3.0) -> float:
    """
    Receipts-based exposure. Cover charge is up-weighted because a venue that
    charges admission is operating as a nightclub -- the setting where roughly
    half of all reported spiking incidents occur -- rather than as a
    restaurant with the same dollar volume.
    """
    return max(float(total_receipts), 0.0) + cover_weight * max(float(cover_charge), 0.0)


def exposure_licence(licence_class: str, years_licensed: float = 1.0) -> float:
    w = LICENCE_CLASS_WEIGHT.get(licence_class, LICENCE_CLASS_WEIGHT["unknown"])
    return w * max(float(years_licensed), 0.0) * 100.0


# ---------------------------------------------------------------------------
# Empirical-Bayes / Poisson-gamma engine
# ---------------------------------------------------------------------------
@dataclass
class EBParams:
    alpha: float
    beta: float
    theta_bar: float
    method: str

    @property
    def prior_mean(self) -> float:
        return self.alpha / self.beta if self.beta else float("nan")


def fit_gamma_prior(O: np.ndarray, E: np.ndarray, method: str = "moments") -> EBParams:
    """
    Estimate the Gamma(alpha, beta) prior on the relative-risk scale by
    method of moments on the SMR distribution, following the standard
    rate-smoothing derivation used in GeoDa's empirical Bayes smoother and in
    the Clayton-Kaldor estimator.

    Working on the RR scale (r_i = O_i/E_i, prior mean 1) rather than the raw
    rate scale keeps the prior interpretable: beta is literally "how many
    exposure units of evidence it takes to move a venue off the citywide
    average".
    """
    O = np.asarray(O, dtype=float)
    E = np.asarray(E, dtype=float)
    keep = E > 0
    O, E = O[keep], E[keep]
    if len(O) < 3 or O.sum() <= 0:
        return EBParams(alpha=1.0, beta=1.0, theta_bar=float(O.sum() / max(E.sum(), 1e-9)),
                        method="degenerate")

    theta_bar = O.sum() / E.sum()
    r = O / (E * theta_bar)                      # RR relative to citywide
    m = float(np.average(r, weights=E))          # exposure-weighted mean ~ 1

    # Moment estimator of the between-place variance, subtracting the Poisson
    # sampling component. Negative estimates mean no detectable extra-Poisson
    # variation -> shrink everything hard toward the mean.
    #
    # Under theta_i = m, Var(r_i) = Var(O_i)/E_i^2 = 1/E_i, so the
    # exposure-weighted expectation of the sampling component is
    # sum(E_i * 1/E_i) / sum(E_i) = n / sum(E). Subtracting that from the
    # observed weighted variance of the SMRs leaves the between-place
    # (extra-Poisson) variance -- the Marshall / Clayton-Kaldor moment
    # estimator that GeoDa's EB rate smoother uses.
    n = len(r)
    raw_var = float(np.average((r - m) ** 2, weights=E))
    poisson_component = n / max(E.sum(), 1e-12)
    var = raw_var - poisson_component

    if not np.isfinite(var) or var <= 1e-9:
        alpha, beta = 50.0, 50.0                 # strong shrink to RR=1
        return EBParams(alpha, beta, theta_bar, "moments-degenerate")

    # Gamma with mean m and variance var  ->  alpha = m^2/var, beta = m/var,
    # expressed in exposure units after rescaling by theta_bar.
    beta = m / var
    alpha = m * beta
    alpha = float(np.clip(alpha, 1e-3, 1e6))
    beta = float(np.clip(beta, 1e-3, 1e6))

    # GUARDRAIL. An estimated prior with alpha << 1 means the fitted
    # between-place variance is enormous relative to the mean. In practice that
    # happens when the overwhelming majority of places have zero cases and a
    # handful have any at all. The estimator is then fitting zero-inflation,
    # not a risk distribution: it applies almost no shrinkage, relative risks
    # explode into the hundreds, and credible intervals stop meaning anything.
    # Read it as evidence that the EXPOSURE MODEL IS MISSPECIFIED, or that
    # counts are too sparse for place-level inference. It is NOT evidence that
    # those places are hundreds of times riskier. Aggregate to a coarser unit
    # (venue type, street segment, block group) and re-run.
    if alpha < 0.5:
        return EBParams(alpha, beta, theta_bar, "UNRELIABLE-sparse-or-misspecified")

    return EBParams(alpha, beta, theta_bar, method)


def _gamma_ppf(q: float, shape: float, rate: float) -> float:
    """Gamma quantile without SciPy: Wilson-Hilferty + bisection refinement."""
    # Wilson-Hilferty start
    z = _norm_ppf(q)
    x = shape * (1.0 - 1.0 / (9.0 * shape) + z / (3.0 * math.sqrt(shape))) ** 3
    x = max(x, 1e-12)
    lo, hi = 1e-12, max(x * 10.0, 10.0 * shape + 10.0)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _gamma_cdf(mid, shape) < q:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi) / rate


def _gamma_cdf(x: float, shape: float) -> float:
    """Regularised lower incomplete gamma P(shape, x) via series/continued fraction."""
    if x <= 0:
        return 0.0
    if x < shape + 1.0:
        term = 1.0 / shape
        total = term
        ap = shape
        for _ in range(500):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return total * math.exp(-x + shape * math.log(x) - math.lgamma(shape))
    # continued fraction for Q, then 1-Q
    tiny = 1e-300
    b = x + 1.0 - shape
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - shape)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delt = d * c
        h *= delt
        if abs(delt - 1.0) < 1e-14:
            break
    q = math.exp(-x + shape * math.log(x) - math.lgamma(shape)) * h
    return 1.0 - q


def _norm_ppf(p: float) -> float:
    """Acklam's inverse normal CDF approximation."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def bh_fdr(pvals: Sequence[float], q: float = 0.05) -> np.ndarray:
    """
    Benjamini-Hochberg step-up. Returns a boolean array of rejections.
    Required because scoring hundreds or thousands of venues at alpha=0.05
    guarantees dozens of spurious 'hotspots' without multiplicity control.
    """
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0:
        return np.array([], dtype=bool)
    order = np.argsort(p)
    thresh = q * (np.arange(1, n + 1) / n)
    passed = p[order] <= thresh
    k = np.max(np.where(passed)[0]) + 1 if passed.any() else 0
    out = np.zeros(n, dtype=bool)
    if k:
        out[order[:k]] = True
    return out


# ---------------------------------------------------------------------------
# Main scoring entry point
# ---------------------------------------------------------------------------
def score_places(
    df: pd.DataFrame,
    *,
    place_col: str = "place_id",
    expected_col: str = "O_expected",
    exposure_col: str = "X_exposure",
    min_exposure: float = 1.0,
    min_expected_cases: float = 1.0,
    cred_mass: float = 0.95,
    fdr_q: float = 0.05,
) -> pd.DataFrame:
    """
    Run Steps 2-6 of the formula on a place-level table.

    Input: one row per place with an expected-case count (from spikelex tier
    weights) and an exposure measure. Output: the same rows plus raw RR,
    EB-shrunk RR, credible interval, exceedance probability, FDR flag, and the
    SRI index, sorted by SRI descending.
    """
    out = df.copy()
    O = out[expected_col].astype(float).to_numpy()
    X = out[exposure_col].astype(float).to_numpy()
    X = np.where(np.isfinite(X), X, 0.0)

    theta_bar = O.sum() / max(X.sum(), 1e-12)     # cases per exposure unit
    E = theta_bar * X                              # expected under citywide rate

    params = fit_gamma_prior(O, E)
    alpha, beta = params.alpha, params.beta

    with np.errstate(divide="ignore", invalid="ignore"):
        rr_raw = np.where(E > 0, O / E, np.nan)

    post_shape = alpha + O
    post_rate = beta + E
    rr_eb = post_shape / post_rate
    w = np.where(post_rate > 0, E / post_rate, 0.0)

    lo_q = (1.0 - cred_mass) / 2.0
    hi_q = 1.0 - lo_q
    rr_lo = np.array([_gamma_ppf(lo_q, s, r) for s, r in zip(post_shape, post_rate)])
    rr_hi = np.array([_gamma_ppf(hi_q, s, r) for s, r in zip(post_shape, post_rate)])

    # One-sided exceedance: P(theta_i <= 1 | data). Small = credibly elevated.
    p_not_elevated = np.array([_gamma_cdf(1.0 * r, s) for s, r in zip(post_shape, post_rate)])

    eligible = (X >= min_exposure) & (O >= min_expected_cases)
    flags = np.zeros(len(out), dtype=bool)
    if eligible.any():
        sub = bh_fdr(p_not_elevated[eligible], q=fdr_q)
        idx = np.where(eligible)[0]
        flags[idx[sub]] = True
    flags = flags & (rr_lo > 1.0)

    out["E_expected_under_citywide"] = E
    out["RR_raw"] = rr_raw
    out["RR_eb"] = rr_eb
    out["RR_eb_lo95"] = rr_lo
    out["RR_eb_hi95"] = rr_hi
    out["shrinkage_weight"] = w
    out["p_not_elevated"] = p_not_elevated
    out["elevated_fdr05"] = flags
    out["SRI"] = 100.0 * rr_eb
    out["eligible_for_publication"] = eligible
    out.attrs["eb_params"] = params
    out.attrs["theta_bar"] = theta_bar

    return out.sort_values("SRI", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Concentration and accuracy diagnostics
# ---------------------------------------------------------------------------
def concentration_curve(counts: Sequence[float]) -> pd.DataFrame:
    """
    Test the law of crime concentration on your own data. The benchmark from
    the street-segment literature is that roughly 50% of crime sits on about
    4-5% of segments and the top 1% carries about a quarter. Risky-facilities
    work on licensed premises found 3 of 30 pubs accounted for just over half
    of assaults. If your spiking signal is NOT concentrated, that is evidence
    your ascertainment is picking up noise rather than places.
    """
    c = np.sort(np.asarray(counts, dtype=float))[::-1]
    total = c.sum()
    if total <= 0:
        return pd.DataFrame(columns=["pct_places", "pct_cases"])
    cum = np.cumsum(c) / total
    pct_places = np.arange(1, len(c) + 1) / len(c)
    return pd.DataFrame({"pct_places": pct_places, "pct_cases": cum})


def pai(n_hotspot_cases: float, n_total_cases: float,
        hotspot_area: float, total_area: float) -> float:
    """
    Predictive Accuracy Index: (n/N) / (a/A). Published benchmarks for
    comparison are roughly 7.2 for kernel-density hotspot maps and 3.5 for
    risk-terrain models; a PAI near 1 means your 'hotspots' are no better
    than picking area at random.
    """
    if n_total_cases <= 0 or total_area <= 0 or hotspot_area <= 0:
        return float("nan")
    return (n_hotspot_cases / n_total_cases) / (hotspot_area / total_area)


def getis_ord_gstar(values: Sequence[float], coords: Sequence[Tuple[float, float]],
                    bandwidth_m: float = 400.0) -> pd.DataFrame:
    """
    Getis-Ord Gi* with a fixed-distance binary weight matrix, computed without
    external spatial libraries. Use it as CONFIRMATION that a set of
    high-SRI venues form a genuine cluster rather than isolated points, and
    apply bh_fdr to the returned p-values.

    coords must be projected metres (e.g. UTM / state-plane), not lat-lon.
    """
    x = np.asarray(values, dtype=float)
    pts = np.asarray(coords, dtype=float)
    n = len(x)
    if n < 3:
        return pd.DataFrame({"gi_star": np.full(n, np.nan), "z": np.full(n, np.nan),
                             "p": np.full(n, np.nan)})
    xbar = x.mean()
    s = math.sqrt(max((x ** 2).mean() - xbar ** 2, 0.0))
    d = np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2).sum(-1))
    W = (d <= bandwidth_m).astype(float)          # includes self -> Gi*
    num = W @ x - xbar * W.sum(1)
    wsum = W.sum(1)
    denom = s * np.sqrt(np.maximum((n * (W ** 2).sum(1) - wsum ** 2) / (n - 1), 1e-12))
    z = np.where(denom > 0, num / denom, 0.0)
    p = 2.0 * (1.0 - _norm_cdf_vec(np.abs(z)))
    return pd.DataFrame({"gi_star": num, "z": z, "p": p})


def _norm_cdf_vec(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Under-reporting: absolute burden only, never ranking
# ---------------------------------------------------------------------------
def inflate_for_underreporting(
    observed: float,
    p_report: float = 0.14,
    p_lo: float = 0.08,
    p_hi: float = 0.22,
) -> Tuple[float, float, float]:
    """
    Scale an observed count to an estimated true count.

    Default p_report = 0.14 comes from the victim-survey finding that about
    14% of spiking victims reported to police (against 16% who told the venue
    and 9% who presented to a hospital), and is bracketed by the 8% and 22%
    figures from adjacent surveys.

    CRITICAL CAVEAT: because this multiplies every place by the same constant,
    it CANNOT change the venue ranking. It exists only to convert a ranking
    into a public-health burden statement. Place-varying reporting propensity
    -- e.g. a campus venue whose patrons route to a student health centre
    rather than to police -- is the only kind that shifts ranks, and it must
    be estimated from an ED-vs-police linkage, not assumed.
    """
    return (observed / p_hi, observed / p_report, observed / p_lo)


def ed_police_ratio_adjust(police_count: float, ed_police_ratio: float = 25.0) -> float:
    """
    Apply a catchment-specific ED-to-police case ratio. The published
    single-catchment comparison found 75 ED-attending suspected-spiking cases
    against 3 police-recorded ones in the same year, a ratio of about 25:1.
    Treat 25 as an upper-bound sanity check, not a default.
    """
    return police_count * ed_police_ratio


# ---------------------------------------------------------------------------
# Covariate model for the "demographic formula" question
# ---------------------------------------------------------------------------
def poisson_glm_offset(
    df: pd.DataFrame,
    y_col: str,
    exposure_col: str,
    covariates: Sequence[str],
    max_iter: int = 100,
    tol: float = 1e-9,
) -> pd.DataFrame:
    """
    Poisson regression with log(exposure) offset, fitted by IRLS -- no
    statsmodels required. exp(coefficient) is a rate ratio per unit of
    covariate, holding exposure fixed. This is the honest way to answer
    "is there a demographic formula": you get rate ratios for venue and
    neighbourhood attributes, NOT a claim about which people are victims.

    Interpretation warning: covariates measured on the surrounding block group
    describe the AREA, not the individuals in it. Attributing area-level
    associations to the people at a venue is the ecological fallacy, and it is
    the single most common way this kind of analysis gets misused.
    """
    X = np.column_stack([np.ones(len(df))] + [df[c].astype(float).to_numpy() for c in covariates])
    y = df[y_col].astype(float).to_numpy()
    off = np.log(np.maximum(df[exposure_col].astype(float).to_numpy(), 1e-12))
    b = np.zeros(X.shape[1])
    b[0] = math.log(max(y.sum() / max(np.exp(off).sum(), 1e-12), 1e-12))

    for _ in range(max_iter):
        eta = X @ b + off
        mu = np.exp(np.clip(eta, -50, 50))
        W = np.diag(mu)
        z = (y - mu)
        try:
            step = np.linalg.solve(X.T @ W @ X + 1e-8 * np.eye(X.shape[1]), X.T @ z)
        except np.linalg.LinAlgError:
            break
        b = b + step
        if np.max(np.abs(step)) < tol:
            break

    eta = X @ b + off
    mu = np.exp(np.clip(eta, -50, 50))
    cov = np.linalg.pinv(X.T @ np.diag(mu) @ X)
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    names = ["intercept"] + list(covariates)
    z_stat = np.where(se > 0, b / se, 0.0)
    return pd.DataFrame({
        "term": names,
        "coef": b,
        "se": se,
        "rate_ratio": np.exp(b),
        "rr_lo95": np.exp(b - 1.96 * se),
        "rr_hi95": np.exp(b + 1.96 * se),
        "z": z_stat,
        "p": 2.0 * (1.0 - _norm_cdf_vec(np.abs(z_stat))),
    })


# ---------------------------------------------------------------------------
# Publication gate
# ---------------------------------------------------------------------------
def publication_gate(
    scored: pd.DataFrame,
    *,
    min_expected: float = 3.0,
    small_cell: int = 5,
    require_fdr: bool = True,
    max_provenance_ambiguous_share: float = 0.30,
) -> pd.DataFrame:
    """
    Apply suppression rules before anything leaves your machine.

      * cells below `small_cell` observations are suppressed, following
        standard public-health small-numbers practice;
      * places whose signal is mostly provenance-ambiguous addresses
        (hospitals, police stations, 'other/unknown' location codes) are
        suppressed, because their geography is probably the reporting site;
      * only FDR-surviving elevated places are named.

    Everything failing the gate should still be retained internally for
    aggregate and trend reporting -- just not attached to a venue name.
    """
    g = scored.copy()
    amb = g["provenance_ambiguous_share"] if "provenance_ambiguous_share" in g else 0.0
    ok = (g["O_expected"] >= min_expected)
    if "n_records" in g:
        ok &= (g["n_records"] >= small_cell)
    if require_fdr and "elevated_fdr05" in g:
        ok &= g["elevated_fdr05"]
    if isinstance(amb, pd.Series):
        ok &= (amb <= max_provenance_ambiguous_share)
    g["publishable"] = ok
    return g
