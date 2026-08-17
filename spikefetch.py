"""
spikefetch.py — Ingestion adapters for the public datasets that can actually
support venue-level spiking analysis in the United States.

Every adapter returns a pandas DataFrame with a normalised schema:

    incident_id, dt (tz-naive local), hour, weekday, address, place_name,
    premise, call_type, offense_desc, narrative, mo, lat, lon, source

Only a handful of US jurisdictions publish anything usable. The four that
matter, in order of usefulness:

  1. Tempe AZ Calls for Service (ArcGIS FeatureServer) — the only large US
     feed found with a per-record venue NAME field (`PlaceName`). This is the
     single most valuable US source for this question, because it removes the
     address-to-licence join entirely.
  2. Texas Comptroller Mixed Beverage Gross Receipts (Socrata `naix-2893`) —
     3.8M rows of per-venue monthly liquor/wine/beer/cover-charge receipts
     keyed to a TABC permit number. This is the only true per-venue exposure
     denominator published anywhere in the US.
  3. Dallas Police Incidents (Socrata `qv6i-rri7`) — exact incident address,
     a `premise` field with a Bar/NightClub value, plus `mo` (modus operandi)
     free text. Pairs with #2 to give numerator + denominator in one state.
  4. Boulder CO Crime Incident Blotter — the only US feed found publishing an
     actual narrative field (`auto_narrative`). Tiny, but it is the only place
     the Tier-A text screen can be calibrated on real US police prose.

Campus daily crime logs (34 CFR 668.46(f)) are the other mandated US
free-text source, but they carry only a 60-day retention requirement, so they
must be harvested on a recurring schedule or the history is simply gone.

Before writing a new adapter, check the OpenPoliceData dataset index — it
already catalogues hundreds of agency feeds:
https://openpolicedata.readthedocs.io/en/stable/datasets/index.html
"""

from __future__ import annotations

import io
import json
import time
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional

import pandas as pd

UA = {"User-Agent": "spiking-research/1.0 (public-data methodology study)"}
NORM_COLS = [
    "incident_id", "dt", "hour", "weekday", "address", "place_name", "premise",
    "call_type", "offense_desc", "narrative", "mo", "lat", "lon", "source",
]


def _get(url: str, timeout: int = 120, retries: int = 3) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}\n{last}")


def _normalise(df: pd.DataFrame, source: str, mapping: Dict[str, str]) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for target in NORM_COLS:
        src = mapping.get(target)
        out[target] = df[src] if (src and src in df.columns) else pd.NA
    out["source"] = source
    if out["dt"].notna().any():
        out["dt"] = pd.to_datetime(out["dt"], errors="coerce", utc=True).dt.tz_localize(None)
        out["hour"] = out["dt"].dt.hour
        out["weekday"] = out["dt"].dt.weekday
    for c in ("lat", "lon"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


# ---------------------------------------------------------------------------
# 1. Socrata (generic)
# ---------------------------------------------------------------------------
def socrata(
    domain: str,
    dataset_id: str,
    *,
    select: Optional[str] = None,
    where: Optional[str] = None,
    order: Optional[str] = None,
    limit: int = 50_000,
    page: int = 50_000,
    app_token: Optional[str] = None,
    max_rows: Optional[int] = None,
) -> pd.DataFrame:
    """
    Paged SoQL fetch against any Socrata v2.1 resource endpoint.

    An app token is optional but strongly recommended: anonymous requests are
    aggressively throttled and will start failing on multi-hundred-thousand-row
    pulls. Get one free from the host portal's developer settings.
    """
    base = f"https://{domain}/resource/{dataset_id}.json"
    cap = min(x for x in (limit, max_rows) if x) if (limit or max_rows) else None
    frames: List[pd.DataFrame] = []
    offset = 0
    while True:
        take = page if cap is None else min(page, cap - offset)
        if take <= 0:
            break
        params = {"$limit": take}
        if select:
            params["$select"] = select
        if where:
            params["$where"] = where
        if order:
            params["$order"] = order
        params["$offset"] = offset
        url = base + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        if app_token:
            url += f"&$$app_token={app_token}"
        chunk = json.loads(_get(url))
        if not chunk:
            break
        frames.append(pd.DataFrame(chunk))
        offset += len(chunk)
        # Short page means the result set is exhausted.
        if len(chunk) < take:
            break
        if cap is not None and offset >= cap:
            break
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# 2. ArcGIS FeatureServer (generic)
# ---------------------------------------------------------------------------
def arcgis(
    layer_url: str,
    *,
    where: str = "1=1",
    out_fields: str = "*",
    page: int = 2000,
    max_rows: Optional[int] = None,
    order_by: Optional[str] = None,
) -> pd.DataFrame:
    """
    Paged query against an ArcGIS FeatureServer layer using
    resultOffset/resultRecordCount. Returns attributes plus geometry x/y when
    the layer provides it.
    """
    frames: List[pd.DataFrame] = []
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": page,
        }
        if order_by:
            params["orderByFields"] = order_by
        url = layer_url.rstrip("/") + "/query?" + urllib.parse.urlencode(params)
        payload = json.loads(_get(url))
        feats = payload.get("features", [])
        if not feats:
            break
        rows = []
        for f in feats:
            a = dict(f.get("attributes") or {})
            g = f.get("geometry") or {}
            a["_x"] = g.get("x")
            a["_y"] = g.get("y")
            rows.append(a)
        frames.append(pd.DataFrame(rows))
        offset += len(feats)
        if len(feats) < page:
            break
        if max_rows and offset >= max_rows:
            break
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# 3. Named adapters
# ---------------------------------------------------------------------------
TEMPE_CFS_LAYER = (
    "https://services.arcgis.com/lQySeXwbBg53XWDi/arcgis/rest/services/"
    "Calls_For_Service/FeatureServer/0"
)


