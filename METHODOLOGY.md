# Locating Non-Consensual Drugging in Public Data: Prevalence, Reporting Pathways, and a Venue-Level Detection Formula

## Executive Summary

Non-consensual drugging is a high-prevalence, near-zero-visibility crime in American public data. Surveys of university populations find that 7.8% of students report having been drugged ([American Psychological Association](https://www.apa.org/pubs/journals/releases/vio-vio0000060.pdf)), yet no US police dataset has an offense code for it. In the FBI's national incident reporting standard, adulterating someone's food or drink falls into `90Z All Other Offenses` — a residual bucket. Austin's police department has confirmed publicly that drugging is not a standalone category and gets logged as either sexual assault or theft ([KXAN](https://www.yahoo.com/news/drugging-austin-why-difficult-track-001822070.html)).

This report resolves four questions and delivers a working implementation.

**Where it happens.** The venue distribution is the most consistent finding in the literature. UK government analysis puts bars and clubs at 50% of incidents, private homes at 22%, and pubs at 14% ([Home Office](https://www.gov.uk/government/publications/understanding-and-tackling-spiking/report-understanding-and-tackling-spiking-accessible)); a parliamentary committee reviewing 4,506 incidents found 87% occurred in nightclubs and pubs ([Home Affairs Committee](https://publications.parliament.uk/pa/cm5802/cmselect/cmhaff/967/report.html)); Australia's national study found 67% at a licensed venue ([Australian Institute of Criminology](https://www.aic.gov.au/sites/default/files/2020-05/national-project-on-drink-spiking-investigating-the-nature-and-extent-of-drink-spiking-in-australia.pdf)). Original analysis conducted for this report produces the first comparable US figure: among Dallas police records whose narrative text explicitly describes a drugging, 55.6% occurred at a bar or nightclub premise.

**Whether it is reported at the hospital or at the venue.** Both, and neither, and this is the single most important finding for anyone building a detection pipeline. Victims tell the venue slightly more often than the police (16% versus 14%), and the hospital least of all (9%) — while 75% tell no one ([Home Affairs Committee](https://publications.parliament.uk/pa/cm5802/cmselect/cmhaff/967/report.html)). But the cases that *do* enter records enter overwhelmingly through health systems: one hospital catchment study recorded 75 emergency-department cases against 3 police-recorded incidents in the same area and year, a ratio of roughly 25:1 ([Emergency Medicine Journal](https://pmc.ncbi.nlm.nih.gov/articles/PMC2658214/)). Crucially, US crime data records the location where the offense *occurred*, not where it was reported — there is a report-date indicator in the national standard but no report-location indicator. That makes every address provenance-ambiguous and demands explicit handling.

**The smallest reliable geography.** Individual venue names, but only in a handful of jurisdictions, and only for aggregate patterns rather than named-venue rankings. Health data cannot go below the state level. Analysis performed for this report demonstrates the hard limit empirically: in the best-equipped US city available, zero individual venues were statistically separable from the citywide average, while venue *categories* differed by a factor of 22.

**The formula.** A seven-step estimator combining probabilistic case ascertainment, exposure normalization, Poisson-gamma shrinkage, and false-discovery control. It is specified in full below and implemented in runnable Python and SQL, validated against simulated data with known ground truth and executed against roughly 200,000 live public records.

---

## Part 1: How Common Is It, and What Fraction Is Real?

### Prevalence

The best-designed US prevalence study surveyed 6,064 students across three universities and found 7.8% reported having been drugged — 9.5% of women and 4.2% of men — with site-level rates ranging from 6.0% to 10.8%. In the same sample, 1.4% admitted to drugging someone else ([American Psychological Association](https://www.apa.org/news/press/releases/2016/05/drink-spiking)). That site-level range matters: a threefold spread across three campuses tells you this is a phenomenon driven by local environment, which is precisely what makes venue-level analysis worth attempting.

National victimization data approaches it from the assault side. The National Intimate Partner and Sexual Violence Survey found 2.3% of women had experienced involuntary drug- or alcohol-facilitated rape, approximately 2.6 million people ([NISVS analysis](https://pmc.ncbi.nlm.nih.gov/articles/PMC8355168/)). Later NISVS data shows steep variation by sexual orientation: 11.8% of heterosexual women reported alcohol- or drug-facilitated penetration, versus 25.1% of bisexual women and 16.8% of gay men ([CDC](https://stacks.cdc.gov/view/cdc/98137/cdc_98137_DS1.pdf)). The Association of American Universities' climate survey found 5.4% of undergraduate women reported incapacitation-penetration since entering college ([AAU](https://www.aau.edu/sites/default/files/%40%20Files/Climate%20Survey/AAU_Campus_Climate_Survey_12_14_15.pdf)).

Among students who reported being unknowingly drugged, 14.5% also reported sexual violence ([NISVS analysis](https://pmc.ncbi.nlm.nih.gov/articles/PMC8355168/)) — meaning most drugging incidents are not sexual assaults, and a detection strategy that only screens sex-offense records will miss the majority of cases.

### The toxicology reality, and why it constrains everything

A critical caveat runs underneath all of this. Testing of 6,051 drug-facilitated crime cases found positive results in 65.2% of blood and 80.1% of urine samples, but the substances detected were dominated by cannabinoids and ethanol. The drugs the public associates with spiking were vanishingly rare: GHB in 0.76% of cases, ketamine 0.30%, and flunitrazepam — Rohypnol — in 0.042% ([Journal of Analytical Toxicology](https://pmc.ncbi.nlm.nih.gov/articles/PMC12584120/)). An independent series of 1,000 cases found 78.4% positive with ethanol in 30.9% ([Journal of Forensic and Legal Medicine](https://www.sciencedirect.com/science/article/abs/pii/S1752928X18304591)).

When researchers restrict to *surreptitious* administration only — someone covertly putting a substance into another person's drink — the confirmed rate falls to 4.2% of examined drug-facilitated sexual assault cases, with a 95% confidence interval of 0.89% to 7.44% ([National Institute of Justice](https://www.ojp.gov/pdffiles1/nij/grants/212000.pdf)). A separate review put proven covert administration at approximately 2% ([NCJRS](https://www.ojp.gov/ncjrs/virtual-library/abstracts/involvement-drugs-and-alcohol-drug-facilitated-sexual-assault)).

Detection windows explain much of the gap: GHB is detectable in urine for roughly 12 hours and blood for 6, benzodiazepines for at least 24 hours, ketamine around 24 hours ([Journal of Clinical and Diagnostic Research](https://pmc.ncbi.nlm.nih.gov/articles/PMC2750925/)). Mean delay from incident to emergency-department presentation was 5.9 hours in a London series and up to 20 hours to sample collection. The evidence is often gone before anyone looks.

The practical consequence for a detection formula is decisive: **any screen must output probabilistic case weights, not counts.** A record that looks like spiking has perhaps a 2–8% chance of being confirmed covert administration if the surreptitious-only literature applies, and a much higher chance if the victim's own account is treated as sufficient. Assigning integer counts to suspicion signals manufactures false precision, and the formula below is built specifically to avoid it.

---

## Part 2: Hospital, Venue, or Police? The Reporting-Pathway Problem

### What victims actually do

Survey evidence on the three-way split is unambiguous and sobering. Among victims surveyed by a UK parliamentary committee, 16% reported to the venue, 14% to the police, 9% to a hospital or emergency department, and 3% to a general practitioner — while 75% did not report at all. Witnesses behaved differently, telling the venue 47% of the time, the emergency department 22%, and the police 15% ([Home Affairs Committee](https://publications.parliament.uk/pa/cm5802/cmselect/cmhaff/967/report.html)). An independent survey found a similar pattern: venue 13%, police 8%, emergency department 9%.

So victims report to venues *marginally more often* than to police, and to hospitals least. But that is about disclosure, not records. The single most instructive study compared what each system captured in the same geography over the same year: 75 emergency-department cases of suspected spiking against just 3 police-recorded incidents. Only 14% told police. Of those ED cases, 82% occurred on a weekend and 66% between 22:00 and 03:00 ([Emergency Medicine Journal](https://pmc.ncbi.nlm.nih.gov/articles/PMC2658214/)).

**The two facts must be held together.** Victims disclose to venues most often, but venues generate no public record — no US state mandates venue-to-police reporting. Hospitals receive the fewest disclosures but generate the most durable records. Police receive few and generate the only geocoded, publicly available ones. Public police data is therefore a small, non-random sample of a large hidden population, and the ratio between health and police capture may be as extreme as 25:1.

### The location-coding trap

Here is the technical crux. In the FBI's national incident-based reporting standard, Data Element 9 (Location Type) records where the **offense took place**, and the reporting agency is assigned by place of occurrence. The standard includes a Report Date Indicator flag but contains **no report-location indicator** ([FBI NIBRS User Manual](https://showmecrime.mo.gov/CrimeReporting/documents/2025.0NIBRSUserManual.pdf), [NIBRS Technical Specification](https://showmecrime.mo.gov/CrimeReporting/documents/2023.0NIBRSTechnicalSpecification_final.pdf)).

In principle that is exactly what an analyst wants. In practice, when a victim wakes up hours later with no memory and presents at an emergency department, the officer taking the report frequently has no venue to code — so the record lands on the hospital, the police station, or a residual code. The International Association of Chiefs of Police supplemental form for sexual assault makes this visible by separating "Location of Interview: Hospital / On Scene / At Department" from the free-text assault-location fields ([IACP](https://www.theiacp.org/sites/default/files/all/2016%20SA%20Supplemental%20Report%20Form.pdf)) — but that distinction does not survive into published open data.

Two location codes are diagnostic tells for this displacement: `09 Doctor's Office/Hospital` and `25 Other/Unknown`. Any pipeline must treat addresses as **provenance-ambiguous** and maintain hospital and police-station exclusion lists.

A well-documented cluster illustrates the drift concretely. Investigators identified 43 drugging incidents tied to Manhattan clubs since September 2021, including drug-aided robberies and seven deaths; victims were targeted at Hell's Kitchen bars but were found in taxis and apartments, sometimes far from the venue ([ABC News](https://abcnews.go.com/US/indictments-obtained-drugging-deaths-2-men-new-york/story?id=98232618), [CNN](https://www.cnn.com/2025/02/13/us/new-york-city-bar-nightclub-robberies-conviction)). Geocode the discovery address and you map taxi routes, not bars.

### Empirical confirmation from live data

Analysis conducted for this report tested the displacement hypothesis directly against 65,001 Tempe, Arizona call records carrying a venue name. The results show the problem is real but smaller than feared in a call-for-service feed:

| Call type | Share at apartment/residence | Share at hospital or clinic | Share at police/justice facility |
| --- | --- | --- | --- |
| Rape Call (n=688) | 30.1% | below top 5 | 4.8% |
| Sexual Abuse Call (n=373) | 20.4% | below top 5 | below top 5 |
| Overdose Call (n=1,162) | 25.3% | below top 5 | below top 5 |

Roughly 4.8% of rape calls were logged at a police or justice facility — direct evidence of report-location coding — while hospitals absorbed under 1% of total screened signal. Call-for-service data, captured at dispatch, appears less displaced than incident-report data, which is an argument for preferring CAD feeds where available.

### Health data cannot rescue the geography

Since hospitals hold the better case capture, the obvious move is to use health data. The geography makes it impossible. The Nationwide Emergency Department Sample provides national and regional estimates only, and the region variable was removed in 2023 ([HCUP](https://hcup-us.ahrq.gov/nedsoverview.jsp)). State Emergency Department Databases are state-level ([HCUP](https://hcup-us.ahrq.gov/seddoverview.jsp)). The CDC's syndromic surveillance platform is access-gated ([CDC](https://www.cdc.gov/nssp/php/about/about-nssp-and-the-biosense-platform.html)), and poison-center data publishes at state level ([National Poison Data System](https://poisoncenters.org/national-poison-data-system)).

For completeness, the relevant assault-intent poisoning codes are `T42.4x3A` (benzodiazepines), `T42.6x3A` (sedative-hypnotics), `T43.8x3A` and `T43.93xA` (psychotropics), and the `T40.0x3A`–`T40.693A` opioid range, alongside `T74.21XA` and `T76.21XA` for confirmed and suspected adult sexual abuse ([Illinois Department of Public Health](https://dph.illinois.gov/content/dam/soi/en/web/idph/files/publications/icd-10-cm-violent-injury-codes.pdf)). One caveat matters: many spiking presentations are coded as *undetermined* intent rather than assault, so an assault-only case definition undercounts.

**Conclusion: health data establishes magnitude; police data establishes place. Neither does both.**

---

## Part 3: What Public Data Can Actually Support

### The four datasets that matter

Most US police open data cannot answer this question because it lacks either a narrative field or a precise address. Four sources are exceptions.

| Source | Why it matters | Limitation |
| --- | --- | --- |
| **Tempe AZ Calls for Service** (ArcGIS) | The only large US feed with a per-record venue **name** field (`PlaceName`) — eliminates geocoding entirely. Verified live: 636,524 records, 334,472 with a venue name, 95 call types. | Mid-size city; no narrative text; 2022-present only |
| **Texas Mixed Beverage Gross Receipts** (`naix-2893`) | The only true per-venue **exposure denominator** in US open data: monthly liquor, wine, beer and cover-charge receipts keyed to a licence number and address. ~3.8M rows. | Texas only |
| **Dallas Police Incidents** (`qv6i-rri7`) | Exact `incident_address`, a Bar/NightClub premise value, and a genuine free-text `mo` field. Pairs with the receipts data for numerator plus denominator in one state. | `mo` is terse |
| **Boulder CO Crime Blotter** | The only US feed found publishing a real `auto_narrative`. | A few hundred rows |

Premise-based screens work in the large city feeds, but with degrading geography: New York uniquely publishes a `loc_of_occur_desc` field distinguishing inside the premise from outside it — the closest thing in US open data to a "did this happen in the venue" flag — while Chicago masks location to the block and Austin censors to the census block group, making venue attribution impossible by design.

Two further notes. Emergency medical service feeds, such as San Francisco's and Seattle's fire-call datasets, often carry a **more precise address** than the police file for the same event, because medics are dispatched to a door rather than a block. And campus daily crime logs, mandated under 34 CFR § 668.46(f), are the only free-text source the US legally requires — but they carry only a 60-day retention requirement, so the history is gone unless harvested on a schedule. Clery Act reporting has no drugging category at all.

### The one real precedent

New South Wales, Australia publishes a dataset titled ["Drink, food and needle spiking incidents"](https://bocsar.nsw.gov.au/documents/open-datasets/Drink_food_spiking_incidents_in_NSW.xlsx) — the only government dataset anywhere that treats spiking as its own category. Anyone designing a US definition should read it first.

### Legal context shaping the data

California treats non-consensual drugging as a felony under Penal Code 347(a)(1), carrying two, four or five years ([FindLaw](https://codes.findlaw.com/ca/penal-code/pen-sect-347/)), with a misdemeanor provision at 347b ([Justia](https://law.justia.com/codes/california/code-pen/part-1/title-9/chapter-12/section-347b/)). Florida classifies it as a first-degree felony ([Florida Senate](https://www.flsenate.gov/Laws/Statutes/2012/0859.01)). New York has legislation pending ([New York Senate](https://www.nysenate.gov/legislation/bills/2025/A8613)).

California's AB 1013, effective July 2024, requires roughly 2,400 licensees serving spirits to offer drug-testing devices and post signage ([California ABC](https://www.abc.ca.gov/new-law-requiring-bars-serving-spirits-to-offer-drug-testing-devices-to-take-effect-july-1/)). This creates a natural experiment: a defined treatment group, a known date, and an outcome measurable in the very data described here. It is probably the highest-value study design available in this domain right now.

For scale comparison, UK police recorded 6,732 spiking reports between May 2022 and April 2023, following a quarterly peak of 4,861 ([Home Office](https://www.gov.uk/government/publications/spiking-factsheet/spiking-factsheet)). No equivalent US figure exists, because no US agency counts it.

---

## Part 4: The Formula

### Design requirements

The formula must satisfy five constraints that follow from the evidence above: ascertainment is uncertain, so cases must be probabilistic; venues differ enormously in patronage, so counts must be exposure-normalized; most venues have zero or one event, so raw rates must be shrunk; hundreds of venues are tested at once, so multiplicity must be controlled; and reporting is incomplete, so absolute burden and relative ranking must be separated.

### Step 1: Probabilistic case ascertainment

Each record is assigned to the single highest tier it satisfies, and contributes that tier's calibrated precision:

\[ O_i = \sum_{r \in i} \pi(\text{tier}(r)) \]

| Tier | Firing condition | Prior precision \(\pi\) |
| --- | --- | --- |
| A1 | Explicit narrative text ("drink was spiked", "believes she was drugged") | 0.90 |
| A2 | Named agent (GHB, ketamine, Rohypnol) **plus** an ingestion or victimization cue | 0.70 |
| B1 | Structured weapon/force code = Poison or Drugs/Narcotics/Sleeping Pills | 0.55 |
| B3 | Drug-facilitated theft pattern at a nightlife premise | 0.30 |
| B2 | Incapacitation language at a nightlife or campus premise | 0.22 |
| C1 | Poisoning or sex-offense call at a nightlife premise | 0.10 |
| C2 | Subject-down or EMS-assist call at a nightlife premise in the night window | 0.04 |

The A2 tier requires a co-occurring cue because narcotics-possession reports mention these drugs constantly; without the cue, precision collapses. A negation list removes unfounded reports, false reports, therapeutic ketamine administration by medics, and irrelevant homographs — spike strips, railroad spikes, volleyball.

Tier weights are anchored on the regex-on-narrative benchmark of 0.94 precision and 0.93 recall for hand-built patterns on police narratives, and on the covert-administration base rates discussed in Part 1. **They are priors, not measurements.** They must be replaced with values from a hand-labelled sample of at least 300 records per tier per city.

### Steps 2–3: Exposure and raw rate

\[ E_i = \bar{\theta} \cdot X_i, \qquad \bar{\theta} = \frac{\sum_j O_j}{\sum_j X_j}, \qquad r_i = \frac{O_i}{E_i} \]

Exposure measures in descending preference:

1. **Capacity × late trading hours.** Grounded in licensed-premise research finding venues with capacity 501–1,000 carried about 6.1 times the risk of the smallest band, with each additional high-alcohol trading hour adding roughly 72%.
2. **Alcohol receipts**, with cover charge weighted ×3 — because cover charge separates a nightclub from a restaurant of identical dollar volume, and clubs are where the incidents concentrate. Verified in live Dallas data: only 9.9% of licensed addresses report any cover charge, making it a genuinely discriminating signal.
3. **Licence-class weight × years licensed.**
4. **Ambient or residential population** — worst, and endogenous.

Using per-capacity denominators follows the established practice of expressing licensed-premise assault rates per 100 capacity rather than per venue.

### Step 4: Poisson-gamma empirical-Bayes shrinkage

\[ O_i \sim \text{Poisson}(E_i \theta_i), \qquad \theta_i \sim \text{Gamma}(\alpha, \beta) \]
\[ \theta_i \mid O_i \sim \text{Gamma}(\alpha + O_i,\ \beta + E_i), \qquad RR_i^{EB} = \frac{\alpha + O_i}{\beta + E_i} \]

with shrinkage weight \(w_i = E_i / (E_i + \beta)\). Working on the relative-risk scale keeps \(\beta\) interpretable as the quantity of exposure evidence required to move a venue off the citywide average. Parameters are fitted by method of moments on the standardized-ratio distribution, subtracting the Poisson sampling component \(n / \sum E\) from the observed exposure-weighted variance — the standard rate-smoothing estimator.

The closed form is deliberate: it runs on a laptop, needs no Markov chain, and is auditable line by line.

### Steps 5–6: Decision rule and index

Flag a venue only when the 95% credible lower bound exceeds 1.0 **and** the venue survives Benjamini-Hochberg FDR control at q = 0.05 applied to the posterior exceedance probability \(P(\theta_i \le 1)\). Report \(SRI_i = 100 \cdot RR_i^{EB}\), where 100 is the citywide exposure-adjusted average.

Confirmation, not ranking, is the appropriate role for spatial statistics here: Getis-Ord Gi* or a space-time permutation scan tests whether high-scoring venues form genuine clusters. Predictive accuracy should be reported as \(PAI = \frac{(n/N)}{(a/A)}\), against benchmarks of roughly 7.2 for kernel-density hotspot mapping and 3.5 for risk-terrain modelling.

### Step 7: Under-reporting — and why it cannot change the ranking

This deserves emphasis because it is widely mishandled. Applying a single global reporting probability \(p\) multiplies every venue identically and therefore **cannot reorder them**. It converts a ranking into a burden estimate and nothing more. Only *place-varying* \(p\) shifts ranks — for instance a campus venue whose patrons route to a student health centre rather than to police — and that must be estimated from an emergency-department-to-police linkage, never assumed. Formal treatment of Poisson-binomial thinning models confirms that observational data identifies only the product \(\lambda \cdot p\), not the two separately.

### The demographic question, answered honestly

A Poisson regression with a log-exposure offset yields rate ratios for venue and neighbourhood attributes:

\[ \log E[O_i] = \log X_i + \beta_0 + \sum_k \beta_k Z_{ik} \]

Validation on simulated data recovered a known club rate ratio of 2.5 as 2.45 and a per-late-hour ratio of 1.15 as 1.14. Useful covariates include capacity, licence class, late-hour count, cover-charge presence, distance to campus, and density of licensed premises within 400 metres.

But a hard warning applies. Covariates measured on the surrounding block group describe **the area, not the individuals in it**. Attributing area-level associations to the people at a venue is the ecological fallacy, and it is the most likely way this analysis gets misused. A defensible demographic formula predicts *risk of an environment*; it cannot and should not profile victims or patrons.

---

## Part 5: Validation and Live Results

### Engine validation

The scoring engine was tested against simulated data with known ground truth (800 venues, exposure spanning three orders of magnitude, Gamma-distributed true risk):

| Check | Result |
| --- | --- |
| Prior recovery | True α=4.00, β=4.00 → estimated α=4.19, β=4.19 |
| Accuracy versus raw rates | 65.5% reduction in mean squared error |
| Credible-interval coverage | 95.5% against a 95% nominal target |
| Realized false-discovery proportion | 0.0% among flagged venues |
| Shrinkage behaviour | A low-exposure venue with one freak case moved from a raw 12.6× to 1.34× and was correctly not flagged; a high-exposure venue with a genuine 5× excess held at 4.04× and was flagged |
| Numerics | Gamma CDF matches SciPy to 4×10⁻¹⁴ |

### Live result 1: Tempe, Arizona — venue types are separable, individual venues are not

Running the pipeline against 65,001 live Tempe call records across 3,135 named venues produced 1,145 screened records and 83.8 expected cases. Because Tempe publishes no narrative text, only the weakest tiers could fire — exactly as the data-availability analysis predicted.

At the venue-type level the signal is strong and statistically robust:

| Venue type | Screened records | Expected cases | EB relative risk | 95% credible interval | Elevated after FDR |
| --- | --- | --- | --- | --- | --- |
| Bar or nightclub | 348 | 15.54 | **7.88** | 4.50 – 12.19 | Yes |
| Campus | 219 | 10.98 | **6.31** | 3.20 – 10.46 | Yes |
| Hotel or motel | 41 | 4.10 | 1.29 | 0.39 – 2.72 | No |
| Apartment or residence | 190 | 19.00 | 1.15 | 0.70 – 1.71 | No |
| Restaurant or fast food | 16 | 1.60 | 0.59 | 0.07 – 1.62 | No |
| Retail or grocery | 67 | 6.70 | 0.54 | 0.22 – 1.00 | No |
| Park or public space | 38 | 3.80 | 0.41 | 0.12 – 0.88 | No |

Bars and nightclubs carry roughly **7.9 times** the citywide exposure-adjusted rate and campuses **6.3 times**, both surviving false-discovery control, with a 22-fold spread between highest and lowest categories. Note that apartments and residences hold the largest absolute share of expected cases (22.7%) while sitting near the citywide average once exposure is accounted for — a reminder that raw counts and rates answer different questions.

At the individual-venue level, **zero of 369 venues** were credibly elevated after FDR control. The prior estimator returned a strong-shrinkage flag: with expected counts averaging a fraction of one case per venue, no venue is distinguishable from the citywide mean. This is the correct answer, not a failure.

The concentration profile was also markedly flatter than the crime-concentration benchmark — 25% of expected cases on 3.5% of venues and 50% on 15.5%, against a street-segment benchmark of roughly half of crime on 4–5% of units. Weak concentration is itself evidence that structured proxies are capturing ambient nightlife activity rather than a distinct spiking phenomenon.

### Live result 2: A negative finding worth reporting

An initial run appeared to show 94.4% of expected cases falling in the 21:00–04:59 window, seemingly confirming the 22:00–03:00 concentration seen in hospital series. **That number is an artifact** — tier C2 requires a nightlife hour to fire, so the screen manufactured its own temporal concentration.

Testing only the subset where the hour was *not* part of the firing condition reversed the result: 48.1% of those cases fell in the night window against a baseline of 68.9% of all bar records, a concentration ratio of 0.70. With n=27 the test is badly underpowered, but it provides no support for temporal clustering. Two readings are consistent: the structured proxies are largely capturing ordinary alcohol overdose and sexual-offense calls, or the timestamp reflects call time, which lags the incident by hours — the same delay that defeats toxicology. Either way, **hour-of-day should not be used as a scoring input** until validated against narrative-confirmed cases.

### Live result 3: Dallas — narrative text works, and a US venue estimate

Dallas allowed the strongest test: real narrative text plus a real receipts denominator. A server-side text screen over the `mo` field returned 3,300 candidate records; combined with 14,689 bar-premise records, the pool reached 16,074 unique incidents. After applying the full tier logic, **36 records were explicit Tier-A narrative confirmations**.

The matches are unmistakable:

- "COMP STATED AN UNK INDIV POSS DRUGGED HER DRINK AT BAR"
- "COMP BELIEVES HER DRINK WAS SPIKED"
- "COMP WAS DRUGGED BY AN UNK SUBSTANCE AND DEPRIVED OF HIS WATCH"
- "INJURED PERSON: W/M THINKS HIS DRINK WAS SPIKED, WENT TO HOSPITAL"

Their premise distribution yields the headline US figure:

| Premise | Share of narrative-confirmed records |
| --- | --- |
| **Bar / NightClub / DanceHall** | **55.6%** |
| Hotel / Motel | 8.3% |
| Highway, street, alley | 5.6% |
| Apartment residence | 5.6% |
| Restaurant / food service | 2.8% |

That 55.6% sits below Australia's 67% at licensed venues ([Australian Institute of Criminology](https://www.aic.gov.au/sites/default/files/2020-05/national-project-on-drink-spiking-investigating-the-nature-and-extent-of-drink-spiking-in-australia.pdf)) and well below the UK's 87% in nightclubs and pubs ([Home Affairs Committee](https://publications.parliament.uk/pa/cm5802/cmselect/cmhaff/967/report.html)), but it is the same order of magnitude and derived independently from US police narratives. The drug-facilitated-theft pattern is also visible: several records pair drugging with missing property, matching the documented Manhattan cluster.

The exposure join worked mechanically — 1,773 distinct licensed Dallas addresses carrying $6.86 billion in receipts, with a 62.2% naive address-key match rate against screened incidents. That match rate is itself a finding: string-key joining falls well short of the ≥85% standard expected for geocoded matching, and venue-level publication requires a proper geocode-and-match workflow.

**And the venue-level result was again null.** With only 78 Tier A+B records spread across 1,699 licensed premises, the prior collapsed to α=β=0.001, shrinkage effectively vanished, apparent relative risks inflated into the hundreds, and **zero premises survived FDR control**. The engine now flags this state explicitly as `UNRELIABLE-sparse-or-misspecified`, because a relative risk of 250 in that regime reflects zero-inflation, not a venue that is 250 times more dangerous.

### The bottom line on geographic resolution

| Target resolution | Feasible? | Evidence |
| --- | --- | --- |
| Named individual venue | **No** | 0 of 369 Tempe venues and 0 of 1,699 Dallas premises survived FDR |
| Venue type | **Yes** | Bars 7.9×, campus 6.3×, both FDR-significant, 22× spread |
| Premise mix of confirmed cases | **Yes** | 55.6% at bars in Dallas narratives |
| Street segment / block group | Likely, untested here | Standard crime-concentration methods apply |
| Sub-state health geography | **No** | Federal ED data is national or state only |

---

## Part 6: Implementation and Governance

The accompanying toolkit implements all of the above:

- **`spikelex.py`** — the tiered screen: regex dictionary, premise and call-type vocabularies, NIBRS code primitives, provenance-ambiguity detection, negation handling.
- **`spikescore.py`** — the engine: exposure construction, Poisson-gamma shrinkage, credible intervals, BH-FDR, Getis-Ord Gi*, PAI, concentration curves, Poisson GLM with offset, and the publication gate. No SciPy required.
- **`spikefetch.py`** — paged Socrata and ArcGIS adapters plus a registry of premise screens, EMS feeds, and licence registries.
- **`queries.sql`** — runnable SoQL for every source discussed, plus the full venue score expressed in SQL.
- **`demo_tempe.py`, `demo_venue_types.py`, `demo_dallas.py`** — the three worked examples above.
- **`test_spikescore.py`** — the validation suite.

### Governance rules built into the code

The publication gate suppresses any venue with fewer than three expected cases or fewer than five underlying records, following standard public-health small-numbers practice; suppresses venues whose signal is more than 30% provenance-ambiguous; and names only FDR-surviving venues. Suppressed rows remain available for aggregate and trend reporting.

Three further constraints deserve stating plainly. Cross-jurisdiction comparison of raw counts is invalid, because agencies differ in coding practice — the CDC's own overdose surveillance program prohibits it. Non-punitive governance is essential: an analysis that penalizes venues for reported incidents creates a direct incentive to suppress reports, which is the opposite of the goal. And no output of this pipeline is evidence about any individual business; tier precisions are well below 1.0, and an incident address may be where a victim was found rather than where they were drugged.

### Recommended sequence

1. **Calibrate first.** Hand-label 300+ records per tier on Boulder narratives and Dallas `mo` text before trusting any weight.
2. **Start where the data allows.** Tempe for venue names, Dallas plus Texas receipts for a true denominator.
3. **Report at the level the data supports** — venue type and premise mix, not venue names.
4. **Harvest campus crime logs on a schedule.** The 60-day retention window means unharvested history is permanently lost.
5. **Exploit the California natural experiment.** AB 1013 gives a defined treatment group and a known date for a difference-in-differences design — the strongest available study in this space.
6. **Pursue an ED-to-police linkage.** It is the only route to place-varying reporting probabilities, and therefore the only route to rankings that survive the under-reporting problem.

## Conclusion

The honest answer to "where do these crimes most often occur" is that public US police data can establish, with statistical confidence, that bars and nightclubs carry roughly eight times the exposure-adjusted rate of the average location and campuses roughly six times, and that a majority of narrative-confirmed cases occur at licensed premises. It cannot, with any currently available US dataset, identify which specific bar — not because the method is inadequate, but because between 75% and 96% of incidents never reach a police record at all, and the handful that do are spread too thin across too many venues.

That gap is not a reason to abandon the analysis. It is the analysis's most important output, and it points directly at the two interventions that would close it: a spiking-specific offense code of the kind New South Wales already publishes, and a linkage between emergency-department and police records that would reveal how much each venue's silence is worth.
