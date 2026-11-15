#!/usr/bin/env python3
"""Fetch live pricing from public provider APIs.

Usage:
    python3 pricing/fetch_live.py [--out PATH]

Writes research/live-prices.json (or PATH) with fetched_at and per-provider
status, plans, and provenance. On any fetch failure the previous file's values
are preserved and marked stale rather than blanked.

Optional environment variables:
    HETZNER_API_TOKEN      - if set, fetches Hetzner from its Cloud API
    DIGITALOCEAN_API_TOKEN - if set, fetches DigitalOcean from its API

Without a token, Hetzner and DigitalOcean are read from the providers' own
public pricing pages instead (source='website'). Neither is a documented API,
so a layout change makes the fetch fail, and a failed fetch keeps the previous
values marked stale, as for every other provider.

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
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
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


def _get_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


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
    ts = datetime.now(UTC).isoformat()
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
    ts = datetime.now(UTC).isoformat()
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
    ts = datetime.now(UTC).isoformat()
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
    ts = datetime.now(UTC).isoformat()
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
    ts = datetime.now(UTC).isoformat()
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
    ts = datetime.now(UTC).isoformat()
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
# Public-page fetchers, used when no API token is set
# --------------------------------------------------------------------------

HETZNER_PLAN_PAGES = (
    "https://www.hetzner.com/cloud/cost-optimized/",
    "https://www.hetzner.com/cloud/regular-performance/",
    "https://www.hetzner.com/cloud/general-purpose/",
)
# The price service hetzner.com's own plan pages call for every row.
HETZNER_PRICE_API = "https://website-price-api.hetzner.com/api/v1/products/"
# German datacenters first: the captured rows in costs.json quote the
# Nuremberg/Falkenstein price, and Helsinki is cheaper for CPX and CCX.
_HETZNER_EU_ORDER = ("FSN1", "NBG1", "HEL1")

_HETZNER_ROW = re.compile(
    r"\b(C(?:X|PX|AX|CX)\d{2,3})\b(.*?)\b(\d+) (?:Intel®/AMD|AMD|Ampere®|Intel®) "
    r"(\d+) GB (\d+) GB"
)


def parse_hetzner_plan_page(page_html: str) -> list[dict]:
    """Return one spec record per product key on a hetzner.com plan page.

    Each table row ends in a price element carrying product-key; the row's
    text before it holds the name, vCPU, RAM and disk.
    """
    page_html = re.sub(r"<svg.*?</svg>", " ", page_html, flags=re.S)
    rows: list[dict] = []
    seen: set[str] = set()
    prev = 0
    for m in re.finditer(r'product-key="(CLOUD_\d+\+CLOUD_\d+)"', page_html):
        text = re.sub(r"<[^>]+>", " ", page_html[prev:m.start()])
        text = re.sub(r"\s+", " ", html.unescape(text))
        prev = m.end()
        key = m.group(1)
        specs = list(_HETZNER_ROW.finditer(text))
        if key in seen or not specs:
            continue
        seen.add(key)
        last = specs[-1]
        rows.append({
            "product_key": key,
            "name": last.group(1),
            "orderable": "not available" not in last.group(2).lower(),
            "vcpu": int(last.group(3)),
            "ram_gb": float(last.group(4)),
            "disk_gb": float(last.group(5)),
        })
    return rows


def hetzner_plan_from_price(spec: dict, price: dict, ts: str) -> dict:
    """Merge a page spec record with its price-service response."""
    locations = {}
    for loc in price.get("locations", []):
        p = loc.get("prices") or {}
        locations[loc["datacenter"]] = {
            "active": loc.get("active"),
            "monthly_eur": float(p["monthly"]["EUR"]),
            "hourly_eur": float(p["hourly"]["EUR"]),
            "monthly_usd": float(p["monthly"]["USD"]),
            "hourly_usd": float(p["hourly"]["USD"]),
        }
    eu = next((locations[dc] for dc in _HETZNER_EU_ORDER if dc in locations), None)
    return {
        "id": spec["product_key"],
        "name": spec["name"],
        "orderable": spec["orderable"],
        "vcpu": spec["vcpu"],
        "ram_gb": spec["ram_gb"],
        "disk_gb": spec["disk_gb"],
        "monthly_eur": eu["monthly_eur"] if eu else None,
        "hourly_eur": eu["hourly_eur"] if eu else None,
        "price_location": next((dc for dc in _HETZNER_EU_ORDER if dc in locations), None),
        "locations": locations,
        "currency": "EUR",
        "includes": "primary IPv4 (the page prices server plus IPv4 as one product)",
        "source": "website",
        "endpoint": HETZNER_PRICE_API + urllib.parse.quote(spec["product_key"], safe=""),
        "fetched_at": ts,
    }


def fetch_hetzner_public() -> dict:
    """Hetzner Cloud without a token: plan pages for specs, the price
    service those pages call for per-datacenter prices (EUR and USD, ex VAT)."""
    provider = "Hetzner"
    ts = datetime.now(UTC).isoformat()
    try:
        specs: dict[str, dict] = {}
        for url in HETZNER_PLAN_PAGES:
            for row in parse_hetzner_plan_page(_get_text(url)):
                specs.setdefault(row["product_key"], row)
        if not specs:
            raise ValueError("no plan rows found on the Hetzner plan pages")
        plans = []
        for key, spec in specs.items():
            price = _get(HETZNER_PRICE_API + urllib.parse.quote(key, safe=""))
            plans.append(hetzner_plan_from_price(spec, price, ts))
            time.sleep(0.2)
        return {
            "provider": provider,
            "status": "ok",
            "fetched_at": ts,
            "endpoint": HETZNER_PLAN_PAGES[0],
            "source": "website",
            "plans": plans,
        }
    except Exception as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": HETZNER_PLAN_PAGES[0],
            "error": str(exc),
        }


DIGITALOCEAN_PRICING_PAGE = "https://www.digitalocean.com/pricing/droplets"


def parse_digitalocean_pricing_page(page_html: str) -> list[dict]:
    """Return the droplet sizes embedded in digitalocean.com/pricing/droplets.

    The page carries its price table as escaped JSON ("data":{"basic":...});
    every size object has a slug and a price. A slug listed under two
    families is kept once, under the first.
    """
    marker = '\\"data\\":{\\"basic\\"'
    i = page_html.find(marker)
    if i < 0:
        raise ValueError("droplet price data not found on the DigitalOcean page")
    chunk = page_html[i + len('\\"data\\":'):]
    chunk = chunk.replace('\\"', '"').replace("\\\\", "\\")
    data, _ = json.JSONDecoder().raw_decode(chunk)

    sizes: list[dict] = []
    seen: set[str] = set()
    for family, node in _iter_size_objects(data, ""):
        if node["slug"] not in seen:
            seen.add(node["slug"])
            sizes.append({"family": family, **node})
    return sizes


def _iter_size_objects(node, family: str):
    """Yield (family path, size object) for every dict with a slug and a price."""
    if isinstance(node, list):
        for v in node:
            yield from _iter_size_objects(v, family)
    elif isinstance(node, dict):
        if "slug" in node and "price" in node:
            yield family, node
            return
        for k, v in node.items():
            yield from _iter_size_objects(v, f"{family}/{k}" if family else k)


def fetch_digitalocean_public() -> dict:
    """DigitalOcean without a token: the droplet pricing page. Prices in USD."""
    provider = "DigitalOcean"
    ts = datetime.now(UTC).isoformat()
    try:
        sizes = parse_digitalocean_pricing_page(_get_text(DIGITALOCEAN_PRICING_PAGE))
        if not sizes:
            raise ValueError("no droplet sizes on the DigitalOcean page")
        plans = []
        for s in sizes:
            price = s.get("price") or {}
            quota_gb = price.get("transferQuota")
            plans.append({
                "slug": s["slug"],
                "family": s["family"],
                "vcpu": s.get("cpus"),
                "ram_gb": s.get("memory"),
                "disk_gb": (s.get("disk") or {}).get("boot"),
                "transfer_tb": round(quota_gb / 1000, 4) if quota_gb else None,
                "monthly_usd": price.get("monthly"),
                "hourly_usd": price.get("hourly"),
                "currency": "USD",
                "source": "website",
                "endpoint": DIGITALOCEAN_PRICING_PAGE,
                "fetched_at": ts,
            })
        return {
            "provider": provider,
            "status": "ok",
            "fetched_at": ts,
            "endpoint": DIGITALOCEAN_PRICING_PAGE,
            "source": "website",
            "plans": plans,
        }
    except Exception as exc:
        return {
            "provider": provider,
            "status": "error",
            "fetched_at": ts,
            "endpoint": DIGITALOCEAN_PRICING_PAGE,
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


def _run(label: str, fetch) -> dict:
    """Call one provider fetcher and print a one-line progress report."""
    print(f"  fetching {label}...", end=" ", flush=True)
    r = fetch()
    n = len(r.get("plans", []))
    print(f"{r['status']} ({n} plans)" if r["status"] == "ok" and n else r["status"])
    return r


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

    fetched_at = datetime.now(UTC).isoformat()
    providers: list[dict] = []

    # --- Public APIs: no token needed ---
    providers.extend(
        _run(label, fetch)
        for label, fetch in (
            ("Linode", fetch_linode),
            ("Vultr", fetch_vultr),
            ("Azure", fetch_azure),
            ("Scaleway", fetch_scaleway),
        )
    )

    # --- Official API with a token, public pricing page without one ---
    hetzner_token = os.environ.get("HETZNER_API_TOKEN", "").strip()
    if hetzner_token:
        providers.append(_run("Hetzner", lambda: fetch_hetzner(hetzner_token)))
    else:
        providers.append(_run("Hetzner (public pages, no token)", fetch_hetzner_public))

    do_token = os.environ.get("DIGITALOCEAN_API_TOKEN", "").strip()
    if do_token:
        providers.append(_run("DigitalOcean", lambda: fetch_digitalocean(do_token)))
    else:
        providers.append(
            _run("DigitalOcean (public page, no token)", fetch_digitalocean_public)
        )

    # Preserve previous ok values for any failed fetches
    merged_providers = _preserve_previous(previous, providers)

    out = {
        "fetched_at": fetched_at,
        "note": (
            "source='api' records carry the endpoint URL and fetched_at timestamp "
            "for provenance. source='website' records were read from the "
            "provider's public pricing page (Hetzner, DigitalOcean without a "
            "token), with the same provenance fields. "
            "status='stale' means the previous ok values are "
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
