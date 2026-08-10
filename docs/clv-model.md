# Prospect value model

What a row in this file is worth, and the ranking that follows from it.

Every rupee figure here is an **input you replace**. The model is specified; the
economics are GCIA's. Figures marked *measured* are counted over the real file;
figures marked *assumed* are placeholders.

---

## Before the numbers

Two constraints shape everything below.

**Nothing has been scraped.** 39 searches have been issued and all 39 went to the
offline test cassette. Every yield figure here is a projection.

**The file contains no value data.** `Revenue`, `Employees` and `Website` are 0%
filled — not sparse, empty. A lifetime-value model therefore cannot be *fitted*
from this data. It has to be specified, then calibrated once outcomes exist.

Measured figures are carried forward from the full-file run documented in the
README. The source CSVs are not in the repo, so nothing here was recomputed at
authoring time.

---

## The existing ranking answers a different question

`identity/priority.py` scores every row 0–1000 and the pipeline works that order
top-down. It ranks **resolvability**, not worth.

| | Existing priority score | Value score |
|---|---|---|
| Asks | Will a search find this person? | Is this person worth finding? |
| Dominant term | Name rarity (weight 0.45) | Board count & company substance |
| Optimises | Queries spent per match | Revenue per conversation |
| Ranks highest | Rare-named director of a two-person firm | Common-named director on six boards |

The disagreement is not a defect. A rare surname genuinely collapses the
candidate set — that is what both live-verified matches had in common. But
`Praveen Kumar`, one of 54 in this file, may be the more valuable prospect and
will sit near the bottom of the queue.

These multiply rather than compete. Ranking by `priority × value` gives expected
value per query; the existing score is unchanged and becomes one factor of two.

---

## The model

**Lifetime value of a converted client** — all three terms are yours:

```
CLV = annual_premium × commission_rate × retention_years
```

**Probability a row converts:**

```
P(route) × P(reach | route) × P(meeting | reach) × P(client | meeting)
```

Only `P(route)` touches the pipeline. Everything downstream is the sales process.

> `P(route) = 37.5%` is the thinnest number here — a sample of eight companies
> taken during development. Right order of magnitude, nothing more. Treat it as
> 20–55% until `run --pilot 1000` replaces it.

---

## Funnel, with placeholder rates

| Stage | Rate | Provenance | Count |
|---|---|---|---|
| Eligible rows | — | measured | 304,962 |
| Contact route found | × 0.375 | measured, n≈8 | 114,361 |
| Actually reached | × 0.30 | assumed | 34,308 |
| Meeting held | × 0.10 | assumed | 3,431 |
| Became a client | × 0.15 | assumed | 515 |

At a placeholder CLV of ₹1,35,000 (₹1.5L premium × 15% × 6 years) those 515
clients are worth roughly ₹6.95 crore.

**The binding stage is meetings, not queries.** 3,431 meetings at four a day,
220 days a year, is about 3.9 rep-years of capacity. That is the number worth
arguing about.

---

## What the search costs

Google Programmable Search: 100/day free, then $5 per 1,000. ₹ at $1 ≈ ₹88.

| Scope | Queries | Paid cost | Free-tier time |
|---|---|---|---|
| Pilot — 170 companies covering 3,000 executives | 1,800 | $9 · ₹792 | 18 days, unattended |
| Company tier only — one roster query each | 139,548 | $698 · ₹61,400 | impractical |
| Full run — complete ladder, all rows | 216,000 | $1,080 · ₹95,040 | impractical |

**One converted client pays for scraping the entire file, with ₹40,000 left
over.** Break-even on the full run is 0.70 clients. Even if every downstream
assumption is off by 10× — 51 clients rather than 515 — the run returns ₹69 lakh
against ₹95,040 of search spend.

Search cost does not belong in the decision. Which changes what prioritisation is
*for*: not to save queries, but to make sure the ~880 conversations a rep can
have in a year are the most valuable 880 in the file.

---

## Value signals the file can support

| Signal | Status | Usable as a value proxy? |
|---|---|---|
| **Boards held per person** | measured | Best available. Already computed in `Corpus.person_boards`. A serial director is a materially different prospect. |
| **Directors per company** | measured | Weak but real. 28,719 rows (9.2%) at companies with ≥5 directors — the only size signal present. |
| **Industry** | measured | 95.9% filled, 21 coarse buckets. Product fit, not sizing. |
| **Designation** | measured | Barely. 87% is literally `Director`, and the file lists people as "owner" of IBM India. |
| **Location** | measured | No discriminating power — every row is Bangalore or Hyderabad. |
| Revenue | absent | Column entirely empty. |
| Employee count | absent | Column entirely empty. |
| Paid-up capital, incorporation date | absent | Not exported — but recoverable, see below. |

---

## Proposed value tiers

Built only from signals that exist today. Tier sizes need one pass over the file;
a `rank --value` command can emit them.

| Tier | Definition | Rows | Reasoning |
|---|---|---|---|
| **A** | ≥3 boards *and* company ≥5 directors | unknown | Serial director at a substantial operation. |
| **B** | Company ≥5 directors, single board | ≤28,719 | Real operating company; the only firmographic signal points here. |
| **C** | ≥3 boards, small companies | unknown | Serial small-business owner. Personal wealth often exceeds any single entity. |
| **D** | ≤4 directors, single board | ~276,000 | 90.8% of the file. Mostly statutory-minimum private limiteds. Work last. |

Do not let Tier D's size tempt you into working it first. 60.2% of the file sits
at companies with two directors or fewer, which in Indian company law is the
legal minimum — it carries no information about the business.

---

## The highest-leverage fix

Every row carries a **PrivateCircle URL**, 100% filled. That page is where the
revenue and employee figures the export dropped actually live. One fetch per
company — 139,548, cached exactly like the roster query — would replace the
guessing above with real firmographics, converting a specified model into a
fitted one.

Two things to confirm first: whether the PrivateCircle subscription permits
programmatic access, and whether the export can simply be re-run with those
columns included — faster and cheaper than fetching anything.

Failing that, MCA publishes paid-up capital and incorporation date against a CIN.
If the CIN exists in the source workbook — it is not in the exported columns —
that is a second route to the same signal.

---

## Open inputs

1. **The three CLV inputs** — average annual premium, commission rate, expected
   retention in years, split by segment if they differ materially.
2. **Funnel rates, or permission to measure them** — reach, meeting and close
   rates from existing outbound. The pilot can produce the first two.
3. **Sales capacity** — reps and conversations per week. Sets how deep into the
   ranking is worth scraping, and is the real constraint.
4. **A Google API key and Search Engine ID** — still the blocker on every
   measured number. The free tier covers the 1,800-query pilot.
5. **A decision on PrivateCircle** — re-export with the missing columns, or
   authorise fetching them.
