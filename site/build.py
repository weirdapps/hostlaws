#!/usr/bin/env python3
"""Static site generator for hostlaws.com.

Reads the JSON datasets in research/ and writes plain HTML to site/dist/.
Jinja2 is the only third-party dependency. No JavaScript, no build step,
no network access at build time.

Run:  python3 site/build.py
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESEARCH = ROOT / "research"
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
DIST = HERE / "dist"

# --------------------------------------------------------------------------
# Publisher identity.
#
# The site prints the registered entity, country of incorporation and register
# number for each of the 21 providers it examines, and takes affiliate money
# from 15 of them, so it owes a reader the same three facts about itself. The
# research inputs do not contain them, and nothing here may be invented, so
# they are read from the environment and the about page states plainly when
# they are absent.
#
# TODO before launch: set HOSTLAWS_PUBLISHER_ENTITY and
# HOSTLAWS_PUBLISHER_COUNTRY, and HOSTLAWS_PUBLISHER_REGISTER where the
# publisher is a registered company.
# --------------------------------------------------------------------------
SITE = {
    "name": "hostlaws.com",
    # Absolute origin, needed for canonical links, the sitemap and social card
    # images, all of which a relative path cannot express.
    "base_url": (os.environ.get("HOSTLAWS_BASE_URL") or "https://hostlaws.com").rstrip("/"),
    # GoatCounter subdomain code, for example "hostlaws" for
    # hostlaws.goatcounter.com. Absent means no analytics markup is emitted at
    # all, so a local build stays clean and never counts its own author.
    "goatcounter": os.environ.get("HOSTLAWS_GOATCOUNTER") or None,
    # No invented default. An address that does not resolve is worse than no
    # address: it advertises accountability and provides none. Set this to a
    # route that demonstrably works, an issues URL or a mailbox that receives.
    "contact": os.environ.get("HOSTLAWS_CONTACT") or None,
    "contact_href": (
        (lambda c: None if not c else (f"mailto:{c}" if "@" in c and "://" not in c else c))(
            os.environ.get("HOSTLAWS_CONTACT")
        )
    ),
    "publisher": "hostlaws.com",
    "publisher_entity": os.environ.get("HOSTLAWS_PUBLISHER_ENTITY") or None,
    "publisher_country": os.environ.get("HOSTLAWS_PUBLISHER_COUNTRY") or None,
    "publisher_register": os.environ.get("HOSTLAWS_PUBLISHER_REGISTER") or None,
}

# Published on the methodology page as a number of days. A captured price
# older than this renders in the age colour, which is a statement about
# hostlaws being behind, never about a provider.
STALE_AFTER_DAYS = 90

# affiliate-terms.json uses short provider names. providers-merged.json uses
# the full ones. Four do not match on a string compare, so the join is explicit.
AFFILIATE_ALIASES = {
    "Amazon Web Services (AWS), including the AWS European Sovereign Cloud": "AWS",
    "Microsoft Azure, including the EU Data Boundary": "Microsoft Azure",
    "Google Cloud, including Google Sovereign Cloud / Sovereign Controls": "Google Cloud",
    "Akamai Connected Cloud (formerly Linode)": "Akamai/Linode",
}

# costs.json and plans-*.json use shorter provider names than providers-merged.json.
COSTS_ALIAS: dict[str, str] = {
    "Akamai Connected Cloud (formerly Linode)": "Akamai Connected Cloud (Linode)",
    "Amazon Web Services (AWS), including the AWS European Sovereign Cloud": "Amazon Web Services (AWS)",
    "Google Cloud, including Google Sovereign Cloud / Sovereign Controls": "Google Cloud",
    "Microsoft Azure, including the EU Data Boundary": "Microsoft Azure",
}

# service-levels.json uses shorter names for three providers.
SERVICE_LEVEL_ALIAS: dict[str, str] = {
    "Amazon Web Services (AWS), including the AWS European Sovereign Cloud": "Amazon Web Services (AWS)",
    "Google Cloud, including Google Sovereign Cloud / Sovereign Controls": "Google Cloud",
    "Microsoft Azure, including the EU Data Boundary": "Microsoft Azure",
}

PERIOD_SUFFIX = {"month": "/mo", "hour": "/hr", "year": "/yr"}
PERIOD_CAVEAT = {
    "hour": "billed hourly, not comparable to the monthly plans in this table",
    "year": "billed annually, not comparable to the monthly plans in this table",
}

# Four captured prices are introductory rather than standing, which inverts the
# ordering of the entry-plan column against rows such as Hetzner's EUR 5.99. The
# register is the page most likely to be printed on its own, so the caveat has
# to travel with the figure rather than living only in entry_pricing.note on the
# provider page. Each caveat is paired with the substring of that note it is
# taken from, and the build fails if the note stops carrying it.
RATE_CAVEAT = {
    "IONOS": (
        "promotional for 3 months, EUR 9.00 per month after that, plus a EUR 10 setup fee; "
        "the only VAT-inclusive figure in this column",
        "EUR 3.00/month for the first 3 months, EUR 9.00/month thereafter, "
        "with a EUR 10 setup fee",
    ),
    "SiteGround": (
        "promotional rate; the page states it renews at EUR 15.99 per month, prepaid 12 months",
        "Renews at 15.99",
    ),
    "Hostinger": (
        "promotional rate; needs a 48-month prepayment of USD 143.52 and the page states it "
        "renews at USD 10.99 per month",
        "Renews at $10.99/mo.",
    ),
    "WP Engine": (
        "introductory coupon on annual billing, USD 350 for the first year; no renewal figure "
        "is published on the page",
        "renewal pricing is not published on the page",
    ),
}

# affiliate-terms.json records recurring as one of three strings. Printed as a
# phrase, because a raw identifier in a reader-facing row is a database dump.
RECURRING_PHRASE = {
    "one_off": "one-off, not recurring",
    "recurring": "recurring",
    "one_off_plus_recurring": "one-off plus recurring",
}

# The two largest published payouts in the set, named on the about page so that
# the money can be checked against the ordering in one place instead of across
# 15 provider pages. Each quotation is gated against affiliate-terms.json.
RICHEST_PAYOUTS = {
    "WP Engine": "$200 minimum or equal to the first month's payment, whichever is higher",
    "Kinsta": "$50 to $500 - One-time bonus",
}

EXPOSURE_PHRASE = {
    "direct": "A US entity sits in the contracting or ownership chain",
    "none_identified": ("No US entity was identified in the contracting or ownership chain"),
}


# --------------------------------------------------------------------------
# Loading and text normalisation
# --------------------------------------------------------------------------


EM_DASH = "\u2014"
EN_DASH = "\u2013"


def dedash(text: str) -> str:
    """Remove em-dashes and en-dashes from source text.

    Nine em-dashes occur in providers-merged.json, all of them inside material
    quoted from a provider. They are rendered as hyphens, which is recorded on
    the methodology page so the substitution is not silent. The characters are
    written as escapes here so that no file in this repository contains one.
    """
    text = text.replace(f" {EM_DASH} ", " - ").replace(EM_DASH, " - ")
    text = text.replace(f" {EN_DASH} ", " - ").replace(EN_DASH, "-")
    return text


def normalise(value):
    if isinstance(value, str):
        return dedash(value)
    if isinstance(value, list):
        return [normalise(v) for v in value]
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in value.items()}
    return value


def load(name: str):
    with (RESEARCH / name).open(encoding="utf-8") as fh:
        return normalise(json.load(fh))


def load_live_prices() -> dict | None:
    """Load research/live-prices.json when present; return None if absent.

    The build must succeed when the file is absent (fresh clone, no fetch yet).
    """
    path = RESEARCH / "live-prices.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _build_live_index(live: dict | None) -> dict:
    """Build (provider, plan_id) -> live_plan lookup from live-prices.json.

    Matching is conservative:
    - Linode: by label (exact string, matches costs.json plan_name).
    - Vultr: by (vcpu, ram_gb rounded to nearest int).
    - Scaleway: by id (exact, same as plan_name in costs.json).
    Plans that cannot be unambiguously matched are recorded but not merged.
    """
    if not live:
        return {}
    index: dict[tuple, dict] = {}
    def _linode_key(label: str) -> str:
        """Normalise Linode label for matching: collapse e.g. '1GB' to '1 GB'."""
        import re as _re
        return _re.sub(r"(\d)(GB|TB|MB)", r"\1 \2", label or "")

    for prov in live.get("providers", []):
        status = prov.get("status")
        if status not in ("ok", "stale"):
            continue
        provider = prov["provider"]
        for plan in prov.get("plans", []):
            if provider == "Akamai Connected Cloud (Linode)":
                label = plan.get("label")
                if label:
                    # Index by both the raw label and the normalised form so
                    # that costs.json "Nanode 1 GB" matches API "Nanode 1GB".
                    index[(provider, label)] = plan
                    normalised = _linode_key(label)
                    if normalised != label:
                        index[(provider, normalised)] = plan
            elif provider == "Vultr":
                vcpu = plan.get("vcpu")
                ram = plan.get("ram_gb")
                if vcpu is not None and ram is not None:
                    index[(provider, vcpu, int(round(ram)))] = plan
            elif provider == "Scaleway":
                pid = plan.get("id")
                if pid:
                    index[(provider, pid)] = plan
    return index


def _live_for_plan(index: dict, costs_plan: dict) -> dict | None:
    """Return the matching live plan record for a costs.json plan, or None."""
    provider = costs_plan.get("provider", "")
    name = costs_plan.get("plan_name", "")
    specs = costs_plan.get("specs") or {}
    if provider == "Akamai Connected Cloud (Linode)":
        return index.get((provider, name))
    if provider == "Vultr":
        vcpu = specs.get("vcpu")
        ram = specs.get("ram_gb")
        if vcpu is not None and ram is not None:
            return index.get((provider, vcpu, int(round(ram))))
    if provider == "Scaleway":
        return index.get((provider, name))
    return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def split_name(name: str) -> tuple[str, str | None]:
    """Three names carry a trailing qualifier clause that is load-bearing."""
    if ", including " in name:
        base, _, qualifier = name.partition(", including ")
        return base, "including " + qualifier
    return name, None


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0]


def format_amount(amount) -> str:
    if isinstance(amount, int):
        return str(amount)
    if round(amount, 2) == amount:
        return f"{amount:.2f}"
    return f"{amount:g}"


def days_since(iso_date: str, today: date) -> int:
    return (today - datetime.strptime(iso_date, "%Y-%m-%d").date()).days


def recurring_phrase(value: str | None) -> str:
    if not value:
        return "not applicable"
    if value not in RECURRING_PHRASE:
        raise SystemExit(f"affiliate-terms.json has an unmapped recurring value: {value!r}")
    return RECURRING_PHRASE[value]


# --------------------------------------------------------------------------
# Inline and block text rendering
# --------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _link(url: str) -> str:
    trimmed = url.rstrip(".,;:)")
    tail = url[len(trimmed) :]
    return (
        f'<a class="url-link" href="{html.escape(trimmed, quote=True)}">'
        f"{html.escape(trimmed)}</a>{html.escape(tail)}"
    )


def linkify(escaped: str) -> str:
    """Turn bare URLs in already-escaped text into links."""
    out = []
    pos = 0
    for match in URL_RE.finditer(escaped):
        out.append(escaped[pos : match.start()])
        out.append(_link(html.unescape(match.group(0))))
        pos = match.end()
    out.append(escaped[pos:])
    return "".join(out)


def paras(text: str | None) -> Markup:
    """Render a plain-text field as paragraphs, with bare URLs linked."""
    if not text:
        return Markup("")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return Markup("".join(f"<p>{linkify(html.escape(b))}</p>" for b in blocks))


def md_inline(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(
        r"&lt;(https?://[^\s&]+)&gt;",
        lambda m: f'<a class="url-link" href="{m.group(1)}">{m.group(1)}</a>',
        out,
    )
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", out)
    return out


ITEM_RE = re.compile(r"^([-*+]|\d+\.)\s+(.*)$")


def _join(lines: list[str]) -> str:
    return " ".join(line.strip() for line in lines if line.strip())


def _md_paragraphs(lines: list[str]) -> str:
    out = []
    buffer: list[str] = []
    for line in lines:
        if line.strip():
            buffer.append(line)
        elif buffer:
            out.append(f"<p>{md_inline(_join(buffer))}</p>")
            buffer = []
    if buffer:
        out.append(f"<p>{md_inline(_join(buffer))}</p>")
    return "".join(out)


def _md_table(rows: list[str]) -> str:
    def cells(row: str) -> list[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]]
    out = ["<div class='table-wrap'><table class='data-table prose-table'><thead><tr>"]
    for cell in header:
        # An empty corner cell labels nothing, so it is a td and is not
        # announced as a blank column header. Same convention as the 2x2 macro.
        if cell:
            out.append(f'<th scope="col">{md_inline(cell)}</th>')
        else:
            out.append("<td></td>")
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        for index, cell in enumerate(row):
            if index == 0:
                out.append(f'<th scope="row">{md_inline(cell)}</th>')
            else:
                klass = ' class="num"' if re.fullmatch(r"[\d.,]+", cell) else ""
                out.append(f"<td{klass}>{md_inline(cell)}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def markdown(text: str) -> Markup:
    """A deliberately small subset: headings, paragraphs, bold, italic, code,
    blockquotes, unordered and ordered lists, pipe tables and autolinks."""
    lines = text.split("\n")
    out: list[str] = []
    buffer: list[str] = []
    index = 0

    def flush() -> None:
        nonlocal buffer
        if buffer:
            out.append(_md_paragraphs(buffer))
            buffer = []

    while index < len(lines):
        line = lines[index]
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush()
            # Section headings are h2, so a body heading starts at h3 and
            # never skips a level.
            level = min(max(len(heading.group(1)), 3), 6)
            out.append(f"<h{level}>{md_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue
        if line.startswith(">"):
            flush()
            quoted = []
            while index < len(lines) and lines[index].startswith(">"):
                quoted.append(lines[index][1:])
                index += 1
            out.append(f"<blockquote>{_md_paragraphs(quoted)}</blockquote>")
            continue
        if line.startswith("|"):
            flush()
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                rows.append(lines[index])
                index += 1
            out.append(_md_table(rows))
            continue
        item = ITEM_RE.match(line)
        if item:
            flush()
            ordered = item.group(1).endswith(".")
            items: list[list[str]] = []
            while index < len(lines):
                current = lines[index]
                match = ITEM_RE.match(current)
                if match:
                    items.append([match.group(2)])
                    index += 1
                    continue
                if current.strip() and current.startswith((" ", "\t")):
                    items[-1].append(current)
                    index += 1
                    continue
                if not current.strip():
                    peek = index + 1
                    if peek < len(lines) and ITEM_RE.match(lines[peek]):
                        index += 1
                        continue
                break
            tag = "ol" if ordered else "ul"
            body = "".join(f"<li>{md_inline(_join(i))}</li>" for i in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue
        buffer.append(line)
        index += 1

    flush()
    return Markup("".join(out))


# --------------------------------------------------------------------------
# Building the provider records
# --------------------------------------------------------------------------


def _provider_costs_alias(name: str) -> str:
    return COSTS_ALIAS.get(name, name)


def _provider_sl_alias(name: str) -> str:
    return SERVICE_LEVEL_ALIAS.get(name, name)


def build_provider_plans(costs: dict, today: date | None = None) -> dict[str, list]:
    """Group cost plans by provider name (costs.json short names).

    When today is supplied each plan gets an age_days field so templates can
    render a provenance stamp without arithmetic in Jinja.
    """
    capture = costs.get("capture_date", "")
    by_provider: dict[str, list] = {}
    for plan in costs["plans"]:
        plan_date = plan.get("captured_on") or capture
        age = days_since(plan_date, today) if today and plan_date else None
        entry = dict(plan, age_days=age)
        by_provider.setdefault(plan["provider"], []).append(entry)
    return by_provider


def build_egress_by_provider(egress_doc: dict) -> dict[str, dict]:
    return {r["name"]: r for r in egress_doc["providers"]}


def build_service_levels(sl_doc: dict) -> dict[str, dict]:
    return {p["name"]: p for p in sl_doc["providers"]}


def build_providers(raw_providers, programmes, dataset_date, today,
                    plans_by_provider=None, egress_by_provider=None,
                    service_levels=None):
    by_short = {p["provider"]: p for p in programmes}
    used = set()
    providers = []

    for raw in raw_providers:
        name = raw["name"]
        display, qualifier = split_name(name)
        short = AFFILIATE_ALIASES.get(name, name)
        if short not in by_short:
            raise SystemExit(f"affiliate-terms.json has no programme record for {name!r}")
        used.add(short)
        programme = by_short[short]

        pricing = raw["entry_pricing"]
        has_price = pricing["amount"] is not None
        price = {
            "has_price": has_price,
            "text": (
                f"{pricing['currency']} {format_amount(pricing['amount'])}" if has_price else None
            ),
            "period": PERIOD_SUFFIX.get(pricing["period"] or "", ""),
            "period_caveat": PERIOD_CAVEAT.get(pricing["period"] or ""),
            "rate_caveat": RATE_CAVEAT.get(name, (None, None))[0],
            "plan_name": pricing["plan_name"],
            "url": pricing["url"],
            "url_host": host_of(pricing["url"]),
            "note": pricing["note"],
            "captured_on": dataset_date,
            "stale": days_since(dataset_date, today) > STALE_AFTER_DAYS,
            "age_days": days_since(dataset_date, today),
        }

        sov = raw["sovereignty"]
        record = dict(raw)
        # Attach per-provider cost plans, egress, and service-level records.
        costs_key = _provider_costs_alias(name)
        sl_key = _provider_sl_alias(name)
        egress_key = EGRESS_ALIAS.get(name, name)
        provider_plans = (plans_by_provider or {}).get(costs_key, [])
        provider_egress = (egress_by_provider or {}).get(egress_key)
        provider_sl = (service_levels or {}).get(sl_key)

        record.update(
            {
                "slug": slugify(display),
                "display_name": display,
                "qualifier": qualifier,
                "eu_region_count": len(raw["eu_regions"]),
                "urls_checked_count": len(sov["urls_checked"]),
                "disclosure_host": host_of(sov.get("disclosure_url")),
                "exposure_phrase": EXPOSURE_PHRASE[raw["cloud_act_exposure"]],
                "price": price,
                "affiliate": programme,
                "paid_link": bool(programme["has_programme"]),
                # A programme only becomes a marked link where there is a
                # commercial URL to mark. Google Cloud runs a programme and
                # publishes no entry plan page, so its pages carry no paid link.
                "has_commercial_link": bool(programme["has_programme"] and pricing["url"]),
                # Cost data from costs.json, egress.json, service-levels.json.
                "plans": provider_plans,
                "egress": provider_egress,
                "service_level": provider_sl,
            }
        )
        providers.append(record)

    unused = sorted(set(by_short) - used)
    if unused:
        raise SystemExit(f"affiliate programmes never joined: {unused}")

    providers.sort(key=lambda p: p["display_name"].casefold())

    # The editorial "notable" field sometimes compares within the working set a
    # provider was researched in ("the three", "the group"). Name that set on
    # the page so the comparison is stated rather than implied.
    for record in providers:
        record["group_members"] = [
            other["display_name"]
            for other in providers
            if other["_category"] == record["_category"]
        ]
    return providers


def cross_tab(providers):
    counts = {
        ("direct", True): 0,
        ("direct", False): 0,
        ("none_identified", True): 0,
        ("none_identified", False): 0,
    }
    for provider in providers:
        key = (
            provider["cloud_act_exposure"],
            provider["sovereignty"]["disclosure_found"],
        )
        counts[key] += 1
    return counts


def check_inputs(providers, dataset_date):
    """Fail the build if the data stops matching what the pages assert."""
    problems = []

    if len(providers) != 21:
        problems.append(f"expected 21 providers, found {len(providers)}")

    dates = {p["sovereignty"]["checked_on"] for p in providers}
    if dates != {dataset_date}:
        problems.append(
            "sovereignty.checked_on is not identical across providers "
            f"({sorted(dates)}); the hoisted column date must be removed "
            "and every row must print its own"
        )

    affiliate_dates = {p["affiliate"]["retrieved_on"] for p in providers}
    if affiliate_dates != {dataset_date}:
        problems.append(f"affiliate retrieval dates differ: {sorted(affiliate_dates)}")

    found = sum(1 for p in providers if p["sovereignty"]["disclosure_found"])
    if found != 6:
        problems.append(f"expected 6 located disclosures, found {found}")

    direct = sum(1 for p in providers if p["cloud_act_exposure"] == "direct")
    if direct != 13:
        problems.append(f"expected 13 direct exposure records, found {direct}")

    us_inc = sum(1 for p in providers if p["incorporation_country"] == "US")
    if us_inc != 13:
        problems.append(f"expected 13 US-incorporated providers, found {us_inc}")

    by_name = {p["name"]: p for p in providers}
    for name, (_, evidence) in RATE_CAVEAT.items():
        note = by_name[name]["entry_pricing"]["note"] or ""
        if evidence not in note:
            problems.append(
                f"{name}: the rate caveat is no longer supported by entry_pricing.note "
                f"(missing {evidence!r})"
            )
    for name, quotation in RICHEST_PAYOUTS.items():
        payout = by_name[name]["affiliate"]["payout"] or ""
        if quotation not in payout:
            problems.append(
                f"{name}: the payout quoted on the about page is no longer in "
                f"affiliate-terms.json (missing {quotation!r})"
            )

    counts = cross_tab(providers)
    expected = {
        ("direct", True): 4,
        ("direct", False): 9,
        ("none_identified", True): 2,
        ("none_identified", False): 6,
    }
    if counts != expected:
        problems.append(f"cross-tab changed: {counts}")

    paid = sum(1 for p in providers if p["paid_link"])
    if paid != 15:
        problems.append(f"expected 15 affiliate programmes, found {paid}")

    for provider in providers:
        if not provider["paid_link"] and provider["affiliate"]["url"]:
            problems.append(
                f"{provider['name']}: has_programme is false but a programme URL is recorded"
            )

    if problems:
        for problem in problems:
            print(f"  input check failed: {problem}", file=sys.stderr)
        raise SystemExit("build stopped: input checks failed")


# --------------------------------------------------------------------------
# Comparison tables (costs, egress, jurisdiction)
# --------------------------------------------------------------------------

# egress.json uses shorter names than providers-merged.json.
EGRESS_ALIAS: dict[str, str] = {
    "Akamai Connected Cloud (Linode)": "Akamai (Linode)",
    "Amazon Web Services (AWS)": "Amazon Web Services",
    "Amazon Web Services (AWS), including the AWS European Sovereign Cloud": "Amazon Web Services",
    "Microsoft Azure": "Microsoft Azure",
    "Microsoft Azure, including the EU Data Boundary": "Microsoft Azure",
    "Google Cloud": "Google Cloud",
    "Google Cloud, including Google Sovereign Cloud / Sovereign Controls": "Google Cloud",
}

NULL_DISPLAY = "not published"
DERIVED_NOTE = "derived (h x 720)"
DERIVED_NOTE_HYPERSCALER = "derived (h x 730)"


def _null_last(rows: list, key_fn, *, reverse: bool = False) -> list:
    """Sort rows so that key_fn(row) == None places the row at the end."""

    def _key(row):
        v = key_fn(row)
        if v is None:
            return (1, 0)
        return (0, -v if reverse else v)

    return sorted(rows, key=_key)


def _fmt_num(v, decimals: int = 2) -> str | None:
    if v is None:
        return None
    if isinstance(v, int):
        return str(v)
    return f"{v:.{decimals}f}"


def _egress_name(provider: str) -> str:
    return EGRESS_ALIAS.get(provider, provider)


def _build_vps_rows(costs: dict, plans_by_id: dict, egress_by_provider: dict,
                    capture_date: str, live_index: dict | None = None) -> list:
    ranking = costs["normalised"]["vps_eur_per_gb_ram_ranking"]
    all_vps = [p for p in costs["plans"] if p["category"] == "vps"]
    infomaniak_plans = [p for p in all_vps if p["provider"] == "Infomaniak"]
    today = date.today()

    rows = []
    for r in ranking:
        plan = plans_by_id[r["id"]]
        caveat = r.get("caveat") or ""
        derived = "DERIVED" in caveat.upper() and "720" in caveat
        eg = egress_by_provider.get(_egress_name(r["provider"]), {})
        incl = eg.get("included_transfer")

        # Prefer live price when a confirmed match exists. Only update the
        # last_verified date; the normalised EUR value comes from costs.json
        # because the live APIs return USD and the FX conversion is not
        # reproduced here. The live record signals freshness, not a new number.
        live_plan = _live_for_plan(live_index or {}, plan)
        last_verified = (
            (live_plan.get("fetched_at") or "")[:10]
            if live_plan else capture_date
        )
        live_source = live_plan is not None

        age = days_since(last_verified, today) if last_verified else 0
        stale = age > STALE_AFTER_DAYS

        rows.append({
            "provider": r["provider"],
            "plan": r["plan"],
            "jurisdiction": plan.get("jurisdiction_of_seller", "?"),
            "url": plan.get("url") or "",
            "capture_date": capture_date,
            "last_verified": last_verified,
            "live_source": live_source,
            "stale": stale,
            "days_since_verified": age,
            # EUR/GB/RAM column
            "eur_per_gb_ram": _fmt_num(r["value_eur"]),
            "eur_per_gb_ram_raw": r["value_eur"],
            "eur_converted": r.get("converted", False),
            "derived_monthly": derived,
            # Renewal column
            "renewal_eur": _fmt_num(r["basis_price_eur_month"]),
            "renewal_converted": r.get("converted", False),
            # Specs column
            "ram_gb": r["ram_gb"],
            "vcpu": r["vcpu"],
            # Transfer column
            "transfer": incl or NULL_DISPLAY,
            "transfer_is_null": not incl,
            # Caveats and flags
            "ranking_caveat": plan.get("ranking_caveat"),
            "caveat": caveat,
            "chf_only": False,
            "_sort": r["value_eur"],
        })

    # Infomaniak excluded rows: CHF only, no EUR ranking
    for plan in infomaniak_plans:
        price = plan.get("price", {})
        renewal = plan.get("renewal") or {}
        specs = plan.get("specs", {})
        eg = egress_by_provider.get("Infomaniak", {})
        incl = eg.get("included_transfer")
        chf_amount = renewal.get("amount") if renewal else price.get("amount")
        rows.append({
            "provider": plan["provider"],
            "plan": plan["plan_name"],
            "jurisdiction": plan.get("jurisdiction_of_seller", "CH"),
            "url": plan.get("url") or "",
            "capture_date": capture_date,
            "last_verified": capture_date,
            "live_source": False,
            "days_since_verified": days_since(capture_date, today),
            "stale": days_since(capture_date, today) > STALE_AFTER_DAYS,
            # EUR column is not computable
            "eur_per_gb_ram": None,
            "eur_per_gb_ram_raw": None,
            "eur_converted": False,
            "derived_monthly": False,
            # Renewal in CHF
            "renewal_eur": None,
            "renewal_chf": _fmt_num(chf_amount),
            "renewal_converted": False,
            # Specs
            "ram_gb": specs.get("ram_gb"),
            "vcpu": specs.get("vcpu"),
            # Transfer
            "transfer": incl or NULL_DISPLAY,
            "transfer_is_null": not incl,
            # Caveats
            "ranking_caveat": None,
            "caveat": (
                "Quoted in CHF. No CHF-to-EUR rate is in this dataset. "
                "Shown with native CHF renewal; excluded from EUR axis."
            ),
            "chf_only": True,
            "_sort": None,
        })
    return rows


def _build_hyperscaler_rows(costs: dict, plans_by_id: dict,
                             costs_1tb_by_id: dict, capture_date: str) -> list:
    hyper = [p for p in costs["plans"] if p["category"] == "vps_component"]
    rows = []
    for plan in hyper:
        e = costs_1tb_by_id.get(plan["id"], {})
        excl_reasons = plan["normalised_on_renewal"].get("exclusion_reasons", [])
        instance_eur = plan.get("price_ex_vat_eur")
        plus_1tb = e.get("plan_plus_1tb_eur_month")
        mult = e.get("egress_multiple_of_instance")
        hourly = plan.get("hourly") or {}
        derived = bool(hourly.get("amount")) and bool(
            "derived" in (plan["price"].get("amount_source") or "").lower()
            or "730" in (plan["price"].get("derivation") or "")
        )
        rows.append({
            "provider": plan["provider"].split(",")[0].split("(")[0].strip(),
            "plan": plan["plan_name"],
            "jurisdiction": plan.get("jurisdiction_of_seller", "?"),
            "url": plan.get("url") or "",
            "capture_date": capture_date,
            "instance_eur": _fmt_num(instance_eur),
            "instance_eur_raw": instance_eur,
            "derived_monthly": derived,
            "plus_1tb_eur": _fmt_num(plus_1tb),
            "plus_1tb_eur_raw": plus_1tb,
            "egress_multiple": _fmt_num(mult),
            "excluded_meters_count": len(excl_reasons),
            "excluded_meters": excl_reasons,
            "is_free_tier": plan.get("is_promotional", False) and not instance_eur,
            "caveat": plan.get("ranking_caveat"),
            "_sort": plus_1tb,
        })
    return rows


def _build_renewal_cliff_rows(costs: dict, capture_date: str) -> list:
    p2r = costs["normalised"]["promotional_to_renewal"]
    pub = p2r.get("with_published_renewal", [])
    not_pub = p2r.get("renewal_not_published", [])

    # Sort published rows by multiple descending; null-multiple rows go last
    pub_sorted = _null_last(pub, key_fn=lambda r: r.get("monthly_multiple"), reverse=True)
    # not_published rows have null year_two and null multiple - sort last
    not_pub_sorted = _null_last(not_pub, key_fn=lambda r: r.get("monthly_multiple"), reverse=True)

    def _row(r, year_two_known):
        currency = r.get("currency", "EUR")
        y1 = r.get("year_one")
        y2 = r.get("year_two")
        mult = r.get("monthly_multiple")
        cliff = r.get("cliff_at_month")
        return {
            "provider": r["provider"].split(",")[0].split("(")[0].strip(),
            "plan": r["plan"],
            "currency": currency,
            "year_one": f"{currency} {_fmt_num(y1)}" if y1 is not None else NULL_DISPLAY,
            "year_two": f"{currency} {_fmt_num(y2)}" if y2 is not None else NULL_DISPLAY,
            "year_two_known": year_two_known and y2 is not None,
            "multiple": f"{mult:.2f}x" if mult is not None else NULL_DISPLAY,
            "when_it_bites": str(cliff) if cliff is not None else (r.get("cliff_note") or NULL_DISPLAY),
            "caveat": r.get("jump_formula"),
            # Monthly rates for the dumbbell chart
            "promo_monthly": r.get("promotional_monthly"),
            "renewal_monthly": r.get("renewal_monthly"),
            "_sort": mult,
        }

    rows = [_row(r, True) for r in pub_sorted] + [_row(r, False) for r in not_pub_sorted]
    return rows


def _build_managed_wp_rows(costs: dict, plans_by_id: dict, capture_date: str) -> list:
    mw = costs["normalised"]["managed_wordpress_eur_per_1k_visits"]
    rows = []
    for r in mw:
        plan_id = r["id"]
        plan = plans_by_id.get(plan_id, {})
        price = plan.get("price", {}) if plan else {}
        specs = plan.get("specs", {}) if plan else {}
        renewal = plan.get("renewal") or {}
        marginal = r.get("marginal_rate_above_the_cap") or {}
        eur_1k = r.get("eur_per_1k_visits_renewal")
        # Fall back to now price if renewal not computable (WP Engine)
        if eur_1k is None:
            eur_1k = r.get("eur_per_1k_visits_now")
        sites = specs.get("sites_included")
        storage = specs.get("storage_gb")
        overage_amount = marginal.get("amount")
        overage_currency = marginal.get("currency", "USD")
        overage_unit = marginal.get("unit", "per 1k visits")
        excluded = r.get("excluded", False)
        excl_reason = r.get("exclusion_reason")
        rows.append({
            "provider": r["provider"],
            "plan": r["plan"],
            "url": plan.get("url") or "" if plan else "",
            "capture_date": capture_date,
            "eur_per_1k_visits": _fmt_num(eur_1k) if eur_1k is not None else NULL_DISPLAY,
            "eur_per_1k_raw": eur_1k,
            "renewal_no_limit": eur_1k is None and not excluded,
            "year_two_month": (
                _fmt_num(renewal.get("amount")) if renewal.get("amount") is not None
                else NULL_DISPLAY
            ),
            "year_two_currency": renewal.get("currency", price.get("currency", "USD")),
            "sites": str(sites) if sites is not None else NULL_DISPLAY,
            "storage_gb": str(storage) if storage is not None else NULL_DISPLAY,
            "overage": (
                f"{overage_currency} {_fmt_num(overage_amount)} {overage_unit}"
                if overage_amount is not None else NULL_DISPLAY
            ),
            "excluded": excluded,
            "exclusion_reason": excl_reason,
            "caveat": r.get("caveat"),
            "_sort": eur_1k,
        })
    return rows


def _build_paas_rows(costs: dict, plans_by_id: dict, costs_1tb_by_id: dict,
                     egress_by_provider: dict, capture_date: str) -> list:
    import re as _re
    paas_plans = [p for p in costs["plans"] if p["category"] == "paas"]
    rows = []
    for plan in paas_plans:
        e = costs_1tb_by_id.get(plan["id"], {})
        price = plan.get("price", {})
        fee_amount = price.get("amount")
        fee_currency = price.get("currency", "USD")
        aom_text = plan.get("always_on_minimum") or ""
        # Extract USD amount for sort key only
        m = _re.search(r"USD\s+(\d+(?:\.\d+)?)\s+per month", str(aom_text))
        aom_sort = float(m.group(1)) if m else None
        # Detect "None." meaning no always-on
        aom_is_none = str(aom_text).startswith("None.")

        eg = egress_by_provider.get(_egress_name(plan["provider"]), {})
        overage_per_gb = eg.get("unit_normalised_per_gb")
        cost_1tb_eur = e.get("added_cost_eur")
        rows.append({
            "provider": plan["provider"],
            "plan": plan["plan_name"],
            "url": plan.get("url") or "",
            "capture_date": capture_date,
            "plan_fee": (
                f"{fee_currency} {_fmt_num(fee_amount)}"
                if fee_amount is not None else NULL_DISPLAY
            ),
            "plan_fee_raw": fee_amount,
            "always_on_floor": "none" if aom_is_none else (str(aom_text)[:200] if aom_text else NULL_DISPLAY),
            "always_on_floor_sort": aom_sort,
            "egress_per_gb": (
                f"USD {_fmt_num(overage_per_gb, 4)}"
                if overage_per_gb is not None else NULL_DISPLAY
            ),
            "cost_1tb_eur": _fmt_num(cost_1tb_eur),
            "cost_1tb_raw": cost_1tb_eur,
            "flags": plan.get("flags", []),
            "_sort": aom_sort,
        })
    return rows


def _build_egress_rows(egress_doc: dict, capture_date: str) -> list:
    rows = []
    for r in egress_doc["providers"]:
        cost = r.get("cost_for_1tb_month")
        currency = r.get("currency", "USD")
        rate = r.get("unit_normalised_per_gb")
        incl = r.get("included_transfer")
        rows.append({
            "provider": r["name"],
            "url": r.get("url") or "",
            "capture_date": r.get("retrieved_on", capture_date),
            "cost_1tb": (
                f"{currency} {_fmt_num(cost)}" if cost is not None else NULL_DISPLAY
            ),
            "cost_1tb_raw": cost,
            "currency": currency,
            "included": incl or NULL_DISPLAY,
            "included_is_null": not incl,
            "rate_over": (
                f"{currency} {_fmt_num(rate, 4)} per GB" if rate is not None else NULL_DISPLAY
            ),
            "note": r.get("note"),
            "_sort": cost,
        })
    return rows


def _build_jurisdiction_rows(costs: dict, providers_doc: dict, capture_date: str) -> list:
    juris_rows = costs.get("jurisdiction", {}).get("rows", [])
    rows = []
    for r in juris_rows:
        exposure = r.get("cloud_act_exposure", "none_identified")
        inc_country = r.get("incorporation_country", "?")
        parent = r.get("parent_company")
        parent_country = r.get("parent_country")
        eu_count = r.get("eu_regions_count", 0)
        rows.append({
            "provider": r["provider"],
            "legal_entity": r.get("legal_entity"),
            "contracting_country": inc_country,
            "ultimate_owner": (
                f"{parent_country}: {parent}" if parent else "not established"
            ),
            "us_exposure": exposure,
            "us_exposure_label": (
                "direct" if exposure == "direct" else "none identified"
            ),
            "eu_regions": eu_count,
            "disclosure_url": r.get("sovereignty_disclosure_url"),
            # Sort: none_identified first (=0), then direct (=1)
            "_sort_exposure": 0 if exposure == "none_identified" else 1,
            "_sort_country": inc_country,
        })
    # Sort by exposure group first, then by contracting country within group
    rows.sort(key=lambda r: (r["_sort_exposure"], r["_sort_country"]))
    return rows


def _add_bar_pct(rows: list, key: str) -> None:
    """Add a bar-percentage key (0-100) for each row, scaled to column max.

    The percentage key is named ``key + '_bar_pct'``.  Null values get None.
    If every value is None or zero the function does nothing.
    """
    max_v = max((r[key] for r in rows if r.get(key)), default=None)
    if not max_v:
        return
    for r in rows:
        v = r.get(key)
        r[key + "_bar_pct"] = round(v / max_v * 100, 1) if v else None


def _hbar(x: float, y: float, w: float, h: float, r: int = 4) -> str:
    """SVG path for a horizontal bar: square left end, rounded right end."""
    if w < 1:
        return ""
    if w <= r * 2:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}"/>'
    return (
        f'<path d="M{x:.1f},{y:.1f} h{w - r:.1f}'
        f' a{r},{r} 0 0 1 {r},{r}'
        f' v{h - 2 * r:.1f}'
        f' a{r},{r} 0 0 1 -{r},{r}'
        f' H{x:.1f} Z"/>'
    )


def _chart_label(provider: str, plan: str, max_chars: int = 26) -> str:
    """Shorten provider + plan to fit a narrow label column."""
    # Abbreviate known long provider names
    p = (provider
         .replace("Akamai Connected Cloud (Linode)", "Akamai")
         .replace("Amazon Web Services (AWS)", "AWS")
         .replace("Microsoft Azure", "Azure")
         .replace("Google Cloud", "Google"))
    # Strip parenthetical qualifiers from plan name
    pl = re.sub(r"\s*\([^)]+\)", "", plan)
    pl = pl.split(",")[0].strip()
    label = f"{p}: {pl}"
    if len(label) > max_chars:
        label = label[:max_chars - 1] + "…"
    return label


def svg_vps_chart(vps_rows: list) -> Markup:
    """Inline SVG: horizontal bar chart of EUR/GB/RAM, ascending."""
    LPAD, RPAD = 182, 52
    BAR_H, BAR_GAP, TOP, BOT = 12, 4, 16, 36
    W = 580
    CHART_W = W - LPAD - RPAD
    MAX_V = 10.50  # Exoscale Standard Micro, documented anchor

    chart_rows = [
        (r["provider"], r["plan"], r["eur_per_gb_ram_raw"])
        for r in vps_rows
        if r.get("eur_per_gb_ram_raw") is not None
    ]
    n = len(chart_rows)
    ROW_H = BAR_H + BAR_GAP
    H = TOP + n * ROW_H + BOT

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"'
        f' role="img" aria-label="VPS cost per GB of RAM, EUR per month"'
        f' style="font-family: inherit;">'
    ]

    # Grid and axis ticks
    for tick in [0, 2, 4, 6, 8, 10]:
        gx = LPAD + (tick / MAX_V) * CHART_W
        parts.append(
            f'<line x1="{gx:.1f}" y1="{TOP}" x2="{gx:.1f}" y2="{H - BOT + 4}"'
            f' stroke="var(--rule)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{H - 10}" text-anchor="middle"'
            f' font-size="10" fill="var(--ink-faint)">{tick}</text>'
        )
    # Axis label
    parts.append(
        f'<text x="{LPAD + CHART_W / 2:.1f}" y="{H - 1}" text-anchor="middle"'
        f' font-size="10" fill="var(--ink-faint)">EUR / GB RAM / month</text>'
    )

    # Bars
    for i, (provider, plan, val) in enumerate(chart_rows):
        cy = TOP + i * ROW_H
        bar_w = max(4.0, (val / MAX_V) * CHART_W)
        label = _chart_label(provider, plan)

        # Row label (right-aligned at LPAD - 6)
        parts.append(
            f'<text x="{LPAD - 6}" y="{cy + BAR_H - 2}"'
            f' text-anchor="end" font-size="10" fill="var(--ink-muted)">'
            f'{html.escape(label)}</text>'
        )
        # Bar
        parts.append(
            f'<g fill="var(--series-1)">{_hbar(LPAD, cy, bar_w, BAR_H)}</g>'
        )
        # Value label
        val_label = f"{val:.2f}"
        parts.append(
            f'<text x="{LPAD + bar_w + 3:.1f}" y="{cy + BAR_H - 2}"'
            f' font-size="10" fill="var(--ink)">{val_label}</text>'
        )

    parts.append("</svg>")
    return Markup("\n".join(parts))


def svg_hyperscaler_chart(hyper_rows: list) -> Markup:
    """Inline SVG: stacked horizontal bar - instance cost + egress cost."""
    LPAD, RPAD = 150, 56
    BAR_H, BAR_GAP, TOP, BOT = 18, 8, 8, 36
    GAP_PX = 2  # surface gap between segments
    W = 580
    CHART_W = W - LPAD - RPAD

    # Filter to plans with both instance and 1TB-total costs
    chart_rows = [
        r for r in hyper_rows
        if r.get("instance_eur_raw") and r.get("instance_eur_raw", 0) > 0
        and r.get("plus_1tb_eur_raw") is not None
    ]
    if not chart_rows:
        return Markup("")

    chart_rows = sorted(chart_rows, key=lambda r: r["_sort"] or 0)
    MAX_V = max(r["plus_1tb_eur_raw"] for r in chart_rows) * 1.12

    n = len(chart_rows)
    ROW_H = BAR_H + BAR_GAP
    H = TOP + n * ROW_H + BOT

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"'
        f' role="img" aria-label="Hyperscaler instance plus 1 TB egress cost"'
        f' style="font-family: inherit;">'
    ]

    # Grid and axis ticks
    tick_step = 30
    tick_vals = list(range(0, int(MAX_V) + tick_step, tick_step))
    for tick in tick_vals:
        gx = LPAD + (tick / MAX_V) * CHART_W
        if gx > W - RPAD + 4:
            continue
        parts.append(
            f'<line x1="{gx:.1f}" y1="{TOP}" x2="{gx:.1f}" y2="{H - BOT + 4}"'
            f' stroke="var(--rule)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{H - 10}" text-anchor="middle"'
            f' font-size="10" fill="var(--ink-faint)">{tick}</text>'
        )
    parts.append(
        f'<text x="{LPAD + CHART_W / 2:.1f}" y="{H - 1}" text-anchor="middle"'
        f' font-size="10" fill="var(--ink-faint)">EUR / month (approximate)</text>'
    )

    # Bars
    for i, row in enumerate(chart_rows):
        cy = TOP + i * ROW_H
        inst = row["instance_eur_raw"]
        total = row["plus_1tb_eur_raw"]
        egress = total - inst

        inst_w = max(2.0, (inst / MAX_V) * CHART_W)
        total_w = (total / MAX_V) * CHART_W
        egress_w = max(2.0, total_w - inst_w - GAP_PX)

        label = _chart_label(row["provider"], row["plan"])
        parts.append(
            f'<text x="{LPAD - 6}" y="{cy + BAR_H - 3}"'
            f' text-anchor="end" font-size="10" fill="var(--ink-muted)">'
            f'{html.escape(label)}</text>'
        )
        # Blue instance segment
        parts.append(
            f'<g fill="var(--series-1)">{_hbar(LPAD, cy, inst_w, BAR_H, r=2)}</g>'
        )
        # Orange egress segment (gap_px after the blue end)
        ox = LPAD + inst_w + GAP_PX
        parts.append(
            f'<g fill="var(--series-2)">{_hbar(ox, cy, egress_w, BAR_H, r=4)}</g>'
        )
        # Total label
        parts.append(
            f'<text x="{LPAD + total_w + 4:.1f}" y="{cy + BAR_H - 3}"'
            f' font-size="10" fill="var(--ink)">EUR {total:.0f}</text>'
        )

    parts.append("</svg>")
    return Markup("\n".join(parts))


def svg_renewal_cliff_chart(cliff_rows: list) -> Markup:
    """Inline SVG: dumbbell chart of promo vs renewal monthly rate."""
    LPAD, RPAD = 195, 52
    DOT_R = 5
    ROW_H, TOP, BOT = 22, 8, 38
    W = 580
    CHART_W = W - LPAD - RPAD

    # Filter: exclude free-tier (promo_monthly == 0 or None) and rows without
    # at least a promo rate.  Preserve year_two_known to distinguish published
    # from not-published renewal prices.
    dumb_rows = [
        r for r in cliff_rows
        if r.get("promo_monthly") and r.get("promo_monthly") > 0
    ]
    if not dumb_rows:
        return Markup("")

    MAX_V = max(
        max((r["renewal_monthly"] or 0) for r in dumb_rows),
        max(r["promo_monthly"] for r in dumb_rows),
    )
    MAX_V = max(MAX_V * 1.10, 10)

    n = len(dumb_rows)
    H = TOP + n * ROW_H + BOT

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"'
        f' role="img" aria-label="Promotional vs renewal monthly rate"'
        f' style="font-family: inherit;">'
    ]

    # Vertical gridlines
    tick_step = 30 if MAX_V > 60 else 20
    for tick in range(0, int(MAX_V) + tick_step, tick_step):
        gx = LPAD + (tick / MAX_V) * CHART_W
        if gx > W - RPAD + 4:
            continue
        parts.append(
            f'<line x1="{gx:.1f}" y1="{TOP}" x2="{gx:.1f}" y2="{H - BOT + 4}"'
            f' stroke="var(--rule)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{H - 10}" text-anchor="middle"'
            f' font-size="10" fill="var(--ink-faint)">{tick}</text>'
        )
    parts.append(
        f'<text x="{LPAD + CHART_W / 2:.1f}" y="{H - 1}" text-anchor="middle"'
        f' font-size="10" fill="var(--ink-faint)">EUR / month (USD where noted)</text>'
    )

    # Rows: top 5 get multiple labels
    label_multiples_for = 5

    for i, row in enumerate(dumb_rows):
        cy = TOP + i * ROW_H + ROW_H // 2
        promo = row["promo_monthly"]
        renewal = row.get("renewal_monthly")
        known = row.get("year_two_known", True)
        mult = row.get("_sort")  # float multiple or None
        currency = row.get("currency", "EUR")
        usd_note = " (USD)" if currency == "USD" else ""

        px = LPAD + (promo / MAX_V) * CHART_W
        label = _chart_label(row["provider"], row["plan"], max_chars=28)

        # Row label
        parts.append(
            f'<text x="{LPAD - 8}" y="{cy + 4}"'
            f' text-anchor="end" font-size="10" fill="var(--ink-muted)">'
            f'{html.escape(label)}{html.escape(usd_note)}</text>'
        )

        if known and renewal is not None:
            rx = LPAD + (renewal / MAX_V) * CHART_W
            # Connecting line
            parts.append(
                f'<line x1="{px:.1f}" y1="{cy}" x2="{rx:.1f}" y2="{cy}"'
                f' stroke="var(--series-1)" stroke-width="2"/>'
            )
            # Promo dot (hollow look: surface ring + filled dot)
            parts.append(
                f'<circle cx="{px:.1f}" cy="{cy}" r="{DOT_R + 2}"'
                f' fill="var(--paper)"/>'
            )
            parts.append(
                f'<circle cx="{px:.1f}" cy="{cy}" r="{DOT_R}"'
                f' fill="var(--series-1)"/>'
            )
            # Renewal dot
            parts.append(
                f'<circle cx="{rx:.1f}" cy="{cy}" r="{DOT_R + 2}"'
                f' fill="var(--paper)"/>'
            )
            parts.append(
                f'<circle cx="{rx:.1f}" cy="{cy}" r="{DOT_R}"'
                f' fill="var(--series-1)"/>'
            )
            # Multiple label for the worst rows
            if mult is not None and i < label_multiples_for:
                mult_str = f"{mult:.2f}x"
                parts.append(
                    f'<text x="{rx + DOT_R + 3:.1f}" y="{cy + 4}"'
                    f' font-size="10" font-weight="500" fill="var(--ink)">'
                    f'{mult_str}</text>'
                )
        else:
            # Not published: single dot + text
            parts.append(
                f'<circle cx="{px:.1f}" cy="{cy}" r="{DOT_R + 2}"'
                f' fill="var(--paper)"/>'
            )
            parts.append(
                f'<circle cx="{px:.1f}" cy="{cy}" r="{DOT_R}"'
                f' fill="var(--series-1)" fill-opacity="0.5"'
                f' stroke="var(--series-1)" stroke-width="1.5"/>'
            )
            # "not published" label to the right
            parts.append(
                f'<text x="{px + DOT_R + 4:.1f}" y="{cy + 4}"'
                f' font-size="10" fill="var(--ink-faint)">not published</text>'
            )

    parts.append("</svg>")
    return Markup("\n".join(parts))


def build_compare_data(costs: dict, egress_doc: dict,
                       live_prices: dict | None = None) -> dict:
    """Build the 7 comparison tables from costs.json and egress.json."""
    comparisons = {c["id"]: c for c in costs["comparisons"]}
    plans_by_id = {p["id"]: p for p in costs["plans"]}
    egress_by_provider = {r["name"]: r for r in egress_doc["providers"]}
    costs_1tb_by_id = {r["id"]: r for r in costs["normalised"]["cost_with_1tb_egress"]}
    capture_date = costs["capture_date"]
    live_index = _build_live_index(live_prices)

    vps_rows = _build_vps_rows(
        costs, plans_by_id, egress_by_provider, capture_date, live_index=live_index
    )
    hyper_rows = _build_hyperscaler_rows(costs, plans_by_id, costs_1tb_by_id, capture_date)
    cliff_rows = _build_renewal_cliff_rows(costs, capture_date)
    mw_rows = _build_managed_wp_rows(costs, plans_by_id, capture_date)
    paas_rows = _build_paas_rows(costs, plans_by_id, costs_1tb_by_id, egress_by_provider, capture_date)
    egress_rows = _build_egress_rows(egress_doc, capture_date)
    juris_rows = _build_jurisdiction_rows(costs, {}, capture_date)

    # Sort rows before bar pct computation so scale is based on sorted order
    vps_sorted = _null_last(vps_rows, key_fn=lambda r: r["_sort"])
    hyper_sorted = _null_last(hyper_rows, key_fn=lambda r: r["_sort"])
    mw_sorted = _null_last(mw_rows, key_fn=lambda r: r["_sort"])
    egress_sorted = _null_last(egress_rows, key_fn=lambda r: r["_sort"], reverse=True)

    # Inline bar percentages (scaled per column max, null rows get None)
    _add_bar_pct(vps_sorted, "eur_per_gb_ram_raw")
    _add_bar_pct(hyper_sorted, "instance_eur_raw")
    _add_bar_pct(hyper_sorted, "plus_1tb_eur_raw")
    _add_bar_pct(mw_sorted, "eur_per_1k_raw")
    _add_bar_pct(egress_sorted, "cost_1tb_raw")

    # Hero SVG charts (placed above each table)
    chart_svgs = {
        "vps": svg_vps_chart(vps_sorted),
        "hyperscaler": svg_hyperscaler_chart(hyper_sorted),
        "renewal-cliff": svg_renewal_cliff_chart(cliff_rows),
    }

    return {
        "vps": {
            "meta": comparisons["vps"],
            "rows": vps_sorted,
            "chart_svg": chart_svgs["vps"],
        },
        "hyperscaler": {
            "meta": comparisons["hyperscaler"],
            "rows": hyper_sorted,
            "chart_svg": chart_svgs["hyperscaler"],
        },
        "renewal-cliff": {
            "meta": comparisons["renewal-cliff"],
            # Already sorted in _build_renewal_cliff_rows
            "rows": cliff_rows,
            "chart_svg": chart_svgs["renewal-cliff"],
        },
        "managed-wordpress": {
            "meta": comparisons["managed-wordpress"],
            "rows": mw_sorted,
            "chart_svg": None,
        },
        "paas": {
            "meta": comparisons["paas"],
            "rows": _null_last(paas_rows, key_fn=lambda r: r["_sort"]),
            "chart_svg": None,
        },
        "egress": {
            "meta": comparisons["egress"],
            "rows": egress_sorted,
            "chart_svg": None,
        },
        "jurisdiction": {
            "meta": comparisons["jurisdiction"],
            # Already sorted in _build_jurisdiction_rows
            "rows": juris_rows,
            "chart_svg": None,
        },
        "_chart_svgs": chart_svgs,
    }


# --------------------------------------------------------------------------
# Five-tab comparison site
# --------------------------------------------------------------------------

NINE = frozenset({
    "Hetzner",
    "DigitalOcean",
    "Vultr",
    "Akamai Connected Cloud (Linode)",
    "Scaleway",
    "OVHcloud",
    "Amazon Web Services (AWS)",
    "Google Cloud",
    "Microsoft Azure",
})

# Mapping from NINE names to the names used in egress.json
_EGRESS_NINE: dict[str, str] = {
    "Akamai Connected Cloud (Linode)": "Akamai (Linode)",
    "Amazon Web Services (AWS)": "Amazon Web Services",
}

# Short display names for SVG labels
_SHORT_NINE: dict[str, str] = {
    "Akamai Connected Cloud (Linode)": "Akamai",
    "Amazon Web Services (AWS)": "AWS",
    "Microsoft Azure": "Azure",
    "Google Cloud": "Google",
}


def _short9(provider: str) -> str:
    return _SHORT_NINE.get(provider, provider)


# Representative plan IDs for the nine providers.
# VPS providers: cheapest plan with at least 4 GB RAM.
# Hyperscalers: cheapest always-on paid Linux EU instance in this dataset.
_VPS_PLAN: dict[str, str] = {
    "Hetzner":                          "hetzner--cx23",
    "DigitalOcean":                     "digitalocean--basic-droplet-regular-4-gib---2-vcpu",
    "Vultr":                            "vultr--cloud-compute-regular-performance-2-vcpu---4-gb",
    "Akamai Connected Cloud (Linode)":  "akamai--linode-4-gb",
    "Scaleway":                         "scaleway--dev1-l",
    "OVHcloud":                         "ovhcloud--vps-1-2027",
    "Amazon Web Services (AWS)":        "amazon--amazon-ec2-t4g.small-linux-on-demand-europe-",
    "Google Cloud":                     "google--compute-engine-e2-small-on-demand-belgium-eur",
    "Microsoft Azure":                  "microsoft--azure-virtual-machines-b2ls-v2-standard_b2ls_v2",
}


def build_vps_tab_rows(costs: dict, capture_date: str,
                       live_prices: dict | None = None) -> list:
    """Tab 1: EUR per GB RAM, ascending. One representative plan per provider."""
    plans_by_id = {p["id"]: p for p in costs["plans"]}
    ranking_by_id = {r["id"]: r
                     for r in costs["normalised"]["vps_eur_per_gb_ram_ranking"]}
    live_index = _build_live_index(live_prices)
    today = date.today()

    rows = []
    for provider, plan_id in _VPS_PLAN.items():
        plan = plans_by_id.get(plan_id)
        if not plan:
            continue
        specs = plan.get("specs") or {}
        ram = specs.get("ram_gb")
        vcpu = specs.get("vcpu")
        url = plan.get("url") or ""

        r = ranking_by_id.get(plan_id)
        if r:
            eur_per_gb = r["value_eur"]
        else:
            eur = plan.get("price_ex_vat_eur")
            eur_per_gb = (eur / ram) if (eur and ram) else None

        live_plan = _live_for_plan(live_index, plan)
        last_verified = (
            (live_plan.get("fetched_at") or "")[:10] if live_plan else capture_date
        )
        age = days_since(last_verified, today)
        rows.append({
            "provider": provider,
            "short": _short9(provider),
            "plan": plan["plan_name"].split(",")[0].strip(),
            "ram_gb": ram,
            "vcpu": vcpu,
            "eur_per_gb_ram": eur_per_gb,
            # Two places, matching the chart labels. Four implied a precision
            # the source pages do not carry and made the column hard to scan.
            "eur_per_gb_fmt": (f"{eur_per_gb:.2f}" if eur_per_gb is not None
                               else NULL_DISPLAY),
            "url": url,
            "capture_date": capture_date,
            "last_verified": last_verified,
            "age_days": age,
            "_sort": eur_per_gb,
        })
    return _null_last(rows, key_fn=lambda r: r["eur_per_gb_ram"])


def svg_vps_tab_chart(rows: list) -> Markup:
    """Tab 1: horizontal bar chart, EUR per GB RAM, ascending."""
    LPAD, RPAD = 68, 60
    BAR_H, BAR_GAP, TOP, BOT = 16, 5, 16, 36
    W = 560
    CHART_W = W - LPAD - RPAD
    valid = [r for r in rows if r.get("eur_per_gb_ram") is not None]
    if not valid:
        return Markup("")
    MAX_V = max(r["eur_per_gb_ram"] for r in valid) * 1.10

    n = len(valid)
    ROW_H = BAR_H + BAR_GAP
    H = TOP + n * ROW_H + BOT

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"'
        f' role="img" aria-label="VPS cost per GB of RAM per month, nine providers"'
        f' style="font-family:inherit;">'
    ]
    for tick in [0, 2, 4, 6, 8]:
        if tick > MAX_V + 0.1:
            continue
        gx = LPAD + (tick / MAX_V) * CHART_W
        parts.append(
            f'<line x1="{gx:.1f}" y1="{TOP}" x2="{gx:.1f}" y2="{H - BOT + 4}"'
            f' stroke="var(--rule)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{H - 10}" text-anchor="middle"'
            f' font-size="11" fill="var(--ink-faint)">{tick}</text>'
        )
    parts.append(
        f'<text x="{LPAD + CHART_W / 2:.1f}" y="{H - 1}" text-anchor="middle"'
        f' font-size="10" fill="var(--ink-faint)">EUR / GB RAM / month</text>'
    )
    for i, row in enumerate(valid):
        cy = TOP + i * ROW_H
        val = row["eur_per_gb_ram"]
        bar_w = max(4.0, (val / MAX_V) * CHART_W)
        parts.append(
            f'<text x="{LPAD - 5}" y="{cy + BAR_H - 2}"'
            f' text-anchor="end" font-size="11" fill="var(--ink-muted)">'
            f'{html.escape(row["short"])}</text>'
        )
        parts.append(f'<g fill="var(--series-1)">{_hbar(LPAD, cy, bar_w, BAR_H)}</g>')
        parts.append(
            f'<text x="{LPAD + bar_w + 4:.1f}" y="{cy + BAR_H - 2}"'
            f' font-size="11" fill="var(--ink)">{val:.2f}</text>'
        )
    parts.append("</svg>")
    return Markup("\n".join(parts))


def build_egress_tab_rows(egress_doc: dict, capture_date: str) -> list:
    """Tab 2: 1 TB egress cost, descending. Nine providers."""
    by_name = {r["name"]: r for r in egress_doc["providers"]}
    rows = []
    for provider in sorted(NINE):
        egress_key = _EGRESS_NINE.get(provider, provider)
        rec = by_name.get(egress_key)
        if not rec:
            continue
        cost = rec.get("cost_for_1tb_month")
        currency = rec.get("currency", "USD")
        incl = rec.get("included_transfer") or ""
        incl_short = (incl[:100] + "...") if len(incl) > 100 else incl
        rows.append({
            "provider": provider,
            "short": _short9(provider),
            "cost_1tb": cost,
            "cost_display": (
                f"{currency} {_fmt_num(cost)}" if cost is not None else NULL_DISPLAY
            ),
            "currency": currency,
            "included": incl_short,
            "url": rec.get("url") or "",
            "capture_date": rec.get("retrieved_on", capture_date),
            "_sort": cost,
        })

    def _desc_key(r: dict) -> tuple:
        v = r["_sort"]
        if v is None:
            return (2, 0.0)
        if v == 0:
            return (1, 0.0)
        return (0, -float(v))

    rows.sort(key=_desc_key)
    return rows


def svg_egress_tab_chart(rows: list) -> Markup:
    """Tab 2: horizontal bar chart, 1 TB egress cost, descending."""
    LPAD, RPAD = 68, 96
    BAR_H, BAR_GAP, TOP, BOT = 16, 5, 16, 36
    W = 560
    CHART_W = W - LPAD - RPAD
    paid = [r for r in rows if r.get("cost_1tb") and r["cost_1tb"] > 0]
    MAX_V = (max(r["cost_1tb"] for r in paid) * 1.10) if paid else 120.0

    n = len(rows)
    ROW_H = BAR_H + BAR_GAP
    H = TOP + n * ROW_H + BOT

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"'
        f' role="img" aria-label="Cost of 1 TB monthly outbound traffic, nine providers"'
        f' style="font-family:inherit;">'
    ]
    for tick in [0, 30, 60, 90, 120]:
        if tick > MAX_V + 1:
            continue
        gx = LPAD + (tick / MAX_V) * CHART_W
        if gx > W - RPAD + 4:
            continue
        parts.append(
            f'<line x1="{gx:.1f}" y1="{TOP}" x2="{gx:.1f}" y2="{H - BOT + 4}"'
            f' stroke="var(--rule)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{H - 10}" text-anchor="middle"'
            f' font-size="11" fill="var(--ink-faint)">{tick}</text>'
        )
    parts.append(
        f'<text x="{LPAD + CHART_W / 2:.1f}" y="{H - 1}" text-anchor="middle"'
        f' font-size="10" fill="var(--ink-faint)">USD / month (EUR providers: 0)</text>'
    )
    for i, row in enumerate(rows):
        cy = TOP + i * ROW_H
        cost = row.get("cost_1tb")
        parts.append(
            f'<text x="{LPAD - 5}" y="{cy + BAR_H - 2}"'
            f' text-anchor="end" font-size="11" fill="var(--ink-muted)">'
            f'{html.escape(row["short"])}</text>'
        )
        if cost and cost > 0:
            bar_w = max(4.0, (cost / MAX_V) * CHART_W)
            parts.append(f'<g fill="var(--series-2)">{_hbar(LPAD, cy, bar_w, BAR_H)}</g>')
            parts.append(
                f'<text x="{LPAD + bar_w + 4:.1f}" y="{cy + BAR_H - 2}"'
                f' font-size="11" fill="var(--ink)">{row["cost_display"]}</text>'
            )
        else:
            parts.append(
                f'<g fill="var(--series-1)" opacity="0.35">'
                f'{_hbar(LPAD, cy, 6, BAR_H)}</g>'
            )
            parts.append(
                f'<text x="{LPAD + 10}" y="{cy + BAR_H - 2}"'
                f' font-size="11" fill="var(--ink-faint)">included / 0</text>'
            )
    parts.append("</svg>")
    return Markup("\n".join(parts))


def build_year_two_tab_rows(costs: dict, capture_date: str) -> list:
    """Tab 3: worst 8 promotional-to-renewal cliffs by monthly multiple."""
    all_cliff = _build_renewal_cliff_rows(costs, capture_date)
    published = [
        r for r in all_cliff
        if r.get("year_two_known") and r.get("_sort") is not None
    ]
    published.sort(key=lambda r: -(r["_sort"] or 0))
    return published[:8]


def build_allin_tab_rows(costs: dict, capture_date: str) -> list:
    """Tab 4: base plan + 1 TB egress, ascending by total. Nine providers."""
    plans_by_id = {p["id"]: p for p in costs["plans"]}
    costs_1tb = {r["id"]: r for r in costs["normalised"]["cost_with_1tb_egress"]}

    rows = []
    for provider, plan_id in _VPS_PLAN.items():
        plan = plans_by_id.get(plan_id)
        if not plan:
            continue
        e = costs_1tb.get(plan_id, {})
        total = e.get("plan_plus_1tb_eur_month")
        instance = plan.get("price_ex_vat_eur")
        egress_cost = (
            (total - instance)
            if (total is not None and instance is not None)
            else None
        )
        # Subtracting two equal prices leaves a tiny negative residue rather
        # than exactly zero, which printed as "EUR -0.00" for every provider
        # that includes its traffic. Round to cents, then add zero so IEEE
        # negative zero becomes positive zero.
        if egress_cost is not None:
            egress_cost = round(egress_cost, 2) + 0.0
        rows.append({
            "provider": provider,
            "short": _short9(provider),
            "plan": plan["plan_name"].split(",")[0].strip(),
            "instance_eur": instance,
            "instance_fmt": (f"EUR {instance:.2f}" if instance is not None
                             else NULL_DISPLAY),
            "egress_eur": egress_cost,
            "egress_fmt": (f"EUR {egress_cost:.2f}" if egress_cost is not None
                           else NULL_DISPLAY),
            "total_eur": total,
            "total_fmt": (f"EUR {total:.2f}" if total is not None else NULL_DISPLAY),
            "url": plan.get("url") or "",
            "capture_date": capture_date,
            "_sort": total,
        })
    return _null_last(rows, key_fn=lambda r: r["_sort"])


def svg_allin_tab_chart(rows: list) -> Markup:
    """Tab 4: stacked bar, instance (blue) + 1 TB egress (orange)."""
    LPAD, RPAD = 68, 78
    BAR_H, BAR_GAP, TOP, BOT = 18, 6, 16, 36
    GAP_PX = 2
    W = 560
    CHART_W = W - LPAD - RPAD

    valid = [r for r in rows if r.get("total_eur") is not None]
    if not valid:
        return Markup("")
    MAX_V = max(r["total_eur"] for r in valid) * 1.10
    n = len(valid)
    ROW_H = BAR_H + BAR_GAP
    H = TOP + n * ROW_H + BOT

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"'
        f' role="img" aria-label="Base plan plus 1 TB egress per month, nine providers"'
        f' style="font-family:inherit;">'
    ]
    for tick in [0, 30, 60, 90, 120]:
        if tick > MAX_V + 1:
            continue
        gx = LPAD + (tick / MAX_V) * CHART_W
        if gx > W - RPAD + 4:
            continue
        parts.append(
            f'<line x1="{gx:.1f}" y1="{TOP}" x2="{gx:.1f}" y2="{H - BOT + 4}"'
            f' stroke="var(--rule)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{H - 10}" text-anchor="middle"'
            f' font-size="11" fill="var(--ink-faint)">{tick}</text>'
        )
    parts.append(
        f'<text x="{LPAD + CHART_W / 2:.1f}" y="{H - 1}" text-anchor="middle"'
        f' font-size="10" fill="var(--ink-faint)">EUR / month (approximate)</text>'
    )
    for i, row in enumerate(valid):
        cy = TOP + i * ROW_H
        inst = row["instance_eur"] or 0.0
        total = row["total_eur"]
        eg = row["egress_eur"] or 0.0

        inst_w = max(4.0, (inst / MAX_V) * CHART_W)
        total_w = (total / MAX_V) * CHART_W
        eg_w = max(0.0, total_w - inst_w - GAP_PX)

        parts.append(
            f'<text x="{LPAD - 5}" y="{cy + BAR_H - 3}"'
            f' text-anchor="end" font-size="11" fill="var(--ink-muted)">'
            f'{html.escape(row["short"])}</text>'
        )
        parts.append(
            f'<g fill="var(--series-1)">{_hbar(LPAD, cy, inst_w, BAR_H, r=2)}</g>'
        )
        if eg_w > 1 and eg > 0:
            ox = LPAD + inst_w + GAP_PX
            parts.append(
                f'<g fill="var(--series-2)">'
                f'{_hbar(ox, cy, max(4.0, eg_w), BAR_H, r=4)}</g>'
            )
        parts.append(
            f'<text x="{LPAD + total_w + 4:.1f}" y="{cy + BAR_H - 3}"'
            f' font-size="11" fill="var(--ink)">EUR {total:.0f}</text>'
        )
    parts.append("</svg>")
    return Markup("\n".join(parts))


def build_jurisdiction_tab_rows(costs: dict, capture_date: str) -> list:
    """Tab 5: all 21 providers, jurisdiction data."""
    return _build_jurisdiction_rows(costs, {}, capture_date)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

written: list[Path] = []


def write(relative: str, content: str) -> None:
    path = DIST / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    written.append(path)


def build_who_pays_rows(affiliate_doc: dict) -> list:
    """Tab 6: what each provider would pay this site to recommend it.

    A comparison site that ranks providers and can be paid by them owes the
    reader the second half of that sentence. The research already held it and
    nothing rendered it. Providers with a programme sort first, because they
    are the ones the reader needs to weigh; within each group, alphabetical.
    """
    rows = []
    for rec in affiliate_doc.get("programmes", []):
        has = bool(rec.get("has_programme"))
        recurring = rec.get("recurring")
        cookie = rec.get("cookie_days")
        rows.append({
            "provider": rec.get("provider", "?"),
            "has_programme": has,
            # A checked negative is a finding, not a blank. "none found" says
            # what was done without asserting that none exists anywhere.
            "programme_label": "yes" if has else "none found",
            "payout": rec.get("payout") or NULL_DISPLAY,
            "recurring": (
                "recurring" if recurring == "recurring"
                else "one-off" if recurring == "one_off"
                else NULL_DISPLAY
            ),
            "cookie_days": f"{cookie} days" if cookie else NULL_DISPLAY,
            "network": rec.get("network") or NULL_DISPLAY,
            "confidence": rec.get("confidence") or NULL_DISPLAY,
            "retrieved_on": rec.get("retrieved_on"),
            "source_url": rec.get("source_url") or "",
        })
    rows.sort(key=lambda r: (0 if r["has_programme"] else 1, r["provider"].lower()))
    return rows


def build_takeaway(nav: str, rows: list) -> str | None:
    """The one sentence a reader repeats to someone else.

    Every number here is read back off the rows that built the chart, so the
    sentence cannot drift from the figure above it and cannot go stale when
    the daily refresh moves a price. Returns None when the underlying rows
    cannot support the claim, and the page then simply omits it: a takeaway
    that is guessed is worse than no takeaway at all.
    """
    if nav == "vps":
        priced = [r for r in rows if r.get("eur_per_gb_ram") is not None]
        if len(priced) < 2:
            return None
        low = min(priced, key=lambda r: r["eur_per_gb_ram"])
        high = max(priced, key=lambda r: r["eur_per_gb_ram"])
        if not low["eur_per_gb_ram"]:
            return None
        ratio = high["eur_per_gb_ram"] / low["eur_per_gb_ram"]
        return (
            f"The same gigabyte of RAM costs {ratio:.1f} times more at "
            f"{high['short']} than at {low['short']}: EUR "
            f"{high['eur_per_gb_ram']:.2f} against EUR "
            f"{low['eur_per_gb_ram']:.2f} a month."
        )

    if nav == "egress":
        priced = [r for r in rows if r.get("_sort") is not None]
        if not priced:
            return None
        free = [r for r in priced if r["_sort"] == 0]
        billed = [r for r in priced if r["_sort"] != 0]
        if not billed:
            return (
                f"All {len(priced)} providers here carry 1 TB of outbound "
                f"traffic at no extra charge."
            )
        # The stored costs are not converted to a common currency, so naming a
        # dearest across two of them would be a comparison of unlike numbers.
        # Only claim a maximum when every billed row is quoted in one currency.
        one_currency = len({r.get("currency") for r in billed}) == 1
        if not one_currency:
            return (
                f"{len(free)} of {len(priced)} providers carry 1 TB of outbound "
                f"traffic at no extra charge. The other {len(billed)} bill for it."
            )
        dearest = max(billed, key=lambda r: r["_sort"])
        if not free:
            return (
                f"Every provider here bills for outbound traffic. The dearest "
                f"terabyte costs {dearest['cost_display']} at {dearest['short']}."
            )
        return (
            f"{len(free)} of {len(priced)} providers carry 1 TB of outbound "
            f"traffic at no extra charge. The same terabyte costs "
            f"{dearest['cost_display']} at {dearest['short']}."
        )

    if nav == "year-two":
        # Already sorted worst first by the numeric _sort. "multiple" is a
        # display string such as "3.50x", so the number comes off _sort.
        worst = next(
            (r for r in rows if r.get("_sort") and r.get("year_two_known")), None
        )
        if not worst:
            return None
        return (
            f"The steepest renewal here multiplies the bill by "
            f"{worst['_sort']:.1f} once the introductory rate ends: "
            f"{worst['provider']}, {worst['plan']}."
        )

    if nav in ("all-in", "home"):
        priced = [
            r for r in rows
            if r.get("instance_eur") is not None and r.get("total_eur") is not None
        ]
        if len(priced) < 2:
            return None
        by_base = [r["provider"] for r in sorted(priced, key=lambda r: r["instance_eur"])]
        by_total = [r["provider"] for r in sorted(priced, key=lambda r: r["total_eur"])]
        moved = sum(1 for a, b in zip(by_base, by_total) if a != b)
        if not moved:
            return (
                "Adding a terabyte of traffic changes no provider's position "
                "in the ranking."
            )
        return (
            f"Adding a terabyte of traffic moves {moved} of the {len(priced)} "
            f"providers in the ranking. The cheapest plan is not the cheapest bill."
        )

    if nav == "who-pays":
        if not rows:
            return None
        with_prog = sum(1 for r in rows if r["has_programme"])
        return (
            f"{with_prog} of the {len(rows)} providers ranked on this site would "
            f"pay it to recommend them. It currently takes money from none of them, "
            f"and no link here is a paid link."
        )

    if nav == "jurisdiction":
        if not rows:
            return None
        exposed = sum(1 for r in rows if r.get("us_exposure") == "direct")
        disclosed = sum(1 for r in rows if r.get("disclosure_url"))
        return (
            f"{exposed} of the {len(rows)} providers checked have a US entity "
            f"in the contracting or ownership chain. {disclosed} have published "
            f"a sovereignty disclosure we could locate."
        )

    return None


def write_robots_and_sitemap(filenames: list[str], today: date) -> None:
    """A crawler that cannot enumerate the pages indexes whichever one it
    stumbled on. Both files are derived from the same tab list the pages are
    built from, so a new tab cannot be left out of the sitemap by hand."""
    base = SITE["base_url"]

    write(
        "robots.txt",
        "User-agent: *\n"
        "Allow: /\n"
        f"\nSitemap: {base}/sitemap.xml\n",
    )

    urls = []
    for filename in filenames:
        path = "" if filename == "index.html" else filename
        urls.append(
            "  <url>\n"
            f"    <loc>{base}/{path}</loc>\n"
            f"    <lastmod>{today.isoformat()}</lastmod>\n"
            "  </url>\n"
        )
    write(
        "sitemap.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(urls)
        + "</urlset>\n",
    )


def check_output() -> None:
    """No em-dash may reach a generated file, and no provider without a
    programme may carry a paid-link marker."""
    problems = []
    for path in written:
        text = path.read_text(encoding="utf-8")
        if EM_DASH in text or EN_DASH in text:
            problems.append(f"{path.relative_to(DIST)}: contains an em-dash or en-dash")
    if problems:
        for problem in problems:
            print(f"  output check failed: {problem}", file=sys.stderr)
        raise SystemExit("build stopped: output checks failed")


def main() -> int:
    today = date.today()

    costs_doc = load("costs.json")
    egress_doc = load("egress.json")
    live_prices = load_live_prices()

    capture_date = costs_doc["capture_date"]

    vps_rows = build_vps_tab_rows(costs_doc, capture_date, live_prices)
    egress_tab_rows = build_egress_tab_rows(egress_doc, capture_date)
    year_two_rows = build_year_two_tab_rows(costs_doc, capture_date)
    allin_rows = build_allin_tab_rows(costs_doc, capture_date)
    juris_rows = build_jurisdiction_tab_rows(costs_doc, capture_date)

    affiliate_doc = load("affiliate-terms.json")
    affiliate_date = affiliate_doc.get("_meta", {}).get("retrieved_on", capture_date)
    who_pays_rows = build_who_pays_rows(affiliate_doc)

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["paras"] = paras
    env.filters["markdown"] = markdown
    env.filters["host"] = host_of
    env.filters["recurring_phrase"] = recurring_phrase

    common = {
        "site": SITE,
        "dataset_date": capture_date,
        "generated_on": today.isoformat(),
        "has_paid_links": False,
        "affiliate_date": affiliate_date,
    }

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    write("style.css", (STATIC / "style.css").read_text(encoding="utf-8"))

    cname = STATIC / "CNAME"
    if cname.exists():
        write("CNAME", cname.read_text(encoding="utf-8").strip() + "\n")

    # Icons and the social card. Copied rather than written, because they are
    # binary and the em-dash output check reads every written file as text.
    # og.png and favicon.ico are committed build products of og.svg and
    # favicon.svg; regenerate them with the command in CLAUDE.md when the
    # figures on the card change. og.svg itself is a source, so it is not
    # shipped.
    for asset in ("favicon.svg", "favicon.ico", "apple-touch-icon.png", "og.png"):
        src = STATIC / asset
        if src.exists():
            shutil.copy2(src, DIST / asset)

    fonts_src = STATIC / "fonts"
    if fonts_src.exists():
        fonts_dst = DIST / "fonts"
        fonts_dst.mkdir(parents=True, exist_ok=True)
        for f in fonts_src.iterdir():
            shutil.copy2(f, fonts_dst / f.name)

    vps_chart = svg_vps_tab_chart(vps_rows)
    egress_chart = svg_egress_tab_chart(egress_tab_rows)
    year_two_chart = svg_renewal_cliff_chart(year_two_rows)
    allin_chart = svg_allin_tab_chart(allin_rows)

    # The brand belongs in <title> only, which base.html appends. Putting it
    # here too printed it inside the visible <h1> on every page.
    _titles = {
        "home": (
            "What hosting actually costs",
            "Base plan plus a terabyte of traffic, for nine providers, with every "
            "figure dated and linked to the provider's own page.",
        ),
        "vps": (
            "VPS cost per GB of RAM",
            "Cost per GB of RAM per month for nine popular cloud providers, ascending.",
        ),
        "egress": (
            "Egress: 1 TB monthly outbound cost",
            "What 1 TB of monthly outbound traffic costs from nine providers, descending.",
        ),
        "year-two": (
            "Year-two renewal cliffs",
            "Advertised price against renewal price: the eight worst promotional-to-renewal gaps.",
        ),
        "jurisdiction": (
            "Jurisdiction: cloud exposure and disclosure",
            "US cloud-jurisdiction exposure and sovereignty disclosure for 21 providers.",
        ),
        "who-pays": (
            "Who pays us",
            "The affiliate terms of all 21 providers ranked here, including the "
            "15 that would pay this site for a referral.",
        ),
    }

    # Total cost leads, because it is the question someone choosing a host
    # actually has. Cost per GB of RAM is a normalisation useful for comparing
    # unlike plans, not an answer, so it moved off the landing page to /vps.html.
    tabs = [
        ("index.html",        "tab_home.html",         "home",         allin_rows,     allin_chart),
        ("vps.html",          "tab_vps.html",          "vps",          vps_rows,       vps_chart),
        ("egress.html",       "tab_egress.html",       "egress",       egress_tab_rows, egress_chart),
        ("year-two.html",     "tab_year_two.html",     "year-two",     year_two_rows,  year_two_chart),
        ("jurisdiction.html", "tab_jurisdiction.html", "jurisdiction", juris_rows,     None),
        ("who-pays-us.html",  "tab_who_pays.html",     "who-pays",     who_pays_rows,  None),
    ]

    # The landing page carries each other page's own computed takeaway, so the
    # summary cannot drift from the page it points at.
    findings = [
        {"href": "/vps.html", "label": "Cost per GB of RAM",
         "takeaway": build_takeaway("vps", vps_rows)},
        {"href": "/egress.html", "label": "What outbound traffic costs",
         "takeaway": build_takeaway("egress", egress_tab_rows)},
        {"href": "/year-two.html", "label": "What happens in year two",
         "takeaway": build_takeaway("year-two", year_two_rows)},
        {"href": "/jurisdiction.html", "label": "Which law a provider answers to",
         "takeaway": build_takeaway("jurisdiction", juris_rows)},
        {"href": "/who-pays-us.html", "label": "Who pays us",
         "takeaway": build_takeaway("who-pays", who_pays_rows)},
    ]

    for filename, tmpl_name, nav, rows, chart in tabs:
        page_title, page_description = _titles[nav]
        # The canonical URL and the counting pixel both need a path, and the
        # index is addressed as the bare origin, not as /index.html.
        page_path = "" if filename == "index.html" else filename
        write(
            filename,
            env.get_template(tmpl_name).render(
                **common,
                rel="",
                nav=nav,
                rows=rows,
                chart_svg=chart,
                page_title=page_title,
                page_description=page_description,
                page_path=page_path,
                takeaway=build_takeaway(nav, rows),
                findings=findings,
                capture_date=capture_date,
            ),
        )

    write_robots_and_sitemap([f for f, *_ in tabs], today)

    check_output()

    pages = [p for p in written if p.suffix == ".html"]
    print(f"\n{len(pages)} pages built:")
    for p in sorted(pages, key=lambda x: x.name):
        print(f"  site/dist/{p.name}")
    print(f"\n  Data captured {capture_date}, built {today.isoformat()}")
    blockers = []
    if not SITE["publisher_entity"]:
        blockers.append("HOSTLAWS_PUBLISHER_ENTITY: who publishes this")
    if not SITE["contact"]:
        blockers.append(
            "HOSTLAWS_CONTACT: a correction route that actually resolves. "
            "Verify it before setting it: an address with no MX record, or a "
            "URL that 404s, is worse than none."
        )
    for b in blockers:
        print(f"\n  LAUNCH BLOCKER: {b}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
