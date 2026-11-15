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

Usage: uv run python pricing/check_drift.py [--json]
Exit code: 0 when every automated row matches, 1 when any row drifted or its
live record is missing, 2 when the input files cannot be read.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COSTS = ROOT / "research" / "costs.json"
EGRESS = ROOT / "research" / "egress.json"
LIVE = ROOT / "research" / "live-prices.json"

# Hours per month used by the captured Azure and Scaleway rows.
HOURS_PER_MONTH = 730
# Captured amounts carry at most 4 decimals; a cent absorbs display rounding.
TOLERANCE = 0.01
# Hourly API figures are rounded to 4 decimals, so hourly x 730 can sit up to
# 0.00005 x 730 = 0.0365 away from a monthly the provider computes unrounded
# (Azure's page does). Real price moves are far larger than this.
TOLERANCE_HOURLY = 0.00005 * HOURS_PER_MONTH + TOLERANCE

LINODE = "Akamai Connected Cloud (Linode)"
AZURE = "Microsoft Azure"
SCALEWAY_MONTHLY = "hourly_eur*730"
AZURE_MONTHLY = "retail_price_eur*730"
# Google's page yields a candidate set per machine and region (see
# fetch_live.parse_gcp_e2_prices): a captured figure in the set is confirmed.
GCP_CANDIDATES = "candidate_monthly_usd"

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
        LINODE, "label", "Nanode 1GB", "monthly_usd"),
    "akamai--linode-4-gb": (
        LINODE, "label", "Linode 4GB", "monthly_usd"),
    "akamai--linode-8-gb": (
        LINODE, "label", "Linode 8GB", "monthly_usd"),
    "scaleway--dev1-s": ("Scaleway", "id", "DEV1-S", SCALEWAY_MONTHLY),
    "scaleway--dev1-l": ("Scaleway", "id", "DEV1-L", SCALEWAY_MONTHLY),
    "scaleway--pro2-s": ("Scaleway", "id", "PRO2-S", SCALEWAY_MONTHLY),
    "amazon--amazon-ec2-t4g.small-linux-on-demand-europe-": (
        "Amazon Web Services (AWS)", "id", "t4g.small", "monthly_usd"),
    "google--compute-engine-e2-small-on-demand-belgium-eur": (
        "Google Cloud", "id", "e2-small@europe-west1", GCP_CANDIDATES),
    "microsoft--azure-virtual-machines-b2ls-v2-standard_b2ls_v2": (
        AZURE, "sku_name", "B2ls v2", AZURE_MONTHLY),
    "microsoft--azure-virtual-machines-b2ts-v2-standard_b2ts_v2": (
        AZURE, "sku_name", "B2ts v2", AZURE_MONTHLY),
    "microsoft--azure-virtual-machines-b2s-standard_b2s-previo": (
        AZURE, "sku_name", "B2s", AZURE_MONTHLY),
}


# egress.json provider name -> live "Egress" row id. Compared on overage_rate
# (USD per GB for all four). Google's figure is a candidate set from a page
# whose per-region tables list several tiers and destinations, so its check
# only proves the captured rate is still among the europe-west1 figures.
EGRESS_MATCHES: dict[str, str] = {
    "Amazon Web Services": "aws-eu-central-1-internet-next-10tb",
    "Akamai (Linode)": "linode-network-transfer",
    "Google Cloud": "gcp-europe-west1-internet",
    "Vultr": "vultr-bandwidth-overage",
}


def _check_egress(egress: dict, live_rows: list[dict]) -> list[dict]:
    by_id = {r.get("id"): r for r in live_rows}
    out = []
    for r in egress["providers"]:
        row = {"id": "egress:" + r["name"], "provider": r["name"],
               "captured_amount": r.get("overage_rate"), "currency": r.get("currency"),
               "captured_on": r.get("verified_on") or r.get("retrieved_on")}
        live_id = EGRESS_MATCHES.get(r["name"])
        live = by_id.get(live_id) if live_id else None
        if live_id is None:
            out.append({**row, "status": "not_automated"})
            continue
        captured = r.get("overage_rate")
        if live is None or captured is None:
            out.append({**row, "status": "missing", "live_id": live_id})
            continue
        same, amount = _egress_compare(live, captured)
        out.append({**row, "status": "match" if same else "drift", "live_id": live_id,
                    "live_amount": amount, "live_fetched_at": live.get("fetched_at")})
    return out


