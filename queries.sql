-- ===========================================================================
-- queries.sql — Runnable SoQL / SQL for non-consensual drugging ("spiking")
-- detection in US public police, CAD, EMS and licensing data.
--
-- SoQL blocks are written as the $query= form and can be pasted straight into
-- a browser after the endpoint URL, e.g.
--   https://www.dallasopendata.com/resource/qv6i-rri7.json?$query=<paste>
-- URL-encode the whole thing, or use the Python `socrata()` helper in
-- spikefetch.py which does it for you.
--
-- Get a free app token from the host portal. Anonymous requests are throttled
-- and WILL start failing part-way through a large paged pull, silently
-- truncating your data.
--
-- ORDERING WARNING: Socrata paging with $offset is only stable if you also
-- specify a deterministic $order. Without it, rows can repeat or vanish
-- between pages.
-- ===========================================================================


-- ===========================================================================
-- SECTION 1 — NARRATIVE / FREE-TEXT SCREENS (the only high-precision route)
-- ===========================================================================

-- 1.1 Dallas Police incidents (www.dallasopendata.com / qv6i-rri7)
-- `mo` is a genuine free-text modus-operandi field. This is the single most
-- productive text screen found in a large US Socrata police feed.
-- Verified live: ~3,300 records match the broad term list; after the Python
-- tier logic (negations, agent+cue requirement) only ~36 are explicit Tier-A
-- narrative confirmations, of which about 56% sit at a bar/nightclub premise.
SELECT
  incidentnum, date1, time1, premise, incident_address, zip_code,
  offincident, nibrs_crime, signal, weaponused, drug, victimtype, mo
WHERE
  upper(mo) LIKE '%SPIK%'
  OR upper(mo) LIKE '%DRUGGED%'
  OR upper(mo) LIKE '%ROOFIE%'
  OR upper(mo) LIKE '%GHB%'
  OR upper(mo) LIKE '%ROHYPNOL%'
  OR upper(mo) LIKE '%KETAMINE%'
  OR upper(mo) LIKE '%SLIPPED SOMETHING%'
  OR upper(mo) LIKE '%PUT SOMETHING IN%'
  OR upper(mo) LIKE '%DATE RAPE%'
  OR upper(mo) LIKE '%UNKNOWN SUBSTANCE IN%'
  OR upper(mo) LIKE '%LACED%'
  OR upper(mo) LIKE '%TAMPERED WITH%'
ORDER BY incidentnum
LIMIT 50000;

-- 1.2 Same screen, but returning only the counts you need to size the problem
-- before pulling rows. Run this FIRST against any new jurisdiction: if it
-- returns single digits, venue-level analysis in that city is not viable.
SELECT premise, count(1) AS n
WHERE upper(mo) LIKE '%SPIK%' OR upper(mo) LIKE '%DRUGGED%'
GROUP BY premise
ORDER BY n DESC;

-- 1.3 Boulder CO crime blotter — the only US feed located that publishes a
-- true `auto_narrative`. Tiny (a few hundred rows) but it is the only place to
-- calibrate the Tier-A regex against real US police prose. Resolve the current
-- dataset id from the portal catalogue; Boulder republishes it periodically.
SELECT * WHERE upper(auto_narrative) LIKE '%SPIK%'
   OR upper(auto_narrative) LIKE '%DRUGGED%'
   OR upper(auto_narrative) LIKE '%DRINK%';


-- ===========================================================================
-- SECTION 2 — PREMISE SCREENS FOR THE LARGE POLICE FEEDS
-- (no narrative available; these produce Tier B/C candidates only)
-- ===========================================================================

-- 2.1 New York City. Historic complaints qgea-i56i; year-to-date 5uac-w243.
-- NYC uniquely publishes `loc_of_occur_desc`, which distinguishes INSIDE the
-- premise from OUTSIDE / FRONT OF / REAR OF. That is the closest thing in US
-- open data to a "did this happen in the venue" flag, and it is essential for
-- separating an assault in a club from one on the sidewalk outside it.
SELECT
  cmplnt_num, cmplnt_fr_dt, cmplnt_fr_tm, ofns_desc, pd_desc,
  prem_typ_desc, loc_of_occur_desc, boro_nm, addr_pct_cd,
  latitude, longitude
WHERE
  prem_typ_desc = 'BAR/NIGHT CLUB'
  AND cmplnt_fr_dt >= '2019-01-01T00:00:00.000'
ORDER BY cmplnt_num
LIMIT 50000;

