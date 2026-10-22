"""Deterministic synthetic names (phase 23.8).

Names ride their own RNG stream (seed + 1001) so enabling them never perturbs
the existing world: every other column is byte-identical for a given seed.
Customer names read like distributor accounts, item descriptions carry a size
and material, supplier and category names read like line cards.
"""

from __future__ import annotations

import numpy as np
import polars as pl

_REGIONS = [
    "Gulf Coast",
    "Tri-State",
    "Lakeshore",
    "Piedmont",
    "High Plains",
    "Bayou",
    "Cascade",
    "Front Range",
    "Low Country",
    "Blue Ridge",
    "North Shore",
    "Permian",
    "Heartland",
    "Riverbend",
    "Granite State",
    "Sun Valley",
]
_TRADES = [
    "Mechanical",
    "Plumbing",
    "Electric",
    "Industrial",
    "HVAC",
    "Pipe & Steel",
    "Builders",
    "Facility",
    "Process",
    "Fluid Power",
    "Refrigeration",
    "Waterworks",
    "Controls",
    "Fastener",
    "Safety",
]
_SUFFIXES = [
    "Supply",
    "Supply Co.",
    "Distributors",
    "Services",
    "Contractors",
    "Systems",
    "Solutions",
    "& Sons",
    "Group",
    "Works",
    "Partners",
    "Inc.",
]

_SIZES = [
    "1/2 in.",
    "3/4 in.",
    "1 in.",
    "1-1/4 in.",
    "2 in.",
    "3 in.",
    "4 in.",
    "6 in.",
    "10 mm",
    "25 mm",
]
_MATERIALS = [
    "Brass",
    "Copper",
    "PVC",
    "CPVC",
    "Stainless",
    "Carbon Steel",
    "Cast Iron",
    "Galvanized",
    "Bronze",
    "Nylon",
    "Aluminum",
    "PEX",
]
_PRODUCTS = [
    "Ball Valve",
    "Gate Valve",
    "Check Valve",
    "Union Coupling",
    "Elbow 90",
    "Tee Fitting",
    "Nipple",
    "Pipe Hanger",
    "Flange Kit",
    "Strainer",
    "Pressure Regulator",
    "Hose Assembly",
    "Gasket Set",
    "Anchor Bolt",
    "Conduit Body",
    "Junction Box",
    "Relay Switch",
    "Thermostat",
    "Filter Cartridge",
    "Bearing Unit",
]
_SUPPLIER_A = [
    "Keystone",
    "Vulcan",
    "Meridian",
    "Allied",
    "Summit",
    "Pinnacle",
    "Atlas",
    "Sterling",
    "Cardinal",
    "Falcon",
    "Granite",
    "Harbor",
]
_SUPPLIER_B = [
    "Manufacturing",
    "Industries",
    "Metals",
    "Components",
    "Products",
    "Fabrication",
    "Tooling",
    "Brands",
]
_CATEGORY_NAMES = [
    "Valves & Actuation",
    "Pipe & Fittings",
    "Electrical Rough-In",
    "Fasteners & Anchors",
    "HVAC Components",
    "Pumps & Fluid Handling",
    "Safety & PPE",
    "Tools & Abrasives",
    "Filtration",
    "Controls & Sensors",
    "Insulation & Sealants",
    "Bearings & Power Transmission",
]


def _pick(rng: np.random.Generator, options: list[str], n: int) -> list[str]:
    return [options[int(i)] for i in rng.integers(0, len(options), n)]


def customer_names(seed: int, customer_ids: list[str]) -> pl.DataFrame:
    """Distributor-style account names, deterministic per seed, unique per id
    (duplicates get the account number's last digits as a branch marker)."""
    rng = np.random.default_rng(seed + 1001)
    n = len(customer_ids)
    names = [
        f"{a} {b} {c}"
        for a, b, c in zip(
            _pick(rng, _REGIONS, n), _pick(rng, _TRADES, n), _pick(rng, _SUFFIXES, n), strict=True
        )
    ]
    seen: dict[str, int] = {}
    out = []
    for cid, name in zip(customer_ids, names, strict=True):
        if name in seen:
            seen[name] += 1
            name = f"{name} Branch {cid[-2:]}"
        else:
            seen[name] = 1
        out.append(name)
    return pl.DataFrame({"customer_id": customer_ids, "customer_name": out})


def item_descriptions(seed: int, item_ids: list[str]) -> pl.DataFrame:
    """Item descriptions with size and material, deterministic per seed."""
    rng = np.random.default_rng(seed + 1002)
    n = len(item_ids)
    descs = [
        f"{s} {m} {p}"
        for s, m, p in zip(
            _pick(rng, _SIZES, n), _pick(rng, _MATERIALS, n), _pick(rng, _PRODUCTS, n), strict=True
        )
    ]
    seen: dict[str, int] = {}
    out = []
    for _iid, d in zip(item_ids, descs, strict=True):
        if d in seen:
            seen[d] += 1
            d = f"{d}, Style {seen[d]}"
        else:
            seen[d] = 1
        out.append(d)
    return pl.DataFrame({"item_id": item_ids, "item_description": out})


def supplier_names(seed: int, supplier_ids: list[str]) -> dict[str, str]:
    rng = np.random.default_rng(seed + 1003)
    out: dict[str, str] = {}
    used: set[str] = set()
    for sid in sorted(set(supplier_ids)):
        name = f"{_pick(rng, _SUPPLIER_A, 1)[0]} {_pick(rng, _SUPPLIER_B, 1)[0]}"
        if name in used:
            name = f"{name} {sid[-2:]}"
        used.add(name)
        out[sid] = name
    return out


def category_names(category_ids: list[str]) -> dict[str, str]:
    """Stable category line-card names: index-mapped, no RNG needed."""
    cats = sorted(set(category_ids))
    return {
        cid: _CATEGORY_NAMES[i % len(_CATEGORY_NAMES)]
        + ("" if i < len(_CATEGORY_NAMES) else f" {i // len(_CATEGORY_NAMES) + 1}")
        for i, cid in enumerate(cats)
    }