def _egress_compare(live: dict, captured: float):
    if "candidate_usd_per_gb" in live:
        same = any(abs(c - captured) < 1e-9 for c in live["candidate_usd_per_gb"])
        return same, (captured if same else live["candidate_usd_per_gb"])
    return abs(live["usd_per_gb"] - captured) < 1e-9, live["usd_per_gb"]


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
    value, _, region = value.partition("@")
    rows = [p for p in providers.get(provider, []) if p.get(field) == value
            and (not region or p.get("region") == region)]
    if provider == AZURE:
        rows = [p for p in rows if _azure_vm_row(p)]
    # Ambiguity is a miss, never a guess.
    return rows[0] if len(rows) == 1 else None


def _live_monthly(plan: dict, price_field: str, captured=None) -> float | None:
    if price_field == GCP_CANDIDATES:
        candidates = plan.get(GCP_CANDIDATES) or []
        hit = [c for c in candidates if captured is not None and abs(c - captured) < 1e-6]
        # The captured figure when confirmed; otherwise the nearest candidate,
        # so the report shows something to compare against.
        if hit:
            return hit[0]
        return min(candidates, key=lambda c: abs(c - (captured or 0))) if candidates else None
    if price_field.endswith("*730"):
        hourly = plan.get(price_field.removesuffix("*730"))
        return None if hourly is None else round(hourly * HOURS_PER_MONTH, 4)
    value = plan.get(price_field)
    return None if value is None else float(value)


def _check_row(plan: dict, providers: dict) -> dict:
    price = plan.get("price") or {}
    row = {
        "id": plan["id"],
        "provider": plan["provider"],
        "captured_amount": price.get("amount"),
        "currency": price.get("currency"),
        "captured_on": plan.get("captured_on"),
    }
    match = MATCHES.get(plan["id"])
    if match is None:
        return {**row, "status": "not_automated"}
    provider, field, value, price_field = match
    live_plan = _find_live(providers, provider, field, value)
    live_amount = (None if live_plan is None
                   else _live_monthly(live_plan, price_field, price.get("amount")))
    if live_plan is None or live_amount is None:
        return {**row, "status": "missing", "live_id": value}
    tolerance = TOLERANCE_HOURLY if price_field.endswith("*730") else TOLERANCE
    captured = row["captured_amount"]
    same = captured is not None and abs(live_amount - captured) <= tolerance
    return {
        **row,
        "status": "match" if same else "drift",
        "live_id": value,
        "live_amount": live_amount,
        "live_fetched_at": live_plan.get("fetched_at"),
    }


def check(costs: dict, live: dict, egress: dict | None = None) -> list[dict]:
    providers = {
        p["provider"]: p.get("plans") or []
        for p in live.get("providers", [])
        if p.get("status") == "ok"
    }
    results = [_check_row(plan, providers) for plan in costs["plans"]]
    if egress is not None:
        results += _check_egress(egress, providers.get("Egress", []))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--json", action="store_true", help="print the full report as JSON instead of a summary"
    )
    args = parser.parse_args()
    try:
        costs = json.loads(COSTS.read_text())
        live = json.loads(LIVE.read_text())
        egress = json.loads(EGRESS.read_text())
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}", file=sys.stderr)
        return 2
    results = check(costs, live, egress)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            if r["status"] in ("drift", "missing"):
                print(
                    f"{r['status'].upper()}: {r['id']} captured {r['captured_amount']} "
                    f"{r['currency']} on {r['captured_on']}, live {r.get('live_amount')}"
                )
        print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("drift") or counts.get("missing") else 0


if __name__ == "__main__":
    sys.exit(main())
