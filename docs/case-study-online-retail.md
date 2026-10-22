# Case study: Online Retail II (real data, Run 4 Phase 28)

A full engine run on a real, public invoice-line dataset: the UCI "Online
Retail II" export of a UK online wholesaler — 1,067,371 lines, 2009-12-01 to
2011-12-09, GBP, with real cancellations, anonymous sales, non-product codes
and duplicate rows. Source, license (CC BY 4.0) and citation:
[data-sources.md](data-sources.md). Reproduce with:

```
uv run python scripts/download_online_retail.py
uv run margin-engine run --data <DATA_DIR> --mapping config/mappings/online_retail_ii.yaml
```

A 5,000-line sample is committed under `data/sample_online_retail/` (with
attribution) and exercised by `tests/test_online_retail_sample.py`, so every
behavior described here is pinned offline in CI.

## What this dataset does NOT have

No cost, no list price, no order numbers, no discount/rebate/freight columns,
no instruments. The run is therefore a test of honest degradation:

| Missing input | Engine behavior |
|---|---|
| Unit cost | `cost_unavailable: true` declared in the mapping (the engine refuses a mapping that merely omits cost); margin, floors, contribution and expected-change opportunities all report `cost_unavailable`, never a number |
| List price | Price index falls back to the item median, labeled `index_fallback_item_median`; the gross-list waterfall stage is labeled incomplete; no floor-index comparisons |
| Order id | The invoice is used as the order unit — a stated proxy in the run notes |
| Discounts / rebates / concessions | Observed leakage is $0 by construction (no visibility), not "no leakage" |
| Instruments | Elasticity status is capped at `descriptive_elasticity`; no IV, no causal claims, no volume-response opportunity sizing |

## Mapping

[`config/mappings/online_retail_ii.yaml`](../config/mappings/online_retail_ii.yaml),
drafted from `margin-engine profile` (which detected the delimiter, encoding,
ISO dates, money column and candidate canonical fields on the real file) and
confirmed field by field with reasoning in comments. Two pieces of configured
business logic, both in the mapping rather than code:

- **Transaction type by prefix rule** (spec 5.2): invoices prefixed `C` are
  cancellations → classified as returns. 19,494 lines.
- **Constant currency**: the export is entirely GBP; conversion to the USD
  reporting currency uses monthly month-end ECB reference rates fetched by the
  Frankfurter connector (offline via the committed fixture).

## Reconciliation and headline totals

Every reconciliation check passes exactly, in both currencies:

| Measure | Value |
|---|---|
| Net invoiced revenue (USD, converted) | $30,397,137 |
| Net invoiced revenue (GBP, source, kept per line) | £19,287,251 |
| Sale lines / value | 1,047,877 / $32.80M |
| Return (cancellation) lines / value | 19,494 / −$2.40M |

## Data issues found and how each was classified

| Issue | Count | Treatment |
|---|---|---|
| Cancellations (`C` prefix) | 19,494 lines | Classified returns via the mapping rule; excluded from behavior views, kept in the ledger |
| Anonymous sales (no Customer ID) | 242,257 lines, 14.7% of sale revenue | Kept in the ledger and all totals; excluded from customer-level views (observations, segmentation, panel) with the share stated |
| Negative quantity without `C` prefix | 3,457 lines | Flagged `negative_qty_sale`, never treated as ordinary sales |
| Non-positive net on sale lines | 25,718 lines | Flagged; excluded from unit-price math |
| True duplicate rows | 67,250 flags | The export has no stable line key (the conversion adds a deterministic row id), so identical rows are LOOK-ALIKE duplicates: flagged, never removed (spec 5.3) |
| Data-quality price anomalies | 11,071 | Robust-z flags within peer groups; records preserved |
| Commercial pricing outliers | 65,082 | Separate flag, never mixed with data-quality flags |
| Non-product codes | POST (2,122 lines, $177k), DOT ($510k), M "manual" (−$133k), D "discount" (−$21k), BANK CHARGES (−$55k), ADJUST, C2 | Remain in the ledger as mapped lines; malformed ones carry the flags above. Lesson: a production mapping should classify these via a stock-code map into fees/adjustments — the engine's honest default is to keep and flag, not to guess |

