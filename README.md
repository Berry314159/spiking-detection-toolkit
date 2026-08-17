# Spiking Detection Toolkit

A formula and reference implementation for locating non-consensual drugging
("drink spiking") in US public police, CAD, EMS and alcohol-licensing data.

## Files

| File | What it is |
| --- | --- |
| `spikelex.py` | Lexicon + tiered case-ascertainment screen. Regex dictionary, premise/call-type vocabularies, NIBRS code primitives, provenance-ambiguity detection, per-tier precision weights. |
| `spikescore.py` | The scoring engine. Exposure construction, Poisson-gamma empirical-Bayes shrinkage, credible intervals, BH-FDR control, Getis-Ord Gi*, PAI, concentration curve, Poisson GLM with offset, publication/suppression gate. No SciPy dependency. |
| `spikefetch.py` | Ingestion adapters: paged Socrata SoQL, paged ArcGIS FeatureServer, plus named adapters and a registry of the premise screens, EMS feeds and licence registries worth using. |
| `queries.sql` | Runnable SoQL and local SQL. Narrative screens, premise screens, the exposure denominator, and the full venue score expressed in SQL. |
| `demo_tempe.py` | Worked example on live Tempe AZ data (the only large US feed with a venue-name field). |
| `demo_venue_types.py` | Venue-type rollup, temporal-signature test, burden estimate. |
| `demo_dallas.py` | Full pipeline with a real per-venue exposure denominator (Dallas incidents x Texas mixed-beverage receipts). |
| `test_spikescore.py` | Validation: prior recovery, shrinkage direction, numerics, GLM recovery. |

## Quick start

```bash
pip install pandas numpy          # scipy optional, only for a test assertion
python test_spikescore.py         # confirm the engine works
python demo_tempe.py              # ~65k live records, caches locally
python demo_venue_types.py        # venue-type risk ratios
python demo_dallas.py             # real receipts denominator
```

## The formula in brief

For each place `i` over an analysis window:

1. **Expected cases** `O_i = Σ π(tier(r))` over records `r` at place `i`.
   Never a raw count — every record enters weighted by the calibrated
   precision of the tier that fired.
2. **Exposure** `E_i = θ̄ · X_i`, where `X_i` is capacity × late trading hours
   (best), alcohol receipts (good), licence-class weight (fallback), or
   ambient population (worst), and `θ̄ = ΣO / ΣX`.
3. **Raw rate** `r_i = O_i / E_i`.
4. **Shrinkage** `RR_i = (α + O_i) / (β + E_i)`, with `α, β` fitted by method
   of moments across all places.
5. **Decision** flag only if the 95% credible lower bound exceeds 1.0 *and*
   the place survives Benjamini-Hochberg FDR at q = 0.05.
6. **Index** `SRI_i = 100 · RR_i` (100 = citywide average).
7. **Burden** apply a reporting probability only to absolute totals — it is
   mathematically incapable of changing the ranking.

## Two things to read before using this

**Calibrate the tier weights.** `TIER_PRECISION` in `spikelex.py` ships with
priors, not measurements. Hand-label at least 300 records per tier per city
and replace them.

**Watch the `UNRELIABLE-sparse-or-misspecified` prior flag.** If
`fit_gamma_prior` returns it, the data cannot support place-level inference and
any relative risks you see are artifacts. Aggregate to a coarser unit.
