"""Compare the captured prices in research/costs.json with research/live-prices.json.

The site's figures come from costs.json. live-prices.json is refreshed daily by
fetch_live.py from provider APIs and public pricing pages. This script is the
automated half of the weekly price review: for every captured row that has an
exact counterpart in the live data, it reports whether the price still matches.

Matching is an explicit table, one entry per captured row, keyed on the
provider's own identifier (Hetzner plan name, DigitalOcean and Vultr slug,
Linode label, Scaleway product, Azure SKU). Nothing is matched by spec or by
price, so a live figure can never land on the wrong row. Rows not in the table
are listed as not automated; the weekly review checks those by hand.

Usage: uv run python pricing/check_drift.py [--json PATH]
Exit code: 0 when every automated row matches, 1 when any row drifted or its
live record is missing, 2 when the input files cannot be read.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COSTS = ROOT / "research" / "costs.json"
LIVE = ROOT / "research" / "live-prices.json"

# Hours per month used by the captured Azure and Scaleway rows.
HOURS_PER_MONTH = 730
# Captured amounts carry at most 4 decimals; a cent absorbs display rounding.
TOLERANCE = 0.01
# Hourly API figures are rounded to 4 decimals, so hourly x 730 can sit up to
# 0.00005 x 730 = 0.0365 away from a monthly the provider computes unrounded
# (Azure's page does). Real price moves are far larger than this.
TOLERANCE_HOURLY = 0.00005 * HOURS_PER_MONTH + TOLERANCE

# costs.json id -> (live provider name, live id field, live id, price field).
# price field "hourly_eur*730" means the live hourly figure times 730.
MATCHES: dict[str, tuple[str, str, str, str]] = {
    "hetzner--cx23": ("Hetzner", "name", "CX23", "monthly_eur"),
    "hetzner--cx33": ("Hetzner", "name", "CX33", "monthly_eur"),
    "hetzner--cpx32": ("Hetzner", "name", "CPX32", "monthly_eur"),
    "hetzner--ccx33": ("Hetzner", "name", "CCX33", "monthly_eur"),
    "digitalocean--basic-droplet-regular-1-gib---1-vcpu": (
        "DigitalOcean", "slug", "s-1vcpu-1gb", "monthly_usd"),
    "digitalocean--basic-droplet-regular-4-gib---2-vcpu": (
        "DigitalOcean", "slug", "s-2vcpu-4gb", "monthly_usd"),
    "digitalocean--basic-droplet-regular-8-gib---4-vcpu": (
        "DigitalOcean", "slug", "s-4vcpu-8gb", "monthly_usd"),
    "vultr--cloud-compute-regular-performance-1-vcpu---1-gb": (
        "Vultr", "id", "vc2-1c-1gb", "monthly_usd"),
    "vultr--cloud-compute-regular-performance-2-vcpu---4-gb": (
        "Vultr", "id", "vc2-2c-4gb", "monthly_usd"),
    "vultr--cloud-compute-regular-performance-4-vcpu---8-gb": (
        "Vultr", "id", "vc2-4c-8gb", "monthly_usd"),
    "akamai--nanode-1-gb": (
        "Akamai Connected Cloud (Linode)", "label", "Nanode 1GB", "monthly_usd"),
    "akamai--linode-4-gb": (
        "Akamai Connected Cloud (Linode)", "label", "Linode 4GB", "monthly_usd"),
    "akamai--linode-8-gb": (
        "Akamai Connected Cloud (Linode)", "label", "Linode 8GB", "monthly_usd"),
    "scaleway--dev1-s": ("Scaleway", "id", "DEV1-S", "hourly_eur*730"),
    "scaleway--dev1-l": ("Scaleway", "id", "DEV1-L", "hourly_eur*730"),
    "scaleway--pro2-s": ("Scaleway", "id", "PRO2-S", "hourly_eur*730"),
    "microsoft--azure-virtual-machines-b2ls-v2-standard_b2ls_v2": (
        "Microsoft Azure", "sku_name", "B2ls v2", "retail_price_eur*730"),
    "microsoft--azure-virtual-machines-b2ts-v2-standard_b2ts_v2": (
        "Microsoft Azure", "sku_name", "B2ts v2", "retail_price_eur*730"),
    "microsoft--azure-virtual-machines-b2s-standard_b2s-previo": (
        "Microsoft Azure", "sku_name", "B2s", "retail_price_eur*730"),
}


def _azure_vm_row(plan: dict) -> bool:
    """Linux pay-as-you-go VM rows in West Europe, the captured basis."""
    product = plan.get("product_name") or ""
    return (
        plan.get("region") == "westeurope"
        and product.startswith("Virtual Machines")
        and "Windows" not in product
        and plan.get("price_type") in (None, "Consumption")
    )


def _find_live(providers: dict, provider: str, field: str, value: str) -> dict | None:
    rows = [p for p in providers.get(provider, []) if p.get(field) == value]
    if provider == "Microsoft Azure":
        rows = [p for p in rows if _azure_vm_row(p)]
    # Ambiguity is a miss, never a guess.
    return rows[0] if len(rows) == 1 else None


def _live_monthly(plan: dict, price_field: str) -> float | None:
    if price_field.endswith("*730"):
        hourly = plan.get(price_field.removesuffix("*730"))
        return None if hourly is None else round(hourly * HOURS_PER_MONTH, 4)
    value = plan.get(price_field)
    return None if value is None else float(value)


def check(costs: dict, live: dict) -> list[dict]:
    providers = {
        p["provider"]: p.get("plans") or []
        for p in live.get("providers", [])
        if p.get("status") == "ok"
    }
    results = []
    for plan in costs["plans"]:
        row = {
            "id": plan["id"],
            "provider": plan["provider"],
            "captured_amount": (plan.get("price") or {}).get("amount"),
            "currency": (plan.get("price") or {}).get("currency"),
            "captured_on": plan.get("captured_on"),
        }
        match = MATCHES.get(plan["id"])
        if match is None:
            results.append({**row, "status": "not_automated"})
            continue
        provider, field, value, price_field = match
        live_plan = _find_live(providers, provider, field, value)
        live_amount = None if live_plan is None else _live_monthly(live_plan, price_field)
        if live_plan is None or live_amount is None:
            results.append({**row, "status": "missing", "live_id": value})
            continue
        captured = row["captured_amount"]
        tolerance = TOLERANCE_HOURLY if price_field.endswith("*730") else TOLERANCE
        same = captured is not None and abs(live_amount - captured) <= tolerance
        results.append({
            **row,
            "status": "match" if same else "drift",
            "live_id": value,
            "live_amount": live_amount,
            "live_fetched_at": live_plan.get("fetched_at"),
        })
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", help="also write the full report to this path")
    args = parser.parse_args()
    try:
        costs = json.loads(COSTS.read_text())
        live = json.loads(LIVE.read_text())
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}", file=sys.stderr)
        return 2
    results = check(costs, live)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2) + "\n")
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] in ("drift", "missing"):
            print(
                f"{r['status'].upper()}: {r['id']} captured {r['captured_amount']} "
                f"{r['currency']} on {r['captured_on']}, live {r.get('live_amount')}"
            )
    print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("drift") or counts.get("missing") else 0


if __name__ == "__main__":
    sys.exit(main())
