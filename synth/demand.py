"""Structural demand and pricing model.

Produces one row per customer x item x month where a purchase happens, plus the
full availability panel (the planted truth for which zeros are real zeros).

Structure (logs unless noted):
    price index  p_cit = log(seg_base * pair_eff)
                         + rho * (z_cost[s_i,t] + z_tariff[g_i,t])   # instruments
                         + gamma * u_ct                              # endogeneity
                         + delta * (beta_seg - mean beta)            # rep targets price-sensitive customers
                         + eps_p                                     # noise
    demand       log mu = a_ci + beta_seg * log(P/P_ref_i) + u_ct
                         + leak * z_tariff[g_i,t]                    # 0 unless invalid_instrument
                         + season_t
    quantity     q ~ max(1, Poisson(mu)) on purchase incidence; 0 otherwise.

Contract-priced pairs get a fixed contract index: no endogenous discounting and
no instrument pass-through (price-protected).

The endogeneity term makes price move WITH the demand shock (reps discount when
demand is weak), so naive OLS on realized prices is biased toward zero. This
direction is recorded in ground truth and asserted by validation tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from synth.config import GeneratorConfig
from synth.entities import SegmentDef
from synth.money import div_round_half_even

REP_SENSITIVITY_DELTA = 0.05  # extra log discount per unit of (beta - mean beta)
SEASON_AMPLITUDE = 0.10


@dataclass
class DemandWorld:
    """Everything the line builder needs, all vectorized per pair-month."""

    pairs: pl.DataFrame  # one row per customer-item pair with structural params
    panel: pl.DataFrame  # pair x month: availability, purchase, qty, price cents
    z_cost: np.ndarray  # (n_suppliers, months)
    z_tariff: np.ndarray  # (n_categories, months)
    demand_shock: np.ndarray  # (n_customers, months)


def _ar1(rng: np.random.Generator, n_series: int, months: int, sd: float, ar: float) -> np.ndarray:
    out = np.zeros((n_series, months))
    if sd == 0:
        return out
    innov = rng.normal(0, sd, (n_series, months))
    out[:, 0] = innov[:, 0]
    for t in range(1, months):
        out[:, t] = ar * out[:, t - 1] + innov[:, t]
    # Instruments are SHOCKS: demean each series so persistent AR(1) paths do
    # not plant permanent supplier/category price-level differences that would
    # contaminate the segment structure. Within-series time variation (what
    # identifies elasticities through pair fixed effects) is untouched.
    return out - out.mean(axis=1, keepdims=True)


def build_pairs(
    cfg: GeneratorConfig,
    customers: pl.DataFrame,
    items: pl.DataFrame,
    contracts: pl.DataFrame,
    segments: list[SegmentDef],
    rng: np.random.Generator,
    substitution_partner: dict[str, str] | None = None,
) -> pl.DataFrame:
    """Assortment: which items each customer can buy, with pair-level parameters."""
    d = cfg.demand
    n_items = items.height
    cust_rows = customers.to_dicts()
    item_role = items["item_role"].to_numpy()
    long_tail = item_role == "long_tail"

    cust_idx: list[np.ndarray] = []
    item_idx: list[np.ndarray] = []
    # KVI behavior (Phase 15): key-value items are bought by many customers.
    # Extra inclusions ride a separate RNG stream so non-KVI draws (and any
    # world generated before this phase) keep their main-stream draws.
    kvi_rng = np.random.default_rng(cfg.seed + 222)
    kvi_items = np.flatnonzero(item_role == "kvi")
    # Substitution partners are usually co-carried (customers stock competing
    # brands); separate stream so other worlds keep their draws.
    sub_rng = np.random.default_rng(cfg.seed + 112)
    item_ids_list = items["item_id"].to_list()
    item_pos_of = {iid: k for k, iid in enumerate(item_ids_list)}
    for ci in range(len(cust_rows)):
        k = int(rng.integers(cfg.assortment_min, cfg.assortment_max + 1))
        k = min(k, n_items)
        picks = rng.choice(n_items, k, replace=False)
        extra = kvi_items[kvi_rng.random(len(kvi_items)) < d.kvi_carriage_prob]
        picks = np.unique(np.concatenate([picks, extra]))
        if substitution_partner:
            partners = [
                item_pos_of[substitution_partner[item_ids_list[p]]]
                for p in picks
                if item_ids_list[p] in substitution_partner and sub_rng.random() < 0.8
            ]
            if partners:
                picks = np.unique(np.concatenate([picks, np.array(partners)]))
        cust_idx.append(np.full(len(picks), ci))
        item_idx.append(picks)
    c = np.concatenate(cust_idx)
    i = np.concatenate(item_idx)

    cust_seg = customers["segment_id"].to_numpy()[c]
    # Long-tail items override the customer segment (planted rule, segment 5).
    seg = np.where(long_tail[i], 5, cust_seg)
    beta = np.array([segments[s].elasticity for s in seg])
    base_ix = np.array([segments[s].base_price_index for s in seg])

    # Contract coverage: customer x item_family exact match.
    fam = items["item_family"].to_numpy()[i]
    cust_id_arr = np.array([r["customer_id"] for r in cust_rows])[c]
    pair_df = pl.DataFrame(
        {
            "customer_id": cust_id_arr,
            "item_pos": i,
            "cust_pos": c,
            "item_id": items["item_id"].to_numpy()[i],
            "item_family": fam,
            "segment_id": seg,
            "elasticity": beta,
            "base_index": base_ix,
        }
    )
    if contracts.height > 0:
        pair_df = pair_df.join(
            contracts.select("customer_id", "item_family", "contract_id", "discount_pct"),
            on=["customer_id", "item_family"],
            how="left",
        )
    else:
        pair_df = pair_df.with_columns(
            pl.lit(None, dtype=pl.String).alias("contract_id"),
            pl.lit(None, dtype=pl.Float64).alias("discount_pct"),
        )

    n_pairs = pair_df.height
    mean_beta = float(np.mean([s.elasticity for s in segments]))
    pair_eff = rng.normal(0, d.pair_price_sd, n_pairs)
    # KVIs have well-known street prices: much tighter pair-level dispersion
    # (this is what makes them comparable, the third candidate-score signal).
    pair_eff = np.where(item_role[i] == "kvi", pair_eff * 0.3, pair_eff)
    base_q = rng.normal(d.base_qty_log_mean, d.base_qty_log_sd, n_pairs)
    # Long-tail pairs buy much less often; KVI pairs much more often.
    buy_prob = np.where(long_tail[i], d.purchase_prob * 0.25, d.purchase_prob)
    buy_prob = np.where(item_role[i] == "kvi", np.minimum(0.92, buy_prob * 1.6), buy_prob)
    # Active window: 80% active from month 0 to end; rest enter late or exit early.
    start_m = np.zeros(n_pairs, dtype=np.int64)
    end_m = np.full(n_pairs, cfg.months - 1, dtype=np.int64)
    late = rng.random(n_pairs) < 0.10
    early = (rng.random(n_pairs) < 0.10) & ~late
    start_m[late] = rng.integers(3, max(4, cfg.months // 2), late.sum())
    end_m[early] = rng.integers(cfg.months // 2, cfg.months - 3, early.sum())

    return pair_df.with_columns(
        pl.Series("pair_eff", pair_eff),
        pl.Series("log_base_qty", base_q),
        pl.Series("buy_prob", buy_prob),
        pl.Series("start_month", start_m),
        pl.Series("end_month", end_m),
        pl.Series("mean_beta", np.full(n_pairs, mean_beta)),
    )


def simulate_panel(
    cfg: GeneratorConfig,
    pairs: pl.DataFrame,
    items: pl.DataFrame,
    customers: pl.DataFrame,
    list_history: pl.DataFrame,
    rng: np.random.Generator,
    substitution: tuple[pl.DataFrame, dict[str, float]] | None = None,
) -> DemandWorld:
    d = cfg.demand
    months = cfg.months
    n_pairs = pairs.height

    # --- shocks ---------------------------------------------------------
    z_cost = _ar1(rng, cfg.n_suppliers, months, d.cost_shock_sd, d.instrument_ar)
    z_tariff = _ar1(rng, cfg.n_categories, months, d.tariff_shock_sd, d.instrument_ar)
    u = rng.normal(0, d.demand_shock_sd, (cfg.n_customers, months))

    # Stockouts: item x month.
    stockout = rng.random((items.height, months)) < cfg.mess.stockout_rate

    # --- expand pairs x months -----------------------------------------
    p_item = pairs["item_pos"].to_numpy()
    p_cust = pairs["cust_pos"].to_numpy()
    sup = np.array([int(s[3:]) for s in items["supplier_id"].to_list()])[p_item]
    cat = np.array([int(s[3:]) for s in items["item_category"].to_list()])[p_item]

    n_obs = n_pairs * months
    pair_rep = np.repeat(np.arange(n_pairs), months)
    t_tile = np.tile(np.arange(months), n_pairs)

    it = p_item[pair_rep]
    cu = p_cust[pair_rep]
    su = sup[pair_rep]
    ca = cat[pair_rep]

    zc = z_cost[su, t_tile]
    zt = z_tariff[ca, t_tile]
    ut = u[cu, t_tile]

    beta = pairs["elasticity"].to_numpy()[pair_rep]
    base_ix = pairs["base_index"].to_numpy()[pair_rep]
    pair_eff = pairs["pair_eff"].to_numpy()[pair_rep]
    mean_beta = pairs["mean_beta"].to_numpy()[pair_rep]
    contract_disc = pairs["discount_pct"].to_numpy()[pair_rep]
    on_contract = ~np.isnan(contract_disc.astype(np.float64))
    log_bq = pairs["log_base_qty"].to_numpy()[pair_rep]
    buy_p = pairs["buy_prob"].to_numpy()[pair_rep]
    start_m = pairs["start_month"].to_numpy()[pair_rep]
    end_m = pairs["end_month"].to_numpy()[pair_rep]

    # --- availability (planted truth for real zeros) --------------------
    active = (t_tile >= start_m) & (t_tile <= end_m)
    available = active & ~stockout[it, t_tile]

    # --- effective list price per item-month ----------------------------
    # list_history rows are (item_id, effective_date, list_cents); build an
    # item x month matrix of the list in force.
    item_ids = items["item_id"].to_list()
    item_pos_map = {iid: k for k, iid in enumerate(item_ids)}
    list_mat = np.zeros((items.height, months), dtype=np.int64)
    hist = list_history.sort("item_id", "effective_date")
    start_year, start_month = cfg.start_year, cfg.start_month
    for row in hist.iter_rows(named=True):
        eff = row["effective_date"]
        m0 = (eff.year - start_year) * 12 + (eff.month - start_month)
        m0 = max(0, m0)
        if m0 < months:
            list_mat[item_pos_map[row["item_id"]], m0:] = row["list_cents"]
    list_cents = list_mat[it, t_tile]

    # --- price index -----------------------------------------------------
    eps_p = rng.normal(0, d.price_noise_sd, n_obs) if d.price_noise_sd > 0 else np.zeros(n_obs)
    log_idx = (
        np.log(base_ix * np.exp(pair_eff))
        + d.price_passthrough * (zc + zt)
        + d.endogeneity_gamma * ut
        + REP_SENSITIVITY_DELTA * (beta - mean_beta)
        + eps_p
    )
    # Contract pairs: fixed index, protected from shocks and discounting.
    contract_idx = np.log(np.where(on_contract, 1 - np.nan_to_num(contract_disc), 1.0))
    log_idx = np.where(on_contract, contract_idx, log_idx)
    price_index = np.exp(log_idx)
    unit_price_cents = div_round_half_even(
        np.round(list_cents * price_index * 1_0000).astype(np.int64), 1_0000
    )
    unit_price_cents = np.maximum(unit_price_cents, 1)

    # --- demand ----------------------------------------------------------
    base_list = items["base_list_cents"].to_numpy()[it]  # P_ref: initial list
    log_p_rel = np.log(np.maximum(unit_price_cents, 1) / base_list)
    season = SEASON_AMPLITUDE * np.sin(2 * np.pi * (t_tile % 12) / 12)
    log_mu = log_bq + beta * log_p_rel + ut + d.tariff_demand_leak * zt + season

    # Cross-price effects (Phase 17): for substitution-group partners, demand
    # for A rises with B's relative price when the same customer carries both.
    if substitution is not None and substitution[0].height:
        groups_df, gammas = substitution
        item_ids_all = items["item_id"].to_list()
        obs = pl.DataFrame(
            {
                "row": np.arange(n_obs),
                "cust": cu,
                "item_id": [item_ids_all[k] for k in it],
                "t": t_tile,
                "logp_rel": log_p_rel,
            }
        )
        g2 = groups_df.join(groups_df, on="group_id", suffix="_partner").filter(
            pl.col("item_id") != pl.col("item_id_partner")
        )
        partner_prices = obs.select(
            pl.col("cust"),
            pl.col("item_id").alias("item_id_partner"),
            pl.col("t"),
            pl.col("logp_rel").alias("partner_logp_rel"),
        )
        cross = (
            obs.join(g2, on="item_id", how="inner")
            .join(partner_prices, on=["cust", "item_id_partner", "t"], how="inner")
            .with_columns(
                pl.col("group_id")
                .replace_strict(gammas, default=0.0, return_dtype=pl.Float64)
                .alias("gamma")
            )
            .group_by("row")
            .agg((pl.col("gamma") * pl.col("partner_logp_rel")).sum().alias("adj"))
        )
        adj = np.zeros(n_obs)
        adj[cross["row"].to_numpy()] = cross["adj"].to_numpy()
        log_mu = log_mu + adj
    mu = np.exp(np.clip(log_mu, -10, 8))
    bought = available & (rng.random(n_obs) < buy_p)
    qty = np.zeros(n_obs, dtype=np.int64)
    if d.qty_process == "poisson":
        qty[bought] = np.maximum(1, rng.poisson(mu[bought]))
    else:
        eps_q = rng.normal(0, d.qty_noise_sd, int(bought.sum()))
        qty[bought] = np.maximum(1, np.round(np.exp(np.log(mu[bought]) + eps_q))).astype(np.int64)

    # --- unit cost per pair-month (moves with supplier cost shock) ------
    base_cost = items["base_cost_cents"].to_numpy()[it]
    cost_factor = np.exp(0.8 * zc)
    unit_cost_cents = div_round_half_even(
        np.round(base_cost * cost_factor * 1_0000).astype(np.int64), 1_0000
    )
    unit_cost_cents = np.where(base_cost == 0, 0, np.maximum(unit_cost_cents, 1))

    panel = pl.DataFrame(
        {
            "pair_pos": pair_rep,
            "month": t_tile,
            "customer_id": pairs["customer_id"].to_numpy()[pair_rep],
            "item_id": pairs["item_id"].to_numpy()[pair_rep],
            "item_pos": it,
            "cust_pos": cu,
            "segment_id": pairs["segment_id"].to_numpy()[pair_rep],
            "active": active,
            "available": available,
            "bought": bought,
            "qty": qty,
            "unit_price_cents": unit_price_cents,
            "unit_cost_cents": unit_cost_cents,
            "list_cents": list_cents,
            "on_contract": on_contract,
            "contract_id": pairs["contract_id"].gather(pair_rep),
            "demand_shock": ut,
            "z_cost": zc,
            "z_tariff": zt,
        }
    )
    return DemandWorld(pairs=pairs, panel=panel, z_cost=z_cost, z_tariff=z_tariff, demand_shock=u)