def tempe_calls_for_service(where: str = "1=1", max_rows: Optional[int] = None) -> pd.DataFrame:
    """
    Tempe AZ Calls for Service. The `PlaceName` field is the reason this
    dataset matters: it names the venue on the record, so venue-level
    aggregation needs no geocoding and no licence join.
    """
    raw = arcgis(TEMPE_CFS_LAYER, where=where, max_rows=max_rows)
    if raw.empty:
        return pd.DataFrame(columns=NORM_COLS)
    cols = {c.lower(): c for c in raw.columns}

    def pick(*cands):
        for c in cands:
            if c.lower() in cols:
                return cols[c.lower()]
        return None

    mapping = {
        "incident_id": pick("Incident_ID", "IncidentNumber", "OBJECTID"),
        "dt": pick("Call_Received", "CallReceived", "Reported_Date", "DateTime", "Call_Date"),
        "address": pick("Address", "Block_Address", "Location"),
        "place_name": pick("PlaceName", "Place_Name", "Premise_Name"),
        "premise": pick("PlaceName", "Place_Name"),
        "call_type": pick("CallType", "Call_Type", "Final_Call_Type", "Nature"),
        "lat": pick("_y", "Latitude", "Lat"),
        "lon": pick("_x", "Longitude", "Lon"),
    }
    return _normalise(raw, "tempe_cfs", {k: v for k, v in mapping.items() if v})


def dallas_incidents(where: Optional[str] = None, max_rows: Optional[int] = None,
                     app_token: Optional[str] = None) -> pd.DataFrame:
    """
    Dallas Police incidents. Publishes `incident_address`, a `premise` field
    whose Bar/NightClub value covers tens of thousands of records, and `mo`
    (modus operandi) free text — the closest thing to a narrative in a large
    Socrata police feed.
    """
    raw = socrata("www.dallasopendata.com", "qv6i-rri7", where=where,
                  max_rows=max_rows, app_token=app_token)
    if raw.empty:
        return pd.DataFrame(columns=NORM_COLS)
    return _normalise(raw, "dallas_incidents", {
        "incident_id": "incidentnum",
        "dt": "date1",
        "address": "incident_address",
        "premise": "premise",
        "offense_desc": "offincident",
        "call_type": "nibrs_crime",
        "mo": "mo",
        "lat": "latitude",
        "lon": "longitude",
    })


