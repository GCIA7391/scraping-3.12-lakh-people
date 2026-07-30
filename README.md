# LinkedIn Enrichment Pipeline

Attaches the most likely **public** LinkedIn profile URL to each person in a
prospect workbook, at a configurable confidence threshold (default 95%).

Built for a specific dataset: 312,160 company directors in Bangalore and
Hyderabad, exported from PrivateCircle's scrape of the Indian MCA/ROC registry.
The design decisions below follow from what that data actually contains.

**The core rule: never fabricate.** A row that cannot be resolved with the
required confidence is left blank and told why. Near-misses go to a separate
review file, never into the LinkedIn column.

---

## Table of contents

- [What the data supports](#what-the-data-supports)
- [How matching works](#how-matching-works)
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

## The query ladder

Naive matching costs 2–3 queries per person: 600k–900k queries for this file. The
ladder cuts that by ~65% (measured, see `dry-run` output below):

```
Tier 1   one roster query per COMPANY        139,548 queries, shared by everyone there
Tier 1b  negative cache — a company with no LinkedIn footprint disqualifies
         all of its people, at zero further cost
Tier 2   per-person query, only when the company HAS a footprint but the
         subject was absent from the roster
```

Tier 1b is a logical consequence of reject rule 1, not a heuristic: if a match
*requires* company evidence and the company has no discoverable presence, no
person there can qualify.

Measured on the real file:

```
Total rows read              :      312,160
Searchable rows              :      306,916
Skipped before searching     :        5,244
Unique companies             :      139,548
Tier 1 (one per company)     :      139,548  exact
Tier 2 @ 25% footprint       :       76,729  -> total 216,277
Naive baseline (2/person)    :      613,832
Saving at 25% footprint      :        64.8%
```

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
| `rate_limit.requests_per_second` | 0.5 | Starting rate; adapts down on throttling |
| `reject.min_name_similarity` | 0.82 | Name evidence gate |
| `reject.min_company_token_coverage` | 0.60 | Company evidence gate |
| `reject.min_margin` | 0.08 | Ambiguity gate |
| `enable_negative_cache` | true | Skip everyone at footprint-less companies |
| `llm.enabled` | false | Optional adjudication of borderline candidates |

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
**byte-for-byte unchanged**, with four appended:

| Column | Contents |
|---|---|
| `LinkedIn Profile` | The URL, **only** when confidence ≥ threshold. Otherwise empty |
| `Confidence Score` | 4-decimal score for matched rows; empty otherwise |
| `Verification Notes` | Why it matched, or why it did not |
| `Source URL(s)` | Search results the decision was based on |

Plus `out/review_queue.csv` (near-misses for human review) and a timestamped QC
report in both JSON and text.

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
  search/      query_builder.py  the Tier 1 / 1b / 2 ladder
               ratelimit.py      adaptive token bucket
               retry.py          exponential backoff with full jitter
               client.py         cache -> limit -> retry -> provider
  identity/    scorer.py         features, scoring, the final decision
               reject.py         the hard gates
               calibrate.py      isotonic fit of raw score -> probability
               llm_adjudicator.py optional, off by default
  cache/       company_cache.py  rosters + the negative cache
               serp_cache.py     raw responses, so re-scoring is free
  database/    schema.py, store.py — SQLite WAL, claim/resume protocol
  workers/     pool.py    async pool, bounded queue, graceful drain
               runner.py  per-row ladder execution
  logging/     setup.py   JSON to file, text to console
               dashboard.py live rich panel
  output/      writer.py, review_queue.py, report.py
  tests/       119 tests, all offline
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
| Many rows `blank_error` with "inconclusive" | The backend is unreliable. Those rows were deliberately *not* cached as absent and will retry on the next run |
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

**A realistic auto-accept yield is therefore 2–6% of rows (~6,000–19,000
matches), with a point estimate near 3%.** A further 5–15% should land in the
review queue. These numbers are reasoned estimates, not measurements — no live
search has been run against this dataset.

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
