"""Scenario-based digital twin for the Supply Chain Intelligence Hub.

The model turns the project's static SKU snapshot into a 90-day stochastic
inventory, supplier, quality, and logistics simulation. It is intentionally
transparent: every policy parameter is exposed through the CLI and the output
tables can be loaded directly into Power BI.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {
    "SKU", "Price", "Number of products sold", "Revenue generated",
    "Stock levels", "Lead times", "Order quantities", "Shipping times",
    "Shipping costs", "Supplier name", "Defect rates", "Costs",
}


@dataclass(frozen=True)
class Policy:
    name: str
    reorder_point_days: float = 7.0
    safety_stock_days: float = 2.0
    order_quantity_multiplier: float = 1.0
    supplier_lead_time_reduction: float = 0.0
    defect_rate_reduction: float = 0.0
    shipping_time_reduction: float = 0.0
    shipping_cost_change: float = 0.0
    demand_uplift: float = 0.0


POLICIES = {
    "baseline": Policy("baseline", reorder_point_days=5.0, safety_stock_days=1.0),
    "recommended": Policy(
        "recommended", reorder_point_days=8.0, safety_stock_days=3.0,
        order_quantity_multiplier=1.15, supplier_lead_time_reduction=0.12,
        defect_rate_reduction=0.15, shipping_time_reduction=0.10,
        shipping_cost_change=-0.05, demand_uplift=0.03,
    ),
    "resilience": Policy(
        "resilience", reorder_point_days=10.0, safety_stock_days=5.0,
        order_quantity_multiplier=1.25, supplier_lead_time_reduction=0.18,
        defect_rate_reduction=0.20, shipping_time_reduction=0.15,
        shipping_cost_change=0.03,
    ),
    "lean": Policy(
        "lean", reorder_point_days=5.0, safety_stock_days=1.0,
        order_quantity_multiplier=0.90, supplier_lead_time_reduction=0.05,
        defect_rate_reduction=0.08, shipping_time_reduction=0.05,
        shipping_cost_change=-0.08,
    ),
}


def load_data(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = sorted(REQUIRED_COLUMNS.difference(df.columns))
    if missing:
        raise ValueError(f"Input is missing required columns: {missing}")
    df = df.copy()
    numeric = list(REQUIRED_COLUMNS.difference({"SKU", "Supplier name"}))
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if df[numeric].isna().any().any():
        bad = df[numeric].columns[df[numeric].isna().any()].tolist()
        raise ValueError(f"Non-numeric or missing values in: {bad}")
    return df


def simulate_once(df: pd.DataFrame, policy: Policy, days: int, seed: int):
    rng = np.random.default_rng(seed)
    n = len(df)
    sku = df["SKU"].astype(str).to_numpy()
    price = (df["Revenue generated"] / df["Number of products sold"].clip(lower=1)).to_numpy(float)
    unit_cost = (df["Costs"] / df["Number of products sold"].clip(lower=1)).to_numpy(float)
    daily_demand = (df["Number of products sold"] / 30.0).to_numpy(float) * (1 + policy.demand_uplift)
    inventory = df["Stock levels"].to_numpy(float)
    base_order = df["Order quantities"].to_numpy(float) * policy.order_quantity_multiplier
    lead = np.maximum(1, np.rint(df["Lead times"].to_numpy(float) * (1 - policy.supplier_lead_time_reduction))).astype(int)
    ship_time = df["Shipping times"].to_numpy(float) * (1 - policy.shipping_time_reduction)
    defect = np.clip(df["Defect rates"].to_numpy(float) / 100 * (1 - policy.defect_rate_reduction), 0, 1)
    ship_cost = df["Shipping costs"].to_numpy(float) * (1 + policy.shipping_cost_change)
    reorder_point = daily_demand * (policy.reorder_point_days + policy.safety_stock_days)

    pipeline: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    revenue = cost = lost_units = sold_units = defective_units = inventory_sum = 0.0
    stockout_sku_days = orders = 0
    daily_rows = []
    sku_stockouts = np.zeros(n, dtype=int)
    sku_lost = np.zeros(n, dtype=float)

    for day in range(1, days + 1):
        for i in range(n):
            arrivals = sum(q for due, q in pipeline[i] if due <= day)
            if arrivals:
                inventory[i] += arrivals
            pipeline[i] = [(due, q) for due, q in pipeline[i] if due > day]

        demand = rng.poisson(np.maximum(0.01, daily_demand)).astype(float)
        fulfilled = np.minimum(inventory, demand)
        lost = demand - fulfilled
        defects = rng.binomial(fulfilled.astype(int), defect).astype(float)
        good_units = fulfilled - defects
        inventory -= fulfilled

        day_revenue = float(np.sum(good_units * price))
        day_cost = float(np.sum(fulfilled * unit_cost + fulfilled * ship_cost / np.maximum(base_order, 1)))
        revenue += day_revenue
        cost += day_cost
        sold_units += float(good_units.sum())
        defective_units += float(defects.sum())
        lost_units += float(lost.sum())
        inventory_sum += float(inventory.sum())
        out = lost > 0
        stockout_sku_days += int(out.sum())
        sku_stockouts += out.astype(int)
        sku_lost += lost

        inventory_position = inventory + np.array([sum(q for _, q in p) for p in pipeline])
        order_mask = inventory_position <= reorder_point
        for i in np.where(order_mask)[0]:
            target = daily_demand[i] * (lead[i] + policy.reorder_point_days + policy.safety_stock_days)
            qty = max(base_order[i], target - inventory_position[i])
            jittered_lead = max(1, int(round(lead[i] * rng.lognormal(0, 0.12))))
            pipeline[i].append((day + jittered_lead, float(qty)))
            cost += float(qty * unit_cost[i])
            orders += 1

        daily_rows.append({
            "scenario": policy.name, "day": day, "revenue": day_revenue,
            "operating_cost": day_cost, "units_sold": float(good_units.sum()),
            "lost_units": float(lost.sum()), "defective_units": float(defects.sum()),
            "ending_inventory": float(inventory.sum()), "stockout_skus": int(out.sum()),
        })

    demanded = sold_units + defective_units + lost_units
    metrics = {
        "scenario": policy.name,
        "revenue": revenue,
        "total_cost": cost,
        "profit": revenue - cost,
        "service_level_pct": 100 * (1 - lost_units / demanded) if demanded else 100.0,
        "lost_units": lost_units,
        "defect_rate_pct": 100 * defective_units / max(1, sold_units + defective_units),
        "avg_inventory_units": inventory_sum / days,
        "stockout_sku_days": stockout_sku_days,
        "orders_placed": orders,
        "avg_shipping_time_days": float(np.mean(ship_time)),
    }
    risks = pd.DataFrame({"SKU": sku, "stockout_days": sku_stockouts, "lost_units": sku_lost})
    return metrics, pd.DataFrame(daily_rows), risks


def run_twin(df: pd.DataFrame, policies: list[Policy], days: int, runs: int, seed: int):
    all_metrics, representative_daily, risk_frames = [], [], []
    for p_idx, policy in enumerate(policies):
        for run in range(runs):
            m, daily, risk = simulate_once(df, policy, days, seed + p_idx * 100_000 + run)
            m["run"] = run + 1
            all_metrics.append(m)
            risk["scenario"], risk["run"] = policy.name, run + 1
            risk_frames.append(risk)
            if run == 0:
                representative_daily.append(daily)
    raw = pd.DataFrame(all_metrics)
    value_cols = [c for c in raw.columns if c not in {"scenario", "run"}]
    summary = raw.groupby("scenario")[value_cols].agg(["mean", "std", lambda x: x.quantile(.05), lambda x: x.quantile(.95)])
    summary.columns = [f"{a}_{b if isinstance(b, str) else 'quantile'}" for a, b in summary.columns]
    summary = summary.reset_index().rename(columns=lambda c: c.replace("_<lambda_0>", "_p05").replace("_<lambda_1>", "_p95"))
    risks = pd.concat(risk_frames).groupby(["scenario", "SKU"], as_index=False).agg(stockout_days_mean=("stockout_days", "mean"), lost_units_mean=("lost_units", "mean"))
    return raw, summary, pd.concat(representative_daily, ignore_index=True), risks


def main() -> None:
    ap = argparse.ArgumentParser(description="Supply-chain digital twin")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, default=Path("digital_twin_output"))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--runs", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scenarios", nargs="+", default=["baseline", "recommended", "resilience", "lean"], choices=sorted(POLICIES))
    args = ap.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    df = load_data(args.input)
    policies = [POLICIES[name] for name in args.scenarios]
    raw, summary, daily, risks = run_twin(df, policies, args.days, args.runs, args.seed)
    raw.to_csv(args.output_dir / "monte_carlo_runs.csv", index=False)
    summary.to_csv(args.output_dir / "scenario_summary.csv", index=False)
    daily.to_csv(args.output_dir / "daily_twin_state.csv", index=False)
    risks.sort_values(["scenario", "lost_units_mean"], ascending=[True, False]).to_csv(args.output_dir / "sku_risk_register.csv", index=False)
    config = {"days": args.days, "runs": args.runs, "seed": args.seed, "policies": [asdict(p) for p in policies]}
    (args.output_dir / "twin_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