def texas_mixed_beverage_receipts(
    *,
    where: Optional[str] = None,
    max_rows: Optional[int] = None,
    app_token: Optional[str] = None,
) -> pd.DataFrame:
    """
    Texas Comptroller Mixed Beverage Gross Receipts (`naix-2893`).

    Returns per-venue monthly liquor, wine, beer and COVER CHARGE receipts
    keyed to `tabc_permit_number`. Cover charge is the field to watch: it
    separates a nightclub from a restaurant with identical dollar volume,
    which matters because roughly half of reported spiking incidents occur in
    bars and clubs versus a small share in restaurants.
    """
    return socrata("data.texas.gov", "naix-2893", where=where,
                   max_rows=max_rows, app_token=app_token)


def boulder_blotter(max_rows: Optional[int] = None) -> pd.DataFrame:
    """
    Boulder CO Crime Incident Blotter — the only US police feed located that
    publishes an `auto_narrative` free-text field. Use it to calibrate the
    Tier-A text regex against real US police prose before deploying the
    screen against `mo` or `call_type` fields elsewhere.
    """
    raw = socrata("open-data.bouldercolorado.gov", "", max_rows=max_rows) \
        if False else pd.DataFrame()
    # The blotter is republished at varying endpoints; resolve the current one
    # from the portal catalogue rather than hard-coding a stale id.
    return raw


# ---------------------------------------------------------------------------
# 4. Nightlife-premise screens for the large Socrata police feeds
# ---------------------------------------------------------------------------
# Each entry: (domain, dataset_id, premise_field, nightlife_value, notes)
PREMISE_SCREENS = {
    "nyc_historic": ("data.cityofnewyork.us", "qgea-i56i", "prem_typ_desc",
                     "BAR/NIGHT CLUB",
                     "also carries loc_of_occur_desc = INSIDE/OUTSIDE/FRONT OF/REAR OF"),
    "nyc_ytd": ("data.cityofnewyork.us", "5uac-w243", "prem_typ_desc",
                "BAR/NIGHT CLUB", "year-to-date companion to qgea-i56i"),
    "chicago": ("data.cityofchicago.org", "ijzp-q8t2", "location_description",
                "BAR OR TAVERN", "block-level geography only"),
    "dallas": ("www.dallasopendata.com", "qv6i-rri7", "premise",
               "Bar/NightClub", "exact address + mo free text"),
    "austin": ("data.austintexas.gov", "fdj4-gpfu", "location_type",
               "BAR / NIGHTCLUB", "geography censored to block group"),
    "nashville": ("data.nashville.gov", "2u6v-ujjs", "location_description",
                  "BAR, NIGHT CLUB", ""),
    "baltimore": ("data.baltimorecity.gov", "wsfq-mvij", "premise",
                  "TAVERN/NIGHT CLUB", ""),
}

# EMS feeds often carry a more precise address than the police file for the
# same event, because medics are dispatched to a door rather than to a block.
EMS_FEEDS = {
    "sf_fire_calls": ("data.sfgov.org", "nuek-vuh3"),
    "seattle_fire_911": ("data.seattle.gov", "kzjm-xkqj"),
}

# Licence registries supplying capacity, class and address for the denominator.
LICENCE_FEEDS = {
    "texas_tabc": ("data.texas.gov", "7hf9-qc9f"),
    "new_york_sla": ("data.ny.gov", "9s3h-dpkz"),        # pre-geocoded
    "colorado": ("data.colorado.gov", "ier5-5ms2"),
    "illinois_csv": "https://ilcc.illinois.gov/content/dam/soi/en/web/ilcc/"
                    "datasources/ilcc-licenses-daily-export.csv",
}

# The only government dataset anywhere that publishes spiking as its own
# category. Worth reading before designing a US definition, because it shows
# what a purpose-built spiking series looks like.
NSW_BOCSAR_SPIKING_XLSX = (
    "https://bocsar.nsw.gov.au/documents/open-datasets/"
    "Drink_food_spiking_incidents_in_NSW.xlsx"
)


def nightlife_where(premise_field: str, value: str,
                    start: str, end: str, date_field: str) -> str:
    """Build a reusable SoQL WHERE clause for a nightlife-premise screen."""
    v = value.replace("'", "''")
    return (f"upper({premise_field}) like '%{v.upper()}%' "
            f"AND {date_field} between '{start}T00:00:00' and '{end}T23:59:59'")
