# LinkedIn Enrichment Pipeline

Finds a **reachable professional route** for each person in a prospect workbook:
the most likely public LinkedIn profile where one can be confirmed, and the
organisation's published contact routes where it cannot.

Built for a specific dataset: 312,160 company directors in Bangalore and
Hyderabad, exported from PrivateCircle's scrape of the Indian MCA/ROC registry.
The design decisions below follow from what that data actually contains.

**The core rule: never fabricate.** A row that cannot be resolved with the
required confidence is left blank and told why. Near-misses go to a separate
review file, never into the LinkedIn column.

**Two outputs, scored independently**, because they are different claims:

| | `LinkedIn Profile` | `Contact Routes` |
|---|---|---|
| What it asserts | *this profile is this human* | *this organisation publishes this way in* |
| Gated on | confidence ≥ threshold **and** corroboration | nothing — it is a checkable fact |
| Wrong answer costs | a call to the wrong person | a call to a switchboard |

That split is what raises yield. A row succeeds when **either** holds, and the
company is discoverable far more often than the individual is.

---

## Table of contents

- [What the data supports](#what-the-data-supports)
- [How matching works](#how-matching-works)
- [Contact routes](#contact-routes)
- [The query ladder](#the-query-ladder)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Architecture](#architecture)
- [Resume and interruption](#resume-and-interruption)
- [Performance tuning](#performance-tuning)
- [Calibration](#calibration)
- [Compliance](#compliance)
- [Troubleshooting](#troubleshooting)
- [What to realistically expect](#what-to-realistically-expect)

---

## What the data supports

Measured across all 312,160 rows of the production export:

| Column | Filled | Useful for matching? |
|---|---|---|
| Name | 100% | Yes — primary signal |
| Company | 100% | **Yes — the decisive signal** |
| Designation | 100% | Barely. 87% is literally `Director` |
| Location | 100% | Barely. Only `Bangalore` / `Hyderabad` |
| Industry | 95.9% | Weak — 21 coarse buckets |
| **Revenue** | **0%** | **No — the column is entirely empty** |
| **Employees** | **0%** | **No — the column is entirely empty** |
| **Website** | **0%** | **No — the column is entirely empty** |
| PrivateCircle URL | 100% | Not a signal, but useful provenance |

Two facts drive the whole design:

1. **"Director" is a registry role, not a job title.** These are MCA board
   appointments. Someone who is a registry director of a two-person company
   usually has an unrelated LinkedIn headline. Designation therefore carries a
   tiny weight and a *mismatch never counts against a candidate*.

2. **The population is mostly micro-companies.** Of 140,351 distinct company
   strings — 139,548 after brand normalisation collapses legal-form and OCR
   variants, which is the number the query ladder actually pays for —
   24,947 have one person in the file and 81,524 have exactly two — Indian
   private limited companies legally require two directors. Most have no web
   presence at all. Only a small minority are recognisable names.

Matching therefore rests almost entirely on **Name × Company**.

## How matching works

Seven live searches were run against this dataset while building the pipeline.
They are recorded verbatim in `linkedin_enrichment/tests/fixtures/serp/` and are
the regression suite. Four of them are false-positive traps:

| Case | What the search returned | Consequence |
|---|---|---|
| `"Girish Rowjee" "Greytip Software"` | `linkedin.com/in/girishrowjee` — name and company both in the title | The true-positive shape |
| `"Debasheesh Bagchi" "Ubiqtech Software"` | That exact person — **at a different employer** | Name alone produces false positives |
| Roster of `Synthesis Winding Technologies` | Ten genuine employees, **none of them the subject** | Company alone produces false positives |
| `"Praveen Kumar" Director Hyderabad` | Eight different, equally plausible people | Homonyms are the norm (54 in the file) |
| Roster of `Greytip Software` | Nine employees — **but not the founder** | Roster queries have limited recall |
| `"Zainab Fatima Fast Foods"` | Profiles matching only the *person's* name | An eponymous company can't corroborate itself |
| `"Natesh Impex"` | Unrelated noise | "No footprint" ≠ "no results returned" |

**The central finding: name alone and company alone each produce
confident-looking false positives. Only the conjunction is safe.**

So the score treats them as a product, not a sum:

```
core       = name_similarity × company_coverage      (weight 0.70)
raw_score  = core + slug + city + locale + industry + designation   (0.30 total)
confidence = calibrate(raw_score)
```

Either factor collapsing takes the whole score with it. On top of that sit hard
reject rules, which run *after* scoring and are where the precision actually
comes from:

1. Company brand tokens absent from the result title/snippet → reject.
2. Name similarity below 0.82 → reject.
3. Top two candidates within 0.08 of each other → blank, ambiguous.
4. URL is not a personal profile (`/company/`, `/pub/dir/`, `/posts/`, …) → drop.
5. The "person" name consists only of company tokens → it is a company account.
6. One profile URL claimed by two *different people* → retract both.

### Name normalisation

Registry names are legal long-forms; LinkedIn names are not. The normaliser
handles initials expansion and contraction (`Melukote Shivaramu Lokesh` ↔
`Lokesh M S`), dropped village/patronymic prefixes, given-name-last ordering,
ALL-CAPS (2.8% of rows), and romanisation variants.

Token similarity is **not** plain Jaro-Winkler. JW's prefix bonus rates
`debasheesh`/`debarshi` at 0.87 — two different real people who appeared in one
live result set. Blending JW with a length-sensitive indel similarity drops that
to 0.77. A consonant-skeleton check then separates genuine romanisation variants
(`murthy`/`moorthi` — vowels differ) from different names (`rajesh`/`ramesh` —
consonants differ). Finally, a weakest-link cap prevents an identical common
surname (Kumar, Reddy, Singh) from masking a mismatched given name.

## Contact routes

A sales team does not need a LinkedIn URL. It needs a way to reach the person.
Requiring the first to deliver the second was this pipeline's own ceiling: in a
measured run **37.5% of companies** had a discoverable presence while only
**12.5% of people** converted.

So the company is searched once, its published contact routes are cached, and
every executive there inherits them. With 306,916 people across **139,548
companies** the cost is amortised 2.2 ways on average — and far more at the large
employers, which are exactly the rows a wealth-management team most wants.

Routes are emitted most-direct-first:

```
direct_corporate_email -> executive_office -> investor_relations -> board_office
-> assistant -> reception -> linkedin_profile -> linkedin_company
-> official_social -> contact_form -> conference_page
```

Every route carries the URL that published it, so any entry can be checked in one
click.

### What is deliberately *not* collected

The scope is published professional contact routes. The pipeline will not emit:

| Refused | Why |
|---|---|
| Personal email addresses | A free-provider address whose local part is the person's own name is a private mailbox |
| Mobile numbers | An Indian 10-digit 6/7/8/9-series number is a personal handset more often than a switchboard, and a snippet cannot tell which. Refused as a class |
| Bare 10-digit numbers, `+91` + 10 digits | Indistinguishable from a mobile. A switchboard is written with its area code split off — `080-4123 4567` — and that formatting is the only reliable tell |
| Anything constructed | There is no address-pattern inference. Every route is a substring of text a source actually published |
| Contact-scraper sites | RocketReach, ZoomInfo and friends restate LinkedIn and are frequently stale |

Two further rules keep a route from being about the wrong person:

* **A LinkedIn profile URL in a result set is never a route.** It is a candidate
  identity, not an established one. The runner adds a profile route only for a
  match that cleared the confidence and corroboration gates. (Caught in a live
  pilot: route collection was emitting every `/in/` URL it saw, so one row picked
  up 26 "contact routes" that were other people's profiles.)
* **A person-scoped route requires the person to be named on the page** — the
  same test corroboration uses. A conference programme listing a colleague is not
  a way to reach this director.

Company-scoped routes reach the *office*, and are correct as leads even after the
individual moves on.

## The query ladder

Naive matching costs 2–3 queries per person: 600k–900k queries for this file. The
ladder cuts that by ~65% (measured, see `dry-run` output below):

```
Tier 1   one roster query per COMPANY        139,548 queries, shared by everyone there
Tier 1b  negative cache — a company with no LinkedIn footprint disqualifies
         all of its people, at zero further cost
Tier 2   per-person source ladder, up to 20 rungs, exhausted before "no match"
Tier 3   company contact discovery — up to 3 queries per COMPANY, cached and
         shared by every executive there
```

Tier 1b is a logical consequence of reject rule 1, not a heuristic: if a match
*requires* company evidence and the company has no discoverable presence, no
person there can qualify. It stops the *person* ladder only — Tier 3 still runs,
because a company with no LinkedIn page may still publish an IR address.

The footprint flag is **tri-state**. An inconclusive roster lookup records
"unknown", not "no presence"; unknown runs the ladder anyway. Only a conclusive
absence short-circuits.

### The person ladder, in order

```
linkedin_scoped   linkedin_named   open_web   location
leadership_page   board_page   executive   annual_report   investor_relations
press_release   conference_speaker
economic_times   business_standard   moneycontrol   bloomberg   crunchbase
mca_din   exchanges
contact   socials
```

Ordering is the only budget control: the runner works down the list and stops the
moment a profile is confirmed, so the expensive tail is paid only for rows that
would otherwise have been abandoned — which is exactly when it is worth paying.
Each rung is named, and `variant_hits` in the QC report shows which ones earn
their cost.

Measured on the real file:

```
Total rows read              :      312,160
Searchable rows              :      306,916
Skipped before searching     :        5,244
Unique companies             :      139,548   (2.20 people per company)
Tier 1 (one per company)     :      139,548  exact
Tier 2 @ 25% footprint       :       76,729  -> total 216,277
Naive baseline (2/person)    :      613,832
Saving at 25% footprint      :        64.8%
```

### What the deep ladder costs at full scale

The deep ladder is not free, and the numbers deserve to be stated rather than
discovered halfway through a run. Working the **whole file** at 37.5% footprint:

| Contact queries/company | Ladder depth | Total queries | At 1 q/s |
|---|---|---|---|
| 0 | 8 | 1,060,296 | 12.3 days |
| 0 | 20 | 2,441,418 | 28.3 days |
| 3 | 8 | 1,478,940 | 17.1 days |
| 3 | 20 | 2,860,062 | 33.1 days |

**This is why you use `--target`, not the whole file.** A run that stops at 3,000
deliverables works only as many rows as it needs:

| Conversion | Rows worked | Queries (depth 8, 3 contact) | At 1 q/s |
|---|---|---|---|
| 20% | 15,000 | ~72,000 | 20 hours |
| 5% | 60,000 | ~288,000 | 3.3 days |
| 2% | 150,000 | ~722,000 | 8.4 days |

The conversion rate is not knowable without searching. `run --pilot 1000` measures
it, then projects; see [Pilot first](#pilot-first).

## Installation

```bash
git clone <repo> && cd scraping-3.12-lakh-people
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                     # 119 tests, no network required
```

Python 3.11+.

### Search providers

The default is **`public_search`**: a composite over key-free public endpoints.
No API key, no account, no self-hosting — a fresh checkout can attempt a real
search immediately. It tries each backend in order until one returns results:

| Backend | Endpoint |
|---|---|
| `ddg_html` | `html.duckduckgo.com/html/` |
| `ddg_lite` | `lite.duckduckgo.com/lite/` |
| `mojeek` | `www.mojeek.com/search` |
| `searxng` | your instance, if `SEARXNG_URL` is set |

Run `python main.py preflight` to see which are reachable from your machine.

All providers:

| Provider | Cost | Notes |
|---|---|---|
| `public_search` | free | **Default.** Multi-backend, key-free |
| `searxng` | free | A single self-hosted instance |
| `duckduckgo` | free | Via the `ddgs` package (`pip install ddgs`) |
| `google_cse` | 100/day free, then $5/1k | Most ToS-clean. 10k/day ceiling |
| `serper` | ~$1/1k | Cheapest paid option, no daily cap |
| `serpapi` | ~$15/1k | Most robust, highest cost |
| `cassette` | free | Offline replay for tests |

Switching is one flag: `--provider serper`, plus the matching key in the
environment. Verify pricing before a large paid run — SERP vendors change terms
often.

#### Optional: your own SearXNG

```bash
docker run -d -p 8080:8080 searxng/searxng
export SEARXNG_URL=http://localhost:8080
```

Enable the JSON API in the instance's `settings.yml`, or every query fails with a
config error telling you exactly this:

```yaml
search:
  formats: [html, json]
```

> **Run it from a residential or low-reputation IP.** SearXNG works by querying
> real search engines, and those engines block the large cloud ranges (AWS, GCP,
> Azure) fastest. An instance on a hyperscaler IP can start returning empty
> results within a few dozen queries.

### Why "no results" is never reported as "no profile"

Three outcomes, not two:

| Outcome | Meaning |
|---|---|
| **SUCCESS** | Results returned and parsed |
| **FAILED** | The backend could not be reached, or refused |
| **INCONCLUSIVE** | HTTP 200, but nothing parseable came back |

INCONCLUSIVE is the dangerous one. It usually means a throttle, a challenge page,
or a markup change — *not* that the person has no profile. Two rules follow:

- The **negative cache refuses to record an absence** it cannot verify. Only a
  backend that returns an explicit empty result set (a JSON API) may establish
  that a company has no LinkedIn presence. An HTML-scraped empty page defers the
  row for retry instead, because one bad response would otherwise permanently
  blank every person at that company.
- A `403` from a **firewall** is distinguished from a `403` from the **search
  engine** — by deny header and body text. They need opposite responses, and
  conflating them sends you rotating user-agents against a network allowlist.

## Configuration

Precedence, lowest to highest:

```
dataclass defaults  ->  config/defaults.yaml  ->  environment variables  ->  CLI flags
```

Every tunable lives in `linkedin_enrichment/config/settings.py`. Environment
variables use the `LE_` prefix with `__` for nesting:

```bash
export LE_WORKERS=8
export LE_CONFIDENCE_THRESHOLD=0.95
export LE_PROVIDER__NAME=serper
export LE_RATE_LIMIT__REQUESTS_PER_SECOND=2.0
```

Secrets use their conventional names: `SEARXNG_URL`, `SERPER_API_KEY`,
`GOOGLE_API_KEY`, `GOOGLE_CSE_ID`, `SERPAPI_API_KEY`, `ANTHROPIC_API_KEY`.

Key settings:

| Setting | Default | Meaning |
|---|---|---|
| `confidence_threshold` | 0.95 | Below this, the LinkedIn column stays blank |
| `review_queue_floor` | 0.55 | Below the threshold but above this → review file |
| `workers` | 4 | Concurrent workers. Keep low for free providers |
| `max_row_attempts` | 3 | Passes a row may retry after a *transient* failure |
| `retry.max_attempts` | 4 | HTTP retries *within* a single search — not the same thing |
| `rate_limit.requests_per_second` | 0.5 | Starting rate; adapts down on throttling |
| `reject.min_name_similarity` | 0.82 | Name evidence gate |
| `reject.min_company_token_coverage` | 0.60 | Company evidence gate |
| `reject.min_margin` | 0.08 | Ambiguity gate |
| `enable_negative_cache` | true | Skip everyone at footprint-less companies |
| `llm.enabled` | false | Optional adjudication of borderline candidates |

## Yield mode — usable HNI prospects

The default for prospect generation. Optimises for leads a sales team can act on
rather than for a statistical proof.

```bash
python main.py preflight
python main.py run --input data/*.csv --yield --pilot 1000 --no-rank   # measure
python main.py run --input data/*.csv --yield --target 3000 --no-rank  # then run
```

| | |
|---|---|
| Threshold | **0.95** with corroboration (not 0.99) — for the profile claim only |
| Pool | **the whole file** — every executive shape, including plain "Director" |
| Company size | **never a rejection**; corroboration settles it instead |
| Corroboration | **one strong public source is enough** |
| Query ladder | **up to 20 formulations per person**, all tried before "no match" |
| Delivery | a confirmed profile **or** ≥1 published contact route |
| Stop | at `--target` deliverables, or genuine database exhaustion |

### Pilot first

```bash
python main.py run --input data/*.csv --yield --pilot 1000 --no-rank
```

A pilot works N rows and ends in a **measurement**, not a deliverable — no
prospect file is written, because presenting a 1,000-row sample's output as the
product is the confusion the pilot exists to prevent. It reports profiles
confirmed, rows with ≥1 route, routes per row, the route-type mix, rows/sec,
queries/row, and the projection to the full file.

It also refuses to flatter itself:

* if >20% of rows failed at the search layer it says so, and says the pilot is
  measuring the backend rather than the data;
* a **ranked** pilot is labelled an upper bound, because it worked the most
  promising rows first;
* an unranked pilot (`--no-rank`) is labelled a fair sample;
* a projected shortfall against `--target` is stated as a shortfall.

`--no-rank` is usually right at this scale. Ranking earns its cost when a run
stops early at a target; when the whole file is going to be worked anyway it is
an extra pass for nothing — and it makes the pilot unrepresentative.

### Why the pool is the whole file

Restricting to "senior" titles discarded **271,775 rows — 87% of the file** —
because that is how many are recorded as plain `Director`. For HNI prospecting a
registry director of a real operating company is a legitimate lead. Role now
orders the queue; it never excludes.

Accepted titles: Founder, Co-Founder, CEO, President, Chairman, Vice Chairman,
Managing Director, Joint MD, Executive Director, Whole Time Director, Director,
Board Member, Partner, Senior Partner, Managing Partner, Principal, Owner,
Proprietor, Promoter, CXO titles, and Business/Country/Regional/Division/
Practice/Global Head.

### Corroboration — one strong source

Accepted: LinkedIn, company leadership and staff pages, press releases,
conference speaker pages, news, Bloomberg, Crunchbase, PitchBook, Forbes,
Economic Times, Business Standard, Financial Express, Moneycontrol, NSE/BSE and
MCA filings, startup sites — and any unclassified domain that names both the
person and the company.

Only **contact scrapers** (RocketReach, ZoomInfo, Lusha) remain excluded: they
restate LinkedIn and add no information.

Registry filings are accepted but tagged `registry_filing` in the output, since
the input was itself MCA-derived — they confirm the directorship rather than the
identity. The source class is on every row so the sales team can see which kind
of evidence backs each lead.

### The search ladder no longer gives up early

One failed search is not evidence of absence. Each person is tried against
LinkedIn-scoped, plain-LinkedIn, bare name+company, leadership, executive,
director, press-release, Crunchbase and Bloomberg formulations, stopping as soon
as a match is confirmed. Evidence accumulates across variants, so a company hit
from query 2 still counts when query 5 finds the profile.

### If the target is not reached

The final CSV is gated on the target. Fall short and you get
`partial_matches.csv` plus a diagnosis naming the exact bottleneck:

```
  TARGET NOT MET — 1 accepted of 1,000 requested

  WHERE EVERY ROW WENT
    No candidate passed the name+company gates              7   87.5%
    Accepted                                                1   12.5%

  BOTTLENECK
    No candidate passed the name+company gates — 7 rows (87.5% of processed)

  COVERAGE
    Companies searched          :         8
    ...with a LinkedIn presence :         3  (37.5%)

  WHY 1,000 WAS NOT REACHED
    ... Bottom line: 1 of 8 processed rows converted (12.50%).
    Reaching 1,000 at this rate needs 8,000 processed rows.
```

It distinguishes the cases that matter: rows never processed (resume), search
failures (**infrastructure, not data** — the run has not fairly tested the file),
no-candidate (the input's ceiling), low-confidence (convertible, see the review
queue), and ambiguity (homonyms).

## Precision mode — ~1,000 profiles at ≥99%

When the goal is a short, defensible list rather than coverage:

```bash
python main.py preflight
python main.py run --input data/*.csv --precision --target 1000
python main.py validate            # hand-label, to measure the precision
```

`--precision` turns on, together and inseparably:

| | |
|---|---|
| Threshold | 0.99 instead of 0.95 |
| Pool | preferred roles only (Founder/CEO/Chairman/Promoter/MD/ED/Owner/Partner) |
| Order | highest offline priority first |
| Corroboration | **mandatory** — an independent non-LinkedIn source naming both person and company |
| Stop | once `--target` matches are found |

The deliverable is `out/top_matches.csv`, ranked by confidence, each row carrying
its corroborating source URL.

### Why corroboration becomes mandatory at 99%

An exact name plus an exact company scores **0.9644**. That clears 95% and
**fails 99%**. So at this bar every match needs a second, independent source —
which is exactly the "official websites, leadership pages, press coverage"
preference, promoted from nice-to-have to requirement.

"Independent" excludes ZaubaCorp, Tofler, TheCompanyCheck and similar: they
republish the same MCA registry the input came from, so agreeing with them is
the data agreeing with itself. Contact scrapers (RocketReach, ZoomInfo) are
excluded too — they restate LinkedIn, so they cannot corroborate LinkedIn.

### Expected yield, measured

Live stratified samples, every claimed match adversarially re-checked:

| Stratum | Pool | Hit rate | 95% CI |
|---|---|---|---|
| Preferred role @ ≥20 directors | 928 | 2/6 = 33% | 9.7% – 70.0% |
| Strict exec @ ≥10 directors | 375 | 0/6 = 0% | 0% – 39.0% |
| **Combined** | 1,303 | **2/12 = 16.7%** | **4.7% – 44.8%** |

Against the 32,951-row preferred pool that projects to **1,600–5,600** matches,
so a 1,000 target is reachable. Cost ≈ 51,000 queries: ~28 h free, ~1.4 h paid.

Note the strict-exec pool alone is only 2,600 rows — and this file contains **no
CTO/CFO/COO/CMO rows at all** — so reaching 1,000 from it would need a 38.5% hit
rate. That is why the pool includes Managing Directors, with strict execs ranked
first.

### What ranking can and cannot do

Ranking decides what is *tried* first. It cannot improve precision — the reject
rules do that. Three things were learned building it, each the hard way:

- **Junk names outrank real ones on rarity alone.** "Rockstar Productions" and
  "Apache Mewr" are as unique as "Thallapragada". Fixed by comparing a token's
  frequency in *person* names against its frequency in *company* names.
- **Nobody founds Apple India.** Founder/CEO titles at large captive subsidiaries
  are registry artifacts and are penalised.
- **A "meaningless" title is not a meaningless row.** Penalising titles shared by
  many people at one company — Wells Fargo's 299 executive directors, DSK Legal's
  38 partners — demoted *both* live-verified matches to the 96th percentile. The
  rule was removed and is kept as a documented no-op so it is not reintroduced.

### Proving the 99% — and feeding it back

The pipeline reports a *calibrated model score*. That is not a measured
precision, and the QC report refuses to present it as one until you validate:

```
  PRECISION
    Confidence threshold :         0.99
    Calibration in force : shipped prior (logistic, not fitted)
    Measured precision   :          none — NOT YET VALIDATED

    The confidence above is a calibrated MODEL SCORE, not a measured
    precision. Do not quote it as one. Run `python main.py validate`:
    381 rows labelled with zero errors give a 95% lower bound of 0.9900.
```

| Labelled | Errors | Wilson 95% lower bound |
|---|---|---|
| 381 | 0 | **0.9900** |
| 381 | 1 | 0.9860 |
| 100 | 0 | 0.9630 |

**381 rows, all correct, is the cheapest honest route to a ≥99% claim.**

The labelling is a closed loop — the effort improves the model, it does not just
produce a number:

```bash
python main.py run --precision      # matches, each with its raw score
python main.py validate             # one keystroke per row, saved as you go
python main.py calibrate --write    # fits from those labels, saves the curve
python main.py report               # now quotes a MEASURED precision
```

`calibrate` reads the labels straight from the database and writes
`out/calibration.yaml`, which later runs load automatically — nothing is pasted
by hand. The report then names the calibration in force, so a run can never
quietly score against a different curve than the reader assumes.

**The refitted threshold is reported, never auto-applied.** A fit may conclude
that ≥99% needs a raw-score cut of 0.83 rather than 0.759. Adopting that changes
how many rows qualify, so it is your explicit decision, not a silent one.

If the measured lower bound comes in under your threshold, the report says so
directly rather than letting the threshold look proven.

At a true 99%, a 1,000-row deliverable still contains ~10 wrong profiles.

## Usage

**Always start with preflight and a 10-row test.** A run whose searches silently
return nothing still completes "successfully", writing a blank and a reason for
every row — output that looks like a finding when it is actually an
infrastructure fault. The first two commands exist to make that impossible.

```bash
# 1. Can this machine actually search? Says precisely why if not.
python main.py preflight
#    exit 0 = go, 1 = all backends failed, 2 = reachable but returned nothing

# 2. Prove it on 10 rows, showing every query and every accept/reject decision.
python main.py run --input data/*.csv --limit 10 --explain

# 3. Only then, the full run.
python main.py run --input data/*.csv
```

`run` calls preflight itself and refuses to start if it fails; `--force`
overrides that if you are certain.

```bash
# See what the run will cost before spending anything — no network calls
python main.py dry-run  --input data/*.csv
python main.py estimate --input data/*.csv

# Re-running the same command resumes; it never redoes finished work.
python main.py run --input data/*.csv

# Continue after an interruption
python main.py resume

# Regenerate outputs / report from the database without re-searching
python main.py write
python main.py report

# Data-subject deletion
python main.py suppress --row-uid <uid> --note "deletion request"
```

### Output

For each input file, `out/<name>_enriched.csv` — the original columns
**byte-for-byte unchanged**, with the four specified columns appended first:

| Column | Contents |
|---|---|
| `LinkedIn Profile` | The URL, **only** when confidence ≥ threshold. Otherwise empty |
| `Confidence Score` | 4-decimal score for matched rows; empty otherwise |
| `Verification Notes` | Why it matched, or why it did not |
| `Source URL(s)` | Search results the decision was based on |

then the contact-route columns, so a consumer reading only the specified four is
unaffected by their existence:

| Column | Contents |
|---|---|
| `Contact Routes` | One route per line, most direct first: `type: value (source)` |
| `Best Contact Type` | The most direct route type on the row |
| `Contact Source URL(s)` | Deduped sources for the routes |

Plus:

* `out/prospects.csv` — **the deliverable a sales team works.** Every row with a
  way in, profiles first, then rows carried by their contact routes.
* `out/top_matches.csv` — confirmed identities only. Written as
  `partial_matches.csv` instead when the target was not met, so a short list is
  never presented under the deliverable's name.
* `out/review_queue.csv` — near-misses for human review.
* a timestamped QC report in both JSON and text.

`Confidence Score` is deliberately *empty* rather than `0.0` on unmatched rows —
a numeric zero invites a downstream reader to think the row was scored and
rejected when it may simply never have been searched.

## Architecture

```
linkedin_enrichment/
  config/      settings.py, defaults.yaml — one place for every tunable
  ingest/      reader.py    streaming CSV (BOM-aware) + all-worksheet XLSX
               normalize.py name/company/location normalisation
               prefilter.py Tier 0 — drop unresolvable rows before any query
  providers/   base.py           SearchProvider ABC -> SerpResult
               http_providers.py searxng, duckduckgo, google_cse, serper, serpapi
               cassette.py       offline replay — the whole suite runs with no network
  search/      query_builder.py  the person ladder + the company contact ladder
               ratelimit.py      adaptive token bucket
               retry.py          exponential backoff with full jitter
               client.py         cache -> limit -> retry -> provider
  identity/    scorer.py         features, scoring, the final decision
               reject.py         the hard gates
               corroborate.py    the independent second source
               company_contact.py company contact discovery, cached per company
               calibrate.py      isotonic fit of raw score -> probability
               llm_adjudicator.py optional, off by default
  cache/       company_cache.py  rosters + the negative cache
               serp_cache.py     raw responses, so re-scoring is free
  database/    schema.py, store.py — SQLite WAL, claim/resume protocol
  workers/     pool.py    async pool, bounded queue, graceful drain
               runner.py  per-row execution: company stage, ladder, routes, decide
  logging/     setup.py   JSON to file, text to console
               dashboard.py live rich panel
  output/      contacts.py   the route model, hierarchy and scope guardrails
               writer.py, top_matches.py, review_queue.py, report.py
               bottleneck.py why a target was missed; pilot.py measure + project
  tests/       426 tests, all offline
main.py        CLI
```

### Data model

SQLite in WAL mode, chosen because the defining requirement is exact crash-safe
resume across a run that may span days.

| Table | Purpose |
|---|---|
| `records` | One row per input row; the input side is immutable |
| `results` | The decision per row |
| `review_candidates` | Sub-threshold candidates — never promoted to a match |
| `company_cache` | Brand, roster, and the footprint flag (the negative cache) |
| `company_contacts` | Published contact routes per company — discovered once, reused |
| `contact_routes` | Per-row routes, unique on `(row, type, value)` so retries cannot duplicate |
| `serp_cache` | Raw provider responses, so re-scoring never re-pays for search |
| `suppressions` | Tombstones for deletion requests |
| `run_stats` | Per-run counters for the QC report |

## Resume and interruption

Resume is automatic and exact. There is no separate checkpoint file.

* A row leaves `pending` only inside a transaction (`claim_batch`).
* It reaches `done` only in the same transaction that writes its result.
* On startup, any row `claimed` for longer than `stale_claim_seconds` (default
  900) returns to `pending` — that is a worker that died.
* `Ctrl-C` stops intake, drains in-flight rows, releases unstarted claims and
  exits 0.

So a process killed at any instant leaves every row either finished or
reclaimable. Re-running `run` (or `resume`) continues exactly where it stopped.
Ingestion is idempotent, so passing `--input` again is harmless.

### Transient failures retry themselves

A *terminal* decision means "we searched and this is the answer". A *transient*
failure means "we did not get an answer" — a search error, or a company lookup
that came back unparseable. Only the first is ever written as a result.

```
pending → claimed → [transient failure] → deferred        (attempts += 1)
                                             │
                        next run: attempts < max_row_attempts → pending
                                  attempts ≥ max_row_attempts → done, with a
                                                                note saying so
```

Rows are parked as `deferred` rather than returned straight to `pending`, so a
broken backend cannot spin them in a hot loop; the next pass picks them up. The
budget (`max_row_attempts`, default 3) guarantees the loop terminates, and a row
that exhausts it is finalised with an accurate note — never a promise of a retry
that will not come.

```bash
python main.py retry            # requeue deferred rows and continue
python main.py retry --reset    # also requeue rows that exhausted their attempts
python main.py retry --no-run   # requeue only
```

Use `--reset` after fixing the underlying cause (an egress allowlist, a stopped
SearXNG): those rows spent their attempts on a problem that no longer exists.
Rows that already succeeded are untouched, and cached SERP responses mean
re-resolving them costs nothing.

### Reading the trustworthiness block

Every QC report opens with it, because a blank only means "we searched and found
nothing convincing" if the searches actually worked:

```
  RUN TRUSTWORTHINESS
    Inconclusive searches:      184,220  (85.2% of 216,277 issued)
    Rows queued for retry:      179,441

    WARNING: 85.2% of searches returned no parseable results.
    The blank rows in this run are NOT evidence that these people have
    no LinkedIn profile — they reflect the state of the search backend.
```

It warns above 20% inconclusive, or if any rows remain deferred. A clean run says
so explicitly instead. **If this block warns, do not quote the match rate to
anyone** — run `preflight`, fix what it reports, then `retry`.

## Performance tuning

### Measured at full scale

A complete 312,160-row pass was executed offline against the recorded cassette
corpus (no network), to validate throughput, the ladder and output integrity:

| Measure | Result |
|---|---|
| Ingest | 52 s, 386 MB peak RSS, 252 MB database |
| Rows in / results out | 312,160 / 312,160 — exact reconciliation |
| Queries issued | 139,499 (139,489 Tier 1 + 10 Tier 2) vs 613,832 naive |
| Negative-cache skips | 306,809 rows resolved at **zero** query cost |
| Company cache hit rate | 54.5% |
| Search errors / throttles | 0 / 0 |
| Original columns modified | **0** across all 312,160 rows |
| Blank rows lacking an explanation | **0** |

Crash safety was verified on the same data: a `SIGKILL` mid-run left
94 done + 7 claimed + 306,815 pending + 5,244 skipped = 312,160 with zero
duplicate results, and the resume reclaimed the stale claims and completed
cleanly.

Only one row matched, which is correct — the offline corpus contains just seven
recorded searches, so only three companies had any footprint to find. The number
that matters here is that **306,915 rows were correctly resolved to a blank with
a stated reason rather than guessed**.

### Tuning

The bottleneck is always the search provider, never this code.

| Symptom | Action |
|---|---|
| Frequent throttling | Lower `rate_limit.requests_per_second`; the limiter also halves itself automatically on each throttle |
| Free provider returning empties | Your instance is likely blocked upstream. Move it off a cloud IP |
| Want more speed on a paid API | Raise `workers` (8–16) and `requests_per_second` |
| Re-tuning thresholds | Just re-run — `serp_cache` replays the corpus at no cost |
| Disk pressure | The DB holds cached SERP JSON; ~250 MB at ingest, growing with cache |

Batches are claimed ordered by `company_key`, so people at the same company tend
to be processed together and share a roster fetch.

## Calibration

The shipped threshold uses a logistic mapping from raw score to probability. It
is a documented prior, not a measurement. To make "95%" mean 95% on *your* data:

1. Lower `review_queue_floor` (e.g. 0.30) and run a sample.
2. Hand-label ~400 candidates 1/0 into a CSV with `raw_score,correct` columns.
   400 gives roughly ±5% at 95% confidence; 1,000 tightens it to ~3%.
3. `python main.py calibrate --labels labelled.csv`
4. Paste the emitted knots into `calibration.isotonic_points`.

The cut point is chosen using the **lower bound** of the Wilson interval, not
the point estimate — 10/10 correct is not evidence of 95% precision.

## Compliance

* **linkedin.com is never fetched.** Matching uses search-result metadata only.
  This avoids LinkedIn's terms on automated access entirely.
* Only public profile URLs are stored, with provenance: source URLs, provider and
  retrieval timestamp on every row (DPDP Act 2023 traceability).
* `main.py suppress` erases a person's enrichment and tombstones the row so a
  later re-run cannot resurrect it.
* No credentialed access, no block circumvention, no CAPTCHA solving.
* Conservative default rate limits.
* **Contact routes stay on the professional side of the line.** Only routes an
  organisation has published: official company addresses, executive-office and
  IR desks, board offices, leadership and contact pages, official profiles, and
  conference/speaker pages. No personal email addresses, no mobile numbers, no
  pattern-guessed individual addresses, nothing behind authentication — see
  [what is deliberately not collected](#what-is-deliberately-not-collected).
  Every route carries its source URL so the claim can be checked and, if a data
  subject objects, traced.

## Troubleshooting

**Start here: `python main.py preflight`.** It classifies the failure and prints
the remedy, which is faster than reading this table.

| Preflight classification | What it means | Fix |
|---|---|---|
| `proxy_policy` | A firewall refused the host; the search engine was never reached. **Not** anti-bot | Add the host to your network egress allowlist, or run from a network that permits search engines. No provider or user-agent change helps |
| `connection_refused` | Nothing is listening at that address | Usually SearXNG not running: `docker run -d -p 8080:8080 searxng/searxng` |
| `anti_bot` | The engine served a challenge/consent page | Try another backend, lower the rate, or use a paid API |
| `rate_limited` | Throttled | Lower `rate_limit.requests_per_second`; the limiter also backs off on its own |
| `empty_results` | HTTP 200, nothing parsed | Markup changed or the page was empty. **Not** proof that no profiles exist — do not run in bulk |
| `config` | e.g. SearXNG serving HTML because `json` is missing from `search.formats` | Fix the setting named in the message |
| `dns` / `tls` / `timeout` | Transport-level | Check resolver, CA bundle, or raise the timeout |

| Message | Cause and fix |
|---|---|
| `SearXNG selected but no URL set` | `export SEARXNG_URL=http://localhost:8080` |
| Refusing to start: searches are not working | Preflight failed. Fix the cause above, or `--force` if you are certain |
| Every row is `blank_no_candidate` | Run `preflight`. If it passes, this is genuine — see [expected yield](#what-to-realistically-expect) |
| Many rows `blank_error` with "inconclusive" | The backend is unreliable. Those rows are deferred, not cached as absent, and retry on the next pass — see [transient failures](#transient-failures-retry-themselves) |
| QC report warns about trustworthiness | Searches were not working. Do not quote the match rate; run `preflight`, fix, then `retry` |
| `duckduckgo: 202 Ratelimit` | Expected under load. The limiter backs off automatically; lower the configured rate if it persists |
| `scoring weights must sum to 1.0` | Custom weights in your config do not total 1.0 |
| Run seems stuck | Check `logs/enrichment.jsonl`. A very low adapted rate means the provider is throttling |
| Match rate looks too low | Probably correct — see below. Check `qc_report_*.txt` for the blank breakdown |
| Want to re-score without re-searching | `python main.py run` again; cached SERP responses are replayed free |

## What to realistically expect

A ≥95%-confidence match needs the company to be identifiable on LinkedIn **and**
the person to be there under a recognisable form of their registry name. Both
conditions are rare in this file. Measured over all 312,160 rows:

| Fact | Figure |
|---|---|
| Rows at companies with ≤ 2 directors | 187,995 (60.2%) |
| Rows at companies with ≤ 4 directors | **283,441 (90.8%)** |
| Rows at companies with ≥ 5 directors | 28,719 (9.2%) |
| Rows whose name is shared with another row *in this file* | 138,705 (44.4%) |

So for roughly nine rows in ten the company is a small family entity with
essentially no web footprint, and for nearly half the file the name alone is
ambiguous within the dataset itself.

**A realistic auto-accept yield for the LinkedIn Profile column is therefore 2–6%
of rows (~6,000–19,000 matches), with a point estimate near 3%.** A further 5–15%
should land in the review queue. These numbers are reasoned estimates, not
measurements — no live search has been run against this dataset.

**Contact routes are the reason the deliverable is not capped at that number.**
The ceiling for a profile is set by whether *the person* is findable; the ceiling
for a route is set by whether *the company* is. In the one measured sample those
were 12.5% and 37.5% respectively. A row at a company that publishes a leadership
page and an IR address is a usable lead even when the individual is invisible —
which is most of this file. That is why `--target 3000` is reachable when
`3,000 confirmed profiles` would not have been.

The honest form of that claim: the profile rate is estimated at 2–6%, the route
rate is unmeasured at scale, and `run --pilot 1000` exists to replace both with
numbers. Until a pilot runs against a working search backend, every figure in
this section is a projection and is labelled as one.

> **If your first live run reports 15–25% matched, treat that as an alarm, not a
> win.** At this data quality it almost certainly means a reject rule has been
> weakened or disabled. The deliverable worth trusting is a *measured precision
> figure on a hand-labelled sample* (see [Calibration](#calibration)), not a row
> count.

Any tool claiming 60–80% on this input is either matching on name alone — which
the `Debasheesh Bagchi` case shows produces confidently wrong answers — or on
company alone, which the `Synthesis Winding` case shows does the same. The review
queue is where the remaining recoverable value sits, at the cost of human
attention rather than false CRM records.

Wall-clock for a full run at the default polite 0.5 req/s is roughly **5 days**
for ~216k queries. That is why checkpoint/resume, the negative cache and the SERP
cache are load-bearing parts of this design rather than conveniences. Raise the
rate if your provider tolerates it; the limiter will back itself off if not.
