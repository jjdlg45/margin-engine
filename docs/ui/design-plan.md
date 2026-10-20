# Margin Engine demo UI — design plan

## Subject, audience, job

**Subject.** The results of an auditable pricing and margin engine run on
synthetic distributor data: a reconciled price waterfall, price dispersion,
segments, guidance with economic floors, elasticity evidence, opportunity
sizing and cost scenarios.

**Audience.** People arriving from LinkedIn or GitHub: engineers judging
rigor, pricing and finance people judging domain sense, recruiters judging
polish. Most will spend two to five minutes, on a phone as often as a desktop.

**Primary job.** In that time, convey one idea: *this engine accounts for
every dollar and says how sure it is about every number*. Every design
decision serves that — the waterfall is the hero because reconciliation is the
product's spine, and evidence status is a first-class visual channel, not a
footnote.

## Voice

A working financial instrument, not a marketing site. The references are a
well-set annual report and a flight instrument panel: generous whitespace,
hard alignment to a grid of numbers, restrained color used almost exclusively
to carry meaning (status, evidence, direction). Decoration is absent; what
reads as "design" is spacing, hierarchy and the quality of the charts.

## Palette

Six named colors, each with a job. Light theme values; dark theme remaps the
same tokens.

| Token | Hex (light) | Hex (dark) | Role |
|---|---|---|---|
| `ink` | `#1C2B33` | `#E8EDEF` | Text, chart axes, the waterfall's revenue bars |
| `paper` | `#F7F8F7` | `#15191C` | Page ground |
| `card` | `#FFFFFF` | `#1D2328` | Panels, table grounds |
| `petrol` | `#0B5563` | `#4FA3B5` | Brand, links, active nav, headline figures — continuity with the engine's HTML report |
| `verdigris` | `#2E7D5B` | `#5CB78F` | PASS, causal-supported, in-band |
| `amber` | `#9A6A00` | `#D7A43C` | Estimated/allocated, IV-level evidence, warnings |
| `garnet` | `#8E2F3C` | `#D06A77` | FAIL, below-floor, losses, negative deltas |

Neutral grays derive from `ink` at fixed opacities (87% body, 70% secondary,
38% disabled chrome only - never meaning-bearing text, 12% hairlines) rather
than a second gray ramp, which keeps both themes coherent from one pair of
anchors. Text on filled chips and active nav uses the `paper` token, not
white, so it stays correct in both themes. (Secondary was first set at 60%
and on-fill text at white; axe measured 3.95:1 and 2.9:1, both under WCAG AA,
so the tokens were corrected - recorded change.)

Deliberately rejected: cream + terracotta and near-black + neon (anti-pattern
list); a blue-purple SaaS gradient (reads as template); pure black and pure
white grounds (harsh for dense numerals).

## Typefaces and roles

| Face | Role |
|---|---|
| IBM Plex Sans (variable, self-hosted woff2) | Everything: UI, headings, body |
| IBM Plex Mono | Identifiers (customer/item ids), code-like rule strings — never small labels |

One family, two voices. Plex was built for "data and engineering" contexts,
has true tabular figures, and its mono sibling lets ids read as data without
the mixed-font noise the anti-pattern list warns about. **All numerals
everywhere render with `font-variant-numeric: tabular-nums`** so columns of
money align to the digit.

Type scale (rem): 2.25 display (headline KPI), 1.5 page title, 1.17 section,
1.0 body, 0.875 table/secondary, 0.78 caption/badge. Weights 400/500/600 only.

Spacing scale (px): 4, 8, 12, 16, 24, 32, 48, 64. Section rhythm is 48;
intra-card rhythm is 12/16. Radius: 6px on cards, 3px on badges — and tables,
charts and full-bleed sections get none, so not everything is a card
(anti-pattern: identical card grids).

## Layout concept

Desktop (1440): a 12-column grid, content max 1120px. The Overview is a
single column of full-width bands, not a card mosaic — each band is one
subject with its own internal grid.

```
+------------------------------------------------------------------+
| MARGIN ENGINE        overview  waterfall  customers  ...   [D/L] |
| Synthetic demo data - every figure reconciles to source          |
+------------------------------------------------------------------+
| Headline: "Every dollar accounted for."                          |
| $12.4M invoiced -> $10.9M pocket     RECONCILED to the cent  OK  |
|                                                                  |
|  ############ gross list                                         |
|     ######### invoice            <- the waterfall, hero visual   |
|        ###### pocket                                             |
|          #### pocket GP                                          |
|           ### contribution                                       |
+------------------------------------------------------------------+
| Key findings (3-5 rows, not cards)                               |
|  1  $412k leakage in 12m, 60% rebates....... -> Waterfall        |
|  2  Segment 4 prices 9% under band........... -> Guidance        |
+------------------------------------------------------------------+
| Coverage strip: lines / cost coverage / list coverage / warnings |
+------------------------------------------------------------------+
```