-- 2.2 NYC: the inside/outside split, which you should always report.
SELECT loc_of_occur_desc, ofns_desc, count(1) AS n
WHERE prem_typ_desc = 'BAR/NIGHT CLUB'
GROUP BY loc_of_occur_desc, ofns_desc
ORDER BY n DESC;

-- 2.3 Chicago (data.cityofchicago.org / ijzp-q8t2). Geography is masked to the
-- block, so this supports street-segment analysis at best, never venue-level.
SELECT
  case_number, date, primary_type, description, location_description,
  block, beat, community_area, latitude, longitude
WHERE
  location_description IN ('BAR OR TAVERN', 'TAVERN/LIQUOR STORE')
  AND date >= '2019-01-01T00:00:00.000'
ORDER BY case_number
LIMIT 50000;

-- 2.4 Austin (data.austintexas.gov / fdj4-gpfu). Location is censored to the
-- census block group, so venue-level attribution is impossible by design.
-- Useful only as an area-level comparison city.
SELECT
  incident_report_number, occ_date_time, category_description,
  location_type, council_district, census_block_group,
  latitude, longitude
WHERE location_type = 'BAR / NIGHTCLUB'
ORDER BY incident_report_number
LIMIT 50000;

-- 2.5 Baltimore, Nashville: same pattern, different premise vocabulary.
--   Baltimore  data.baltimorecity.gov  premise = 'TAVERN/NIGHT CLUB'
--   Nashville  data.nashville.gov      location_description = 'BAR, NIGHT CLUB'
-- Always enumerate the vocabulary before filtering, because these strings are
-- not standardised across agencies:
SELECT premise, count(1) AS n GROUP BY premise ORDER BY n DESC LIMIT 200;


-- ===========================================================================
-- SECTION 3 — ArcGIS: TEMPE AZ, THE ONLY FEED WITH A VENUE NAME
-- ===========================================================================
-- Tempe Calls for Service carries `PlaceName`, an actual venue name on each
-- record. That eliminates the geocode-and-match step entirely, which is why it
-- is the best US starting point despite being a mid-size city.
-- Endpoint:
--   https://services.arcgis.com/lQySeXwbBg53XWDi/arcgis/rest/services/
--   Calls_For_Service/FeatureServer/0/query
-- Verified live: 636,524 total records, 334,472 with a non-null PlaceName,
-- 95 distinct CallType values, NIBRS reporting period 2022-present.

-- 3.1 Candidate records (ArcGIS `where` clause):
--   CallType IN ('Rape Call','Sexual Abuse Call','Overdose Call',
--                'Subject Down Call','Robbery Call','Drunk Disturbing Call',
--                'Liquor Violation Call','Injured/Sick Person Call',
--                'Drugs Call','Theft Call','Fraud Call','Unknown Trouble Call',
--                'Check Welfare Call','Fight Call','Assault Call')
--   AND PlaceName IS NOT NULL AND PlaceName <> ''
--   outFields=PrimaryKey,OccurrenceDatetime,OccurrenceHour,OccurrenceWeekday,
--             ObfuscatedAddress,PlaceName,CallType,FinalCaseTypeTrans,
--             CensusTractID,NeighborhoodName,Latitude,Longitude

-- 3.2 Per-venue denominator in one server-side aggregate call:
--   where=PlaceName IS NOT NULL AND PlaceName <> ''
--   groupByFieldsForStatistics=PlaceName
--   outStatistics=[{"statisticType":"count","onStatisticField":"OBJECTID",
--                   "outStatisticFieldName":"total_calls"}]


-- ===========================================================================
-- SECTION 4 — THE EXPOSURE DENOMINATOR (the part most analyses get wrong)
-- ===========================================================================

-- 4.1 Texas Comptroller Mixed Beverage Gross Receipts
--     (data.texas.gov / naix-2893) — ~3.8M venue-month rows.
-- This is the ONLY true per-venue exposure denominator published anywhere in
-- US open data: monthly liquor, wine, beer and cover-charge receipts keyed to
-- a TABC permit number and a street address.
-- `cover_charge_receipts` is the field that matters most: it separates a
-- nightclub from a restaurant with identical dollar volume. Verified live for
-- Dallas 2020+: 120,654 venue-month rows, 1,773 distinct licensed addresses,
-- $6.86B total receipts, and only 9.9% of addresses reporting any cover charge
-- — so cover charge is a genuinely discriminating signal, not a common one.
SELECT
  location_name, location_address, location_city, location_zip,
  tabc_permit_number, obligation_end_date_yyyymmdd,
  liquor_receipts, wine_receipts, beer_receipts,
  cover_charge_receipts, total_receipts