## Stages that ran and stages that switched off

Ran: ingest, classification, two views, waterfall (stages present), exact
reconciliation, FX conversion, dispersion and outliers, ABC, commercial
segmentation (4 segments on region/order-size context), reference bands,
guidance (bands only, no floors), panel, descriptive elasticity, level-2
opportunity sizing, order/customer economics (invoice-as-order proxy),
real-data validation, HTML report. Runtime 152 s end to end, peak 4.2 GB.

Switched off, each with a stated reason in the run notes: floors and all
cost-dependent metrics, leakage decomposition, inventory costing, tiered
rebates, kits, quotes/win-loss, cross-price, books tie-out, IV estimation.

## Findings

- **Dispersion is real and large**: within peer groups, 110,994 pair-months
  sit below the P25 reference and 108,185 above P75 (262,738 in band).
- **Level-2 gross opportunity** (below-reference pairs raised to segment P50
  at unchanged volume, pocket basis): **$698,667** across 76,255 records, all
  on the labeled `index_fallback_item_median` price basis; the conservative
  below-P25-to-P25 variant is $566,992. No expected-contribution level exists
  (no cost, no causal elasticity) — the engine refuses to invent one.
- **Elasticity: descriptive at most, as predicted.** FE-OLS point estimates
  (−0.6 to −3.1 by segment) disagree with PPML (−1.3 to −5.8) — exactly the
  robustness spread that keeps the status down.

## Real-data validation (no planted truth)

This section is the engine arguing *against* its own estimates — on this
dataset the checks correctly refuse to support causal claims:

- **Temporal backtest** (train on months 1..T−6, predict held-out volume
  changes): the fitted descriptive elasticity does NOT beat the
  zero-elasticity baseline (improvement −0.9% to −10.0% by segment), and the
  out-of-sample slope is much smaller than the in-sample estimate (e.g. −0.82
  vs −1.93 in the largest segment). Descriptive estimates here over-fit
  within-sample price-volume comovement.
- **Fake price-change placebo**: 80–100% of random fake change dates show a
  "significant" volume response (+0.04 to +0.10 log points). The panel
  violates the no-effect null even without any price change — pairs trend
  after entry — so an event-style causal reading of any price change in this
  data would be spurious. (No instruments exist, so the shuffled-instrument
  placebo does not apply.)
- **Rolling stability**: 12-month-window FE-OLS elasticities move a lot
  (segment 4: −2.9 → −2.1 → −1.3), and segmentation partitions agree only
  moderately across windows (ARI 0.31–0.49). The business changed across
  2010–2011; a single pooled elasticity is not a stable object here.

Together these justify the engine's own gating: nothing in this run reaches
`iv_estimated_elasticity`, let alone causal support, and the report says why.

## Lessons for real deployments

1. **The profiler works on real exports** — delimiter, dates, money and key
   candidates were detected; the human work is confirming semantics, which is
   exactly where the `# CONFIRM` protocol forces attention.
2. **Declared absence beats silent defaults.** `cost_unavailable: true` and
   the prefix-derived transaction type are one-line mapping declarations that
   would otherwise be silent code assumptions.
3. **Identity gaps dominate**: 14.7% of revenue has no customer and ~6k lines
   are fee/adjustment codes. Both are ledger-relevant but behavior-irrelevant,
   and the engine's two-view design (5.4) is what keeps them from corrupting
   estimates while still reconciling every dollar.
4. **Validation without planted truth earns its keep**: on synthetic worlds
   those checks pass; here they fail loudly and the evidence status stays
   low. That asymmetry is the point.