Mobile (375): the same bands stack; the waterfall rotates to horizontal bars
full-width; nav collapses to a scrollable top strip (no hamburger — seven
destinations fit a strip).

## Chart style rules

- Recharts with every default stripped: no gridline mesh (y-hairlines only at
  12% ink), no legend chrome (direct labels), no animation except the one
  deliberate Overview waterfall build.
- Waterfall: stage bars in `ink`, with the *drops* between stages (discounts,
  rebates, concessions) hatched in `garnet`-tinted fills and labeled with
  dollars and % of list. Connectors at 38% ink, 1px.
- Band/interval charts (guidance, elasticity CIs): the band is a tinted area,
  the point is a 2px `ink` tick; floors are a solid `garnet` rule with a
  label, never just a colored line.
- Dollars abbreviated on axes ($1.2M), exact in tooltips and tables.
  Negative money always signed with a leading −, also in `garnet` text.

## Evidence-status visual language

Never color alone; each status pairs a fill treatment with a text label and a
tooltip giving the reason it was assigned (from the bundle's reasons fields).

| Status | Treatment |
|---|---|
| `causal_elasticity_supported`, `estimated_win_curve` | Solid chip, `verdigris` fill, white text |
| `iv_estimated_elasticity` | Diagonal-hatch chip (SVG pattern), `amber` border and text |
| `descriptive_elasticity` | Outline-only chip, `ink` 60% |
| `insufficient_data`, `insufficient_precision` | Dashed-outline chip, 38% ink, reason in tooltip and inline where space allows |
| `allocated` / `estimated` amounts | Superscript small square marker after the figure + tooltip naming the method |

The same grammar colors chart elements: solid = causal, hatched = IV,
outline = descriptive, dashed = insufficient.

## Self-review against the brief and anti-patterns

- *Generic card grids*: first draft had a 2×2 KPI card row; replaced with one
  full-width headline band and an inline coverage strip. Recorded change.
- *All-caps eyebrows*: none; section titles are sentence-case headings.
- *Middle-dot meta strings*: coverage strip uses labeled pairs in a table-like
  band instead.
- *Monospace small labels*: mono is reserved for ids and rule strings at body
  size, never captions.
- *Fade-in everything*: motion budget is exactly one page-load moment (the
  Overview waterfall drawing once, 600ms, skipped under reduced-motion);
  everything else moves only in response to the user.
- *Numbered markers on non-sequences*: key findings are ranked by dollar
  impact, so numbering is meaningful and kept.
- Palette avoids both named anti-pattern schemes; checked.
- First draft used Space Grotesk headings over Inter body; dropped to a single
  Plex family — two display personalities read as template, and Plex's tabular
  figures are the better fit for a numbers product. Recorded change.


## Phase 19.1 revisions (owner review)

Every token change, with the reason:

| Token | Was (light / dark) | Now (light / dark) | Why |
|---|---|---|---|
| `paper` | `#F7F8F7` / `#15191C` | `#F7F6F3` / `#101418` | Warmer, less clinical off-white; dark page dropped darker so panels separate clearly |
| `card` | `#FFFFFF` / `#1D2328` | `#FFFFFF` / `#1A2026` | Dark card re-tuned against the darker paper for visible layering |
| `inset` (new) | - | `#F0EDE7` / `#232B33` | Third surface tone for strips and table heads - "2-3 layered surfaces" instead of one flat ground |
| `ink` | `#1C2B33` / `#E8EDEF` | `#22313A` / `#F4F7F8` | Light ink warmed slightly; dark ink brightened so primary text (87%) sits at or above 7:1 on card and paper, secondary (70%) above 4.5:1 |
| `petrol` | `#0B5563` / `#4FA3B5` | `#0B5563` / `#7BD0E2` | Dark variant was muddy as text and bars; brightened for chart and link legibility |
| `verdigris` | `#2E7D5B` / `#5CB78F` | `#2E7D5B` / `#7FDCAB` | Brighter dark status color so solid chips read at small sizes |
| `amber` | `#9A6A00` / `#D7A43C` | `#9A6A00` / `#F0BC55` | Same |
| `garnet` | `#8E2F3C` / `#D06A77` | `#8E2F3C` / `#F08D99` | Same; also drives the leakage hatch, which needed to read on dark |
| `--shadow` (new) | - | soft 2-layer shadow per theme | Gentle elevation on major panels only |

Structure and treatment changes:

- **Accent rules.** A 36x3px petrol rule under the page title and each section
  heading (`.heading-rule`), replacing full-width hairlines as the primary
  section marker. Deliberate, short, consistent across pages.
- **Panels.** `.panel` = 8px radius + hairline border + soft shadow, applied
  to major panels only (the waterfall). The coverage band uses the inset tone
  at 60% with the same radius but no border or shadow - layered, not a card
  grid. Tables, chips and findings stay square.
- **Cost vs leakage in the waterfall.** Product cost and cost-to-serve drops
  now use an ink dot pattern; the garnet hatch is reserved for pricing
  leakage (discounts, rebates, concessions). Costs are not leakage, and the
  two now cannot be read as the same thing. The chart caption names both.
- **Findings.** Numbered markers removed (the list reads as ranked without
  them); figure column bolded. "Orders destroy contribution" reworded to
  "orders lose money after cost-to-serve."
- **KPI type.** Headline figures at weight 700 with tight tracking against
  caption-size labels; every figure's label now carries its time window.
- **Mobile nav.** The scrollable strip (which clipped its last item) wraps
  instead - no cut-off destinations at 375px.
- **Spacing.** Section rhythm opened from 40/48 to 48/56px equivalents
  (py-12/14), gap between KPI blocks widened.


## Phase 19.2 revisions (waterfall readability, KPI basis)

Chart token changes, with measured contrast against the panel (`card`):

| Element | Was | Now | Contrast light / dark |
|---|---|---|---|
| Leakage hatch stroke | garnet / 0.6 | garnet / 0.8, width 1.6 | 5.2:1 / 4.9:1 |
| Leakage hatch wash | garnet / 0.16 | garnet / 0.2 | (wash, not meaning-bearing) |
| Cost dots | ink / 0.5 on ink / 0.07 | ink / 0.7 dots on ink / 0.12 wash, r 1.3 | 7.4:1 / 8.1:1 |
| Step connectors (new) | - | ink / 0.55, 1px, 3-3 dash | 5.1:1 / 6.3:1 |
| Ghost columns (new) | - | ink / 0.05 under floating bars | (grounding wash) |
| Stage bars | ink | ink (unchanged) | 13:1 / 12:1 |

All meaning-bearing marks clear the 3:1 requirement in both themes; washes
(hatch ground, ghost columns) are deliberately sub-3:1 because they carry no
meaning alone - the grammar stays solid = totals, hatch = leakage, dots =
costs. The chart height dropped from 300 to 264px with zero bottom margin,
removing the dead band under the axis. Dashed connectors are drawn from each
bar to the start of the next by a Customized layer reading the rendered bar
rects, so they stay exact under any scale and in both orientations.

KPI basis: all headline KPIs (net revenue, pocket revenue, pocket margin,
and the coverage band's gross profit and contribution) are now computed after
returns, so pocket revenue can never exceed net revenue; the waterfall is
labeled as sale lines with returns shown in Customers & orders.


## Phase 23.5 revisions (readability polish)

**Table standard** (defined once in the tokens, applied to every table -
opportunities, residuals, customers, guidance, segments, validation,
reconciliation):

- Cell padding 10px vertical / 16px horizontal; relaxed line height.
- A thin hairline divider between rows (no vertical cell grid); the header row
  separated by a 2px rule at 25% ink.
- Numbers right-aligned with tabular figures (global); text cells left-aligned
  via the `.left` marker; first column always left.
- Explanation ("why") cells render as their own block: prose max width,
  relaxed leading, extra vertical padding (`.why`).

**Label language.** A central code-to-label dictionary (engine-side
`labels.py`, embedded in the bundle, consumed by every page and by the HTML
report) replaces raw codes in all visible text: statuses, flags, residual
keys, reconciliation check names, volume treatments. Raw codes survive only in
tooltips (status chips show `[code]` in the tooltip) and "Technical
rule/key" expanders. Segment names and descriptions are generated from the
tree-rule structure at export time - never hand-written - with the raw rule in
an expander.