WHERE
  upper(location_city) = 'DALLAS'
  AND obligation_end_date_yyyymmdd >= '2020-01-01T00:00:00.000'
ORDER BY tabc_permit_number, obligation_end_date_yyyymmdd
LIMIT 400000;

-- 4.2 Collapse to a per-venue denominator, weighting cover charge x3 to
-- up-weight true nightclub operation.
SELECT
  location_address,
  max(location_name) AS venue,
  count(1) AS venue_months,
  sum(total_receipts) AS receipts,
  sum(cover_charge_receipts) AS cover,
  sum(total_receipts) + 3 * sum(cover_charge_receipts) AS exposure_units
WHERE upper(location_city) = 'DALLAS'
GROUP BY location_address
HAVING sum(total_receipts) > 0
ORDER BY exposure_units DESC;

-- 4.3 Licence registries supplying class, capacity and address.
--   Texas TABC        data.texas.gov     7hf9-qc9f
--   New York SLA      data.ny.gov        9s3h-dpkz   (already geocoded)
--   Colorado          data.colorado.gov  ier5-5ms2
--   Illinois (CSV, not Socrata):
--     https://ilcc.illinois.gov/content/dam/soi/en/web/ilcc/datasources/
--     ilcc-licenses-daily-export.csv
SELECT * WHERE upper(license_type) LIKE '%MIXED BEVERAGE%' LIMIT 50000;


-- ===========================================================================
-- SECTION 5 — EMS / FIRE: often a MORE precise address than the police file
-- ===========================================================================
-- Medics are dispatched to a door, not to a block, so these feeds can localise
-- an incident that the police file has masked. They also capture the large
-- share of cases that reach a hospital but never reach a police report.

-- 5.1 San Francisco Fire calls (data.sfgov.org / nuek-vuh3)
SELECT
  call_number, call_type, call_date, received_dttm, address, zipcode_of_incident,
  final_priority, case_location
WHERE
  call_type IN ('Medical Incident', 'Alcohol/Drug Related')
  AND received_dttm >= '2022-01-01T00:00:00.000'
ORDER BY call_number
LIMIT 50000;

-- 5.2 Seattle Fire 911 (data.seattle.gov / kzjm-xkqj)
SELECT address, type, datetime, latitude, longitude, incident_number
WHERE upper(type) LIKE '%AID%' OR upper(type) LIKE '%MEDIC%'
ORDER BY incident_number
LIMIT 50000;


-- ===========================================================================
-- SECTION 6 — POST-INGESTION SQL (run locally, e.g. DuckDB over the CSVs)
-- ===========================================================================

-- 6.1 The full venue score in one query. Mirrors spikescore.score_places():
-- expected cases from tier weights, exposure-normalised rate, Poisson-gamma
-- shrinkage, and the citywide reference rate. Substitute your own fitted
-- :alpha and :beta from fit_gamma_prior() -- do not guess them.
WITH screened AS (
  SELECT
    addr_key,
    venue_name,
    -- Step 1: expected cases = sum of calibrated tier precisions.
    SUM(CASE spike_tier
          WHEN 'A1_explicit_text'         THEN 0.90
          WHEN 'A2_agent_plus_cue'        THEN 0.70
          WHEN 'B1_nibrs_drug_weapon'     THEN 0.55
          WHEN 'B3_dftheft_nightlife'     THEN 0.30
          WHEN 'B2_incap_nightlife'       THEN 0.22
          WHEN 'C1_poisoning_nightlife'   THEN 0.10
          WHEN 'C2_subjectdown_nightlife' THEN 0.04
          ELSE 0 END) AS o_expected,
    COUNT(*) AS n_records,
    AVG(CASE WHEN provenance_ambiguous THEN 1.0 ELSE 0.0 END) AS amb_share
  FROM incidents_screened
  WHERE spike_tier IS NOT NULL
  GROUP BY addr_key, venue_name
),
exposure AS (
  SELECT
    location_address AS addr_key,
    (SUM(total_receipts) + 3 * SUM(cover_charge_receipts)) / 1000.0 AS x_exposure
  FROM tx_receipts
  GROUP BY location_address
  HAVING SUM(total_receipts) > 0
),
-- Step 2: LEFT JOIN from exposure, never from the incidents. Licensed premises
-- with zero cases must stay in the denominator or every rate is biased upward.
joined AS (
  SELECT
    e.addr_key,
    s.venue_name,
    COALESCE(s.o_expected, 0) AS o_expected,
    COALESCE(s.n_records, 0)  AS n_records,
    COALESCE(s.amb_share, 0)  AS amb_share,
    e.x_exposure
  FROM exposure e
  LEFT JOIN screened s ON s.addr_key = e.addr_key
),
citywide AS (
  SELECT SUM(o_expected) / SUM(x_exposure) AS theta_bar FROM joined
)
SELECT
  j.venue_name,
  j.addr_key,
  j.n_records,
  ROUND(j.o_expected, 2)                                   AS o_expected,
  ROUND(c.theta_bar * j.x_exposure, 3)                     AS e_expected,
  -- Step 3: raw relative risk
  ROUND(j.o_expected / NULLIF(c.theta_bar * j.x_exposure, 0), 2) AS rr_raw,
  -- Step 4: Poisson-gamma posterior mean
  ROUND((:alpha + j.o_expected)
        / (:beta + c.theta_bar * j.x_exposure), 2)          AS rr_eb,
  -- shrinkage weight
  ROUND((c.theta_bar * j.x_exposure)
        / (:beta + c.theta_bar * j.x_exposure), 3)          AS shrink_w,
  -- Step 6: reporting index, 100 = citywide average
  ROUND(100 * (:alpha + j.o_expected)
        / (:beta + c.theta_bar * j.x_exposure), 0)          AS sri,
  j.amb_share
