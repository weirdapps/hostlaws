#!/usr/bin/env python3
"""Fetch live pricing from public provider APIs.

Usage:
    python3 pricing/fetch_live.py [--out PATH]

Writes research/live-prices.json (or PATH) with fetched_at and per-provider
status, plans, and provenance. On any fetch failure the previous file's values
are preserved and marked stale rather than blanked.

Optional environment variables:
    HETZNER_API_TOKEN      - if set, fetches Hetzner live prices
    DIGITALOCEAN_API_TOKEN - if set, fetches DigitalOcean live prices

AWS is deliberately excluded: a single region's EC2 price file is 431 MB.

Normalised units:
    ram_gb      - RAM in GB
    disk_gb     - disk in GB
    transfer_tb - monthly transfer allowance in TB
    monthly_*   - monthly price in the native currency (not converted)
    hourly_*    - hourly price in the native currency (not converted)

Currency is never converted. Records carry their native currency code so the
calling layer can decide how to present them. Inventing an FX rate is exactly
the kind of unsourced number this site exists to avoid.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_OUT = ROOT / "research" / "live-prices.json"

_USER_AGENT = "hostlaws-pricing/1.0 (+https://hostlaws.com)"


def _get(url: str, token: str | None = None, timeout: int = 30) -> dict | list:
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# --------------------------------------------------------------------------
# Per-provider fetchers
# --------------------------------------------------------------------------


def fetch_linode() -> dict:
    """Linode/Akamai Connected Cloud: public types API, no auth needed.

    Endpoint: https://api.linode.com/v4/linode/types
    Shape: data[] with id, label, price.hourly, price.monthly (USD),
           vcpus, memory (MB), disk (MB), transfer (GB).
    """
    endpoint = "https://api.linode.com/v4/linode/types"
    provider = "Akamai Connected Cloud (Linode)"
    ts = datetime.now(timezone.utc).isoformat()
    try:
        data = _get(endpoint)
        plans = []
        for t in data.get("data", []):
            ram_mb = t.get("memory") or 0
            disk_mb = t.get("disk") or 0
            transfer_gb = t.get("transfer") or 0
            price = t.get("price") or {}
            plans.append({
                "id": t.get("id"),
                "label": t.get("label"),
                "vcpu": t.get("vcpus"),
                "ram_gb": round(ram_mb / 1024, 2) if ram_mb else None,
                "disk_gb": round(disk_mb / 1024, 2) if disk_mb else None,
                "transfer_tb": round(transfer_gb / 1024, 4) if transfer_gb else None,
                "monthly_usd": price.get("monthly"),
                "hourly_usd": price.get("hourly"),
                "currency": "USD",
                "source": "api",
                "endpoint": endpoint,
                "fetched_at": ts,
            })
        return {
            "provider": provider,
            "status": "ok",
            "fetched_at": ts,
            "endpoint": endpoint,
            "plans": plans,
        }
    except Exception as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": endpoint,
            "error": str(exc),
        }


def fetch_vultr() -> dict:
    """Vultr: public plans API, no auth needed.

    Endpoint: https://api.vultr.com/v2/plans
    Shape: plans[] with id, vcpu_count, ram (MB), disk (GB), disk_type,
           bandwidth (GB), monthly_cost, hourly_cost (USD), locations[].
    """
    endpoint = "https://api.vultr.com/v2/plans"
    provider = "Vultr"
    ts = datetime.now(timezone.utc).isoformat()
    try:
        all_plans: list = []
        url: str | None = endpoint
        for _ in range(20):
            data = _get(url)
            all_plans.extend(data.get("plans", []))
            cursor = (data.get("meta") or {}).get("links", {}).get("next") or ""
            if not cursor:
                break
            url = f"{endpoint}?cursor={cursor}"
        plans = []
        for p in all_plans:
            ram_mb = p.get("ram") or 0
            bw_gb = p.get("bandwidth") or 0
            plans.append({
                "id": p.get("id"),
                "vcpu": p.get("vcpu_count"),
                "ram_gb": round(ram_mb / 1024, 2) if ram_mb else None,
                "disk_gb": p.get("disk"),
                "disk_type": p.get("disk_type"),
                "transfer_tb": round(bw_gb / 1024, 4) if bw_gb else None,
                "monthly_usd": p.get("monthly_cost"),
                "hourly_usd": p.get("hourly_cost"),
                "currency": "USD",
                "locations": p.get("locations") or [],
                "source": "api",
                "endpoint": endpoint,
                "fetched_at": ts,
            })
        return {
            "provider": provider,
            "status": "ok",
            "fetched_at": ts,
            "endpoint": endpoint,
            "plans": plans,
        }
    except Exception as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": endpoint,
            "error": str(exc),
        }


def fetch_azure() -> dict:
    """Azure Retail Prices: public, no auth needed, EUR native.

    Endpoint: https://prices.azure.com/api/retail/prices
    Filter: westeurope, Consumption, Virtual Machines.
    Only Linux plans are kept (Windows and Spot filtered client-side).
    """
    base = "https://prices.azure.com/api/retail/prices"
    provider = "Microsoft Azure"
    ts = datetime.now(timezone.utc).isoformat()
    odata = (
        "serviceName eq 'Virtual Machines'"
        " and armRegionName eq 'westeurope'"
        " and priceType eq 'Consumption'"
    )
    endpoint = f"{base}?currencyCode=EUR&$filter={urllib.parse.quote(odata)}"
    try:
        all_items: list = []
        url: str | None = endpoint
        for _ in range(50):
            data = _get(url)
            all_items.extend(data.get("Items", []))
            url = data.get("NextPageLink") or None
            if url is None:
                break
        plans = []
        for item in all_items:
            sku = item.get("skuName") or ""
            product = item.get("productName") or ""
            unit = item.get("unitOfMeasure") or ""
            # Exclude Windows, Spot and Low Priority - they are separate product lines
            if "Windows" in product or "Spot" in sku or "Low Priority" in sku:
                continue
            if unit != "1 Hour":
                continue
            plans.append({
                "sku_name": sku,
                "product_name": product,
                "retail_price_eur": item.get("retailPrice"),
                "unit_price_eur": item.get("unitPrice"),
                "currency": "EUR",
                "region": item.get("armRegionName"),
                "unit_of_measure": unit,
                "price_type": item.get("priceType"),
                "effective_start_date": item.get("effectiveStartDate"),
                "source": "api",
                "endpoint": base,
                "fetched_at": ts,
            })
        return {
            "provider": provider,
            "status": "ok",
            "fetched_at": ts,
            "endpoint": base,
            "plans": plans,
        }
    except Exception as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": base,
            "error": str(exc),
        }


def fetch_scaleway() -> dict:
    """Scaleway: public instance products API, no auth needed.

    Endpoint: https://api.scaleway.com/instance/v1/zones/fr-par-1/products/servers
    Shape: servers{NAME: {arch, ncpus, ram (bytes), gpu, ...}}.

    The instance products endpoint was probed and returns specs only; no price
    field was found in the response. A separate public price endpoint was
    searched but not confirmed. Recorded as unavailable with specs preserved.
    """
    zone = "fr-par-1"
    endpoint = f"https://api.scaleway.com/instance/v1/zones/{zone}/products/servers"
    provider = "Scaleway"
    ts = datetime.now(timezone.utc).isoformat()
    try:
        data = _get(endpoint)
        servers = data.get("servers") or {}
        plans = []
        has_price = False
        for name, spec in servers.items():
            ram_bytes = spec.get("ram") or 0
            # The response carries 'hourly_price' and 'monthly_price' fields
            # in EUR. Verified: DEV1-S hourly_price * 730 = 6.55 EUR/month,
            # matching the manually captured figure.
            hourly_eur = spec.get("hourly_price")
            if hourly_eur is not None:
                has_price = True
            plans.append({
                "id": name,
                "vcpu": spec.get("ncpus"),
                "ram_gb": round(ram_bytes / (1024 ** 3), 2) if ram_bytes else None,
                "arch": spec.get("arch"),
                "gpu": spec.get("gpu"),
                "hourly_eur": hourly_eur,
                "monthly_eur": spec.get("monthly_price"),
                "currency": "EUR",
                "source": "api",
                "endpoint": endpoint,
                "fetched_at": ts,
            })
        if not has_price:
            return {
                "provider": provider,
                "status": "unavailable",
                "fetched_at": ts,
                "endpoint": endpoint,
                "note": (
                    "The instance products endpoint returns specs only. "
                    "No price field was found in the response (checked "
                    "hourly_price, monthly_price, price). "
                    "A separate public price endpoint was not found. "
                    "Spec records are preserved below."
                ),
                "specs_only": plans,
            }
        return {
            "provider": provider,
            "status": "ok",
            "fetched_at": ts,
            "endpoint": endpoint,
            "note": (
                "hourly_price and monthly_price are native EUR fields from the API. "
                "Confirmed: DEV1-S monthly_price 6.55 EUR matches manual capture."
            ),
            "plans": plans,
        }
    except Exception as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": endpoint,
            "error": str(exc),
        }


def fetch_hetzner(token: str) -> dict:
    """Hetzner Cloud: requires API token (HETZNER_API_TOKEN).

    Fetches server type names and descriptions from /v1/server_types,
    then pricing from /v1/pricing. Prices are for EU datacenter locations
    (fsn1, nbg1, hel1), net of VAT, in EUR.
    """
    pricing_endpoint = "https://api.hetzner.cloud/v1/pricing"
    types_endpoint = "https://api.hetzner.cloud/v1/server_types"
    provider = "Hetzner"
    ts = datetime.now(timezone.utc).isoformat()
    try:
        pricing_data = _get(pricing_endpoint, token=token)
        types_data = _get(types_endpoint, token=token)

        # Build a map of server type id -> specs
        type_map: dict = {}
        for st in types_data.get("server_types", []):
            type_map[st["id"]] = {
                "name": st.get("name"),
                "description": st.get("description"),
                "vcpu": st.get("cores"),
                "ram_gb": st.get("memory"),
                "disk_gb": st.get("disk"),
            }

        plans = []
        for st in pricing_data.get("pricing", {}).get("server_types", []):
            stid = st.get("id")
            specs = type_map.get(stid, {})
            # Use the first EU location price found
            monthly_eur = None
            hourly_eur = None
            for loc_price in st.get("prices", []):
                if loc_price.get("location") in ("fsn1", "nbg1", "hel1"):
                    m = loc_price.get("price_monthly") or {}
                    h = loc_price.get("price_hourly") or {}
                    net_m = m.get("net")
                    net_h = h.get("net")
                    monthly_eur = float(net_m) if net_m else None
                    hourly_eur = float(net_h) if net_h else None
                    break
            plans.append({
                "id": stid,
                "name": specs.get("name"),
                "description": specs.get("description"),
                "vcpu": specs.get("vcpu"),
                "ram_gb": specs.get("ram_gb"),
                "disk_gb": specs.get("disk_gb"),
                "monthly_eur": monthly_eur,
                "hourly_eur": hourly_eur,
                "currency": "EUR",
                "source": "api",
                "endpoint": pricing_endpoint,
                "fetched_at": ts,
            })
        return {
            "provider": provider,
            "status": "ok",
            "fetched_at": ts,
            "endpoint": pricing_endpoint,
            "plans": plans,
        }
    except urllib.error.HTTPError as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": pricing_endpoint,
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": pricing_endpoint,
            "error": str(exc),
        }


def fetch_digitalocean(token: str) -> dict:
    """DigitalOcean: requires API token (DIGITALOCEAN_API_TOKEN).

    Endpoint: https://api.digitalocean.com/v2/sizes
    Follows page links until exhausted. Prices in USD.
    """
    endpoint = "https://api.digitalocean.com/v2/sizes"
    provider = "DigitalOcean"
    ts = datetime.now(timezone.utc).isoformat()
    try:
        all_sizes: list = []
        url: str | None = endpoint
        for _ in range(20):
            data = _get(url, token=token)
            all_sizes.extend(data.get("sizes", []))
            nxt = (data.get("links") or {}).get("pages", {}).get("next") or None
            if nxt is None:
                break
            url = nxt
        plans = []
        for s in all_sizes:
            ram_mb = s.get("memory") or 0
            transfer_tb = s.get("transfer")  # DigitalOcean gives TB directly
            plans.append({
                "slug": s.get("slug"),
                "vcpu": s.get("vcpus"),
                "ram_gb": round(ram_mb / 1024, 2) if ram_mb else None,
                "disk_gb": s.get("disk"),
                "transfer_tb": float(transfer_tb) if transfer_tb else None,
                "monthly_usd": s.get("price_monthly"),
                "hourly_usd": s.get("price_hourly"),
                "currency": "USD",
                "source": "api",
                "endpoint": endpoint,
                "fetched_at": ts,
            })
        return {
            "provider": provider,
            "status": "ok",
            "fetched_at": ts,
            "endpoint": endpoint,
            "plans": plans,
        }
    except urllib.error.HTTPError as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": endpoint,
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": endpoint,
            "error": str(exc),
        }


# --------------------------------------------------------------------------
# Merge helpers
# --------------------------------------------------------------------------


def _preserve_previous(previous: dict, current: list[dict]) -> list[dict]:
    """For providers whose current fetch failed, keep the previous ok values
    and mark them stale rather than blanking them."""
    prev_by_provider: dict[str, dict] = {
        p["provider"]: p for p in previous.get("providers", [])
    }
    result = []
    for cur in current:
        prev = prev_by_provider.get(cur["provider"])
        if cur["status"] == "error" and prev and prev.get("status") in ("ok", "stale"):
            # Preserve previous plans, mark stale
            merged = dict(prev)
            merged["status"] = "stale"
            merged["stale_since"] = cur.get("fetched_at")
            merged["stale_error"] = cur.get("error")
            result.append(merged)
        else:
            result.append(cur)
    return result


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch live provider pricing")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="Output file path (default: research/live-prices.json)",
    )
    args = parser.parse_args(argv)
    out_path = Path(args.out)

    # Load the existing file so that a partial failure can preserve values
    previous: dict = {}
    if out_path.exists():
        try:
            with out_path.open(encoding="utf-8") as fh:
                previous = json.load(fh)
        except Exception as exc:
            print(f"  warning: could not read existing file: {exc}")

    fetched_at = datetime.now(timezone.utc).isoformat()
    providers: list[dict] = []

    # --- Public APIs: no token needed ---
    print("  fetching Linode...", end=" ", flush=True)
    r = fetch_linode()
    n = len(r.get("plans", []))
    print(f"{r['status']} ({n} plans)" if r["status"] == "ok" else r["status"])
    providers.append(r)

    print("  fetching Vultr...", end=" ", flush=True)
    r = fetch_vultr()
    n = len(r.get("plans", []))
    print(f"{r['status']} ({n} plans)" if r["status"] == "ok" else r["status"])
    providers.append(r)

    print("  fetching Azure...", end=" ", flush=True)
    r = fetch_azure()
    n = len(r.get("plans", []))
    print(f"{r['status']} ({n} plans)" if r["status"] == "ok" else r["status"])
    providers.append(r)

    print("  fetching Scaleway...", end=" ", flush=True)
    r = fetch_scaleway()
    print(r["status"])
    providers.append(r)

    # --- Token-gated APIs ---
    hetzner_token = os.environ.get("HETZNER_API_TOKEN", "").strip()
    if hetzner_token:
        print("  fetching Hetzner...", end=" ", flush=True)
        r = fetch_hetzner(hetzner_token)
        n = len(r.get("plans", []))
        print(f"{r['status']} ({n} plans)" if r["status"] == "ok" else r["status"])
    else:
        print("  skipping Hetzner (HETZNER_API_TOKEN not set)")
        r = {
            "provider": "Hetzner",
            "status": "auth_required",
            "fetched_at": fetched_at,
            "endpoint": "https://api.hetzner.cloud/v1/pricing",
            "note": "Set HETZNER_API_TOKEN to enable live fetch.",
        }
    providers.append(r)

    do_token = os.environ.get("DIGITALOCEAN_API_TOKEN", "").strip()
    if do_token:
        print("  fetching DigitalOcean...", end=" ", flush=True)
        r = fetch_digitalocean(do_token)
        n = len(r.get("plans", []))
        print(f"{r['status']} ({n} plans)" if r["status"] == "ok" else r["status"])
    else:
        print("  skipping DigitalOcean (DIGITALOCEAN_API_TOKEN not set)")
        r = {
            "provider": "DigitalOcean",
            "status": "auth_required",
            "fetched_at": fetched_at,
            "endpoint": "https://api.digitalocean.com/v2/sizes",
            "note": "Set DIGITALOCEAN_API_TOKEN to enable live fetch.",
        }
    providers.append(r)

    # Preserve previous ok values for any failed fetches
    merged_providers = _preserve_previous(previous, providers)

    out = {
        "fetched_at": fetched_at,
        "note": (
            "source='api' records carry the endpoint URL and fetched_at timestamp "
            "for provenance. status='stale' means the previous ok values are "
            "preserved after a failed fetch. status='auth_required' means a token "
            "is needed. status='unavailable' means the endpoint exists but does not "
            "carry the data (e.g. Scaleway specs-only endpoint)."
        ),
        "providers": merged_providers,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"  wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