FROM joined j CROSS JOIN citywide c
WHERE j.x_exposure >= 10
ORDER BY sri DESC;

-- 6.2 Concentration test. If your spiking signal is NOT concentrated on a
-- small share of places, your ascertainment is measuring noise. The
-- street-segment benchmark is roughly half of crime on 4-5% of segments;
-- risky-facilities work on licensed premises found 3 of 30 pubs carrying just
-- over half of all assaults.
WITH ranked AS (
  SELECT addr_key, o_expected,
         SUM(o_expected) OVER (ORDER BY o_expected DESC
                               ROWS UNBOUNDED PRECEDING) AS cum,
         SUM(o_expected) OVER ()                         AS total,
         ROW_NUMBER() OVER (ORDER BY o_expected DESC)    AS rn,
         COUNT(*)     OVER ()                            AS n
  FROM venue_scores
)
SELECT ROUND(100.0 * rn / n, 2) AS pct_places,
       ROUND(100.0 * cum / total, 2) AS pct_cases
FROM ranked
WHERE cum / total IN (
  SELECT MIN(cum / total) FROM ranked WHERE cum / total >= 0.25
  UNION SELECT MIN(cum / total) FROM ranked WHERE cum / total >= 0.50
  UNION SELECT MIN(cum / total) FROM ranked WHERE cum / total >= 0.80
);

-- 6.3 Provenance diagnostic: are your "hotspots" actually hospitals and police
-- stations? Run this BEFORE believing any venue ranking. Location codes for
-- 'Other/Unknown' and 'Doctor's Office/Hospital' are the classic tells for
-- displaced geocoding, because a case recorded where the victim reported will
-- land on the facility, not the venue.
SELECT
  CASE
    WHEN upper(addr_key) LIKE '%HOSPITAL%'  THEN 'hospital'
    WHEN upper(addr_key) LIKE '%MEDICAL%'   THEN 'hospital'
    WHEN upper(addr_key) LIKE '%POLICE%'    THEN 'police'
    WHEN upper(addr_key) LIKE '%PRECINCT%'  THEN 'police'
    ELSE 'other'
  END AS provenance,
  COUNT(*)         AS records,
  SUM(o_expected)  AS expected_cases
FROM venue_scores
GROUP BY 1 ORDER BY expected_cases DESC;

-- 6.4 Venue-TYPE rollup. This is the level at which US public data can
-- actually support inference. Verified live in Tempe: bars/nightclubs 7.9x and
-- campus 6.3x the citywide exposure-adjusted rate, both surviving FDR control,
-- with a 22x spread between the highest and lowest venue type -- while ZERO
-- individual venues were separable from the citywide mean.
SELECT
  venue_type,
  COUNT(*)                                    AS venues,
  SUM(o_expected)                             AS expected_cases,
  SUM(x_exposure)                             AS exposure,
  ROUND(1000 * SUM(o_expected) / SUM(x_exposure), 3) AS rate_per_1k_exposure,
  ROUND(100.0 * SUM(o_expected) / SUM(SUM(o_expected)) OVER (), 1) AS pct_of_cases
FROM venue_scores
GROUP BY venue_type
ORDER BY expected_cases DESC;
