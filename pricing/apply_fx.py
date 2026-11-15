"""Move every USD-derived EUR figure in research/costs.json to a new USD to EUR rate.

The dataset converts USD at one indicative rate, fx.rate, read from the
currencyRates object on https://kinsta.com/pricing/. The weekly price review
reads that object and runs this script, so the EUR figures follow the rate
every Sunday.

What moves:
- every field whose name contains "eur" in a row priced in USD (or unpriced,
  like the enterprise rows whose egress is in USD), and the EUR figures in
  that row's text;
- the USD egress inside EUR rows (Azure) and USD overage rates in EUR rows
  (WP Engine);
- the copies of those figures under `normalised`, re-sorted;
- fx itself, with the outgoing rate kept in fx.previous.

Each EUR figure is first scaled by new/old, then the ones a USD amount sits
next to are recomputed exactly from that amount. The script then re-checks
every "USD a x rate = EUR b" and "EUR c / n unit = EUR d" formula in the file
and refuses to write if any is off.

Usage: uv run python pricing/apply_fx.py RATE [--date YYYY-MM-DD] [--source TEXT]
Exit code: 0 written or nothing to do, 1 validation failed (file untouched),
2 bad input.
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

COSTS = Path(__file__).resolve().parent.parent / "research" / "costs.json"
NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
SKIP_KEYS = {"monthly_multiple", "year_two_over_year_one"}
NATIVE_EUR_ROWS = {"microsoft--azure-free-account-usd-200-credit"}  # EUR renewal, not converted
RANKINGS = (
    ("vps_eur_per_gb_ram_ranking", "eur_per_gb_ram_month"),
    ("vps_and_hyperscaler_eur_per_gb_ram_ranking", "eur_per_gb_ram_month"),
    ("vps_eur_per_vcpu_ranking", "eur_per_vcpu_month"),
    ("vps_and_hyperscaler_eur_per_vcpu_ranking", "eur_per_vcpu_month"),
)


def _places(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def _num(text: str) -> float:
    return float(text.replace(",", ""))


class Rescaler:
    """Scales EUR figures, in numbers and in text, by new / old."""

    def __init__(self, old: float, new: float, old_inv: str, new_inv: str):
        self.ratio = new / old
        self.old_s, self.new_s = repr(old), repr(new)
        self.old_inv, self.new_inv = old_inv, new_inv

    def number(self, v):
        if isinstance(v, bool) or not isinstance(v, int | float) or v == 0:
            return v
        return round(v * self.ratio + 1e-12, max(2, _places(repr(float(v)))))

    def token(self, tok: str) -> str:
        places = _places(tok.replace(",", ""))
        val = round(_num(tok) * self.ratio + 1e-12, places)
        return f"{val:,.{places}f}" if "," in tok else f"{val:.{places}f}"

    def text(self, s: str) -> str:
        s = re.sub(rf"(?<![\d.]){re.escape(self.old_s)}(?!\d)", "\x00R\x00", s)
        s = re.sub(rf"(?<![\d.]){re.escape(self.old_inv)}(?!\d)", "\x00I\x00", s)
        s = re.sub(rf"(EUR\s?)({NUM})", lambda m: m.group(1) + self.token(m.group(2)), s)
        s = re.sub(rf"(?<![\d.,])({NUM})(\s?EUR)", lambda m: self.token(m.group(1)) + m.group(2), s)
        return s.replace("\x00R\x00", self.new_s).replace("\x00I\x00", self.new_inv)

    def obj(self, o: Any, key: str = "") -> Any:
        if isinstance(o, dict):
            return {k: self.obj(v, k) for k, v in o.items()}
        if isinstance(o, list):
            return [self.obj(v, key) for v in o]
        if isinstance(o, str):
            return self.text(o)
        if re.search(r"eur", key, re.I) and key not in SKIP_KEYS:
            return self.number(o)
        return o


def _usd_rows(doc: dict) -> set[str]:
    return {
        p["id"] for p in doc["plans"]
        if (p.get("price") or {}).get("currency") in ("USD", None)
        and p["id"] not in NATIVE_EUR_ROWS
    }


def _rescale_normalised(node, rs: Rescaler, ids: set[str], providers: set[str]):
    def owned(v):
        return isinstance(v, dict) and (
            v.get("id") in ids or ("id" not in v and v.get("provider") in providers))

    items = node.items() if isinstance(node, dict) else enumerate(node)
    for k, v in items:
        if owned(v):
            node[k] = rs.obj(v)
        elif isinstance(v, dict | list):
            _rescale_normalised(v, rs, ids, providers)


def _exact_usd_row(plan: dict, rate: float) -> None:
    """Recompute from the USD amount the figures that sit next to one."""
    price, renewal = plan.get("price") or {}, plan.get("renewal") or {}
    if price.get("currency") != "USD":
        return
    if isinstance(price.get("amount"), int | float) and plan.get("price_ex_vat_eur") is not None:
        plan["price_ex_vat_eur"] = round(price["amount"] * rate + 1e-12, 4)
    if isinstance(renewal.get("amount"), int | float) and "ex_vat_eur" in renewal \
            and renewal.get("currency", "USD") == "USD":
        renewal["ex_vat_eur"] = round(renewal["amount"] * rate + 1e-12, 4)
    specs = plan.get("specs") or {}
    for block_name in ("normalised_on_renewal", "normalised_on_current_price"):
        block = plan.get(block_name) or {}
        base = _align_basis(block, _basis_ref(plan, block_name))
        _exact_units(block, base, specs)
        for mr in block.get("machine_rates") or []:
            _exact_machine_rate(mr, rate)


def _basis_ref(plan: dict, block_name: str):
    renewal_eur = (plan.get("renewal") or {}).get("ex_vat_eur")
    if block_name == "normalised_on_renewal" and isinstance(renewal_eur, int | float):
        return renewal_eur
    return plan.get("price_ex_vat_eur")


def _align_basis(block: dict, ref):
    """Snap the block's basis to the exact EUR price when it is that price, rounded."""
    base = block.get("basis_price_eur_month")
    both = isinstance(base, int | float) and isinstance(ref, int | float)
    if both and abs(base - ref) < 0.01:
        block["basis_price_eur_month"] = base = ref
    return base


def _exact_machine_rate(mr: dict, rate: float) -> None:
    if "usd_per_month" not in mr or not mr.get("ram_gb"):
        return
    eur = round(mr["usd_per_month"] * rate + 1e-12, 4)
    per = round(eur / mr["ram_gb"] + 1e-12, 4)
    mr.update(eur_per_month=eur, eur_per_gb_ram_month=per, eur_per_gb_ram_formula=(
        f"USD {mr['usd_per_month']} x {rate!r} = EUR {eur:.4f}; "
        f"/ {mr['ram_gb']} GB = EUR {per:.4f} per GB per month"))


def _exact_units(block: dict, base, specs: dict) -> None:
    if not isinstance(base, int | float):
        return
    for key, div, unit, per in (("eur_per_gb_ram_month", "ram_gb", "GB RAM", "GB"),
                                ("eur_per_vcpu_month", "vcpu", "vCPU", "vCPU")):
        if isinstance(block.get(key), int | float) and specs.get(div):
            value = round(base / specs[div] + 1e-12, 4)
            block[key] = value
            formula = key.replace("_month", "_formula")
            if isinstance(block.get(formula), str) and " EUR / " in block[formula]:
                block[formula] = (f"{base:.4f} EUR / {specs[div]} {unit} = "
                                  f"{value:.4f} EUR per {per} per month")


PAIRS = (  # (USD key, EUR key, decimals) on the same object
    ("amount_usd", "amount_eur", 2),
    ("plan_plus_1tb_usd_month", "plan_plus_1tb_eur_month", 2),
    ("usd_per_month", "eur_per_month", 4),
)


def _fmt(val: float, like: str) -> str:
    places = _places(like.replace(",", ""))
    val = round(val + 1e-12, places)
    return f"{val:,.{places}f}" if "," in like else f"{val:.{places}f}"


def _exact_text(s: str, rate: float) -> str:
    """Recompute EUR figures that the text derives from a USD amount beside them."""
    s = re.sub(rf"(USD ({NUM}) x {re.escape(repr(rate))} = EUR )({NUM})",
               lambda m: m.group(1) + _fmt(_num(m.group(2)) * rate, m.group(3)), s)
    return re.sub(rf"(USD ({NUM})((?:(?!USD|EUR).){{0,40}}?)indicatively EUR )({NUM})",
                  lambda m: m.group(1) + _fmt(_num(m.group(2)) * rate, m.group(4)), s)


def _exact_unit_rates(out: dict, rate: float) -> None:
    mem, cpu = out.get("memory_usd_per_gb_month"), out.get("cpu_usd_per_vcpu_month")
    if not (isinstance(mem, int | float) and isinstance(cpu, int | float)):
        return
    out["memory_eur_per_gb_month"] = m = round(mem * rate + 1e-12, 2)
    out["cpu_eur_per_vcpu_month"] = c = round(cpu * rate + 1e-12, 2)
    if isinstance(out.get("formula"), str):
        out["formula"] = re.sub(
            rf"At {re.escape(repr(rate))} that is EUR {NUM} per GB and EUR {NUM} per vCPU",
            f"At {rate!r} that is EUR {m:.2f} per GB and EUR {c:.2f} per vCPU", out["formula"])


def _exact_instance_rate(out: dict, rate: float) -> None:
    usd = out.get("usd_per_month")
    if not isinstance(usd, int | float) or "eur_per_month" in out:
        return
    eur = usd * rate
    for key, div in (("eur_per_gb_ram_month", "ram_gb"), ("eur_per_vcpu_month", "vcpu")):
        if isinstance(out.get(key), int | float) and out.get(div):
            out[key] = round(eur / out[div] + 1e-12, 4)
    if isinstance(out.get("formula"), str) and out.get("ram_gb"):
        out["formula"] = re.sub(
            rf"USD ({NUM}) x {re.escape(repr(rate))} / ({NUM}) GB = EUR {NUM}",
            lambda m: (f"USD {m.group(1)} x {rate!r} / {m.group(2)} GB = EUR "
                       f"{_num(m.group(1)) * rate / _num(m.group(2)):.4f}"), out["formula"])


def _exact_visits(plan: dict, rate: float) -> None:
    """Cost per 1,000 visits, from the exact EUR price of a USD row."""
    visits = plan.get("per_1k_visits") or {}
    stated = visits.get("monthly_visits_stated")
    if not stated:
        return
    thousands = stated / 1000
    for key, formula, base in (
            ("eur_per_1k_visits_now", "formula_now", plan.get("price_ex_vat_eur")),
            ("eur_per_1k_visits_renewal", "formula_renewal",
             (plan.get("renewal") or {}).get("ex_vat_eur"))):
        if not (isinstance(visits.get(key), int | float) and isinstance(base, int | float)):
            continue
        visits[key] = value = round(base / thousands + 1e-12, 4)
        if isinstance(visits.get(formula), str):
            visits[formula] = re.sub(
                rf"{NUM} EUR (per month|renewal) / \(([\d,]+) visits / 1,000\) = {NUM} EUR",
                lambda m, b=base, v=value: (f"{b:.4f} EUR {m.group(1)} / ({m.group(2)} visits "
                                            f"/ 1,000) = {v:.4f} EUR"), visits[formula])
    cap = visits.get("marginal_rate_above_the_cap") or {}
    now = visits.get("eur_per_1k_visits_now")
    if cap.get("currency") == "USD" and isinstance(now, int | float) and now:
        cap["eur"] = round(cap["amount"] * rate + 1e-12, 4)
        cap["comparison_formula"] = (
            f"Overage EUR {cap['eur']:.4f} per 1,000 visits / average included "
            f"EUR {now:.4f} per 1,000 visits = {cap['eur'] / now:.2f}x")


def _exact_conversions(node, rate: float):
    """Make every EUR figure that sits next to its USD amount exact at this rate."""
    if isinstance(node, dict):
        out = {k: _exact_conversions(v, rate) for k, v in node.items()}
        for usd, eur, places in PAIRS:
            if isinstance(out.get(usd), int | float) and isinstance(out.get(eur), int | float):
                out[eur] = round(out[usd] * rate + 1e-12, places)
        if out.get("currency") == "USD" and isinstance(out.get("amount"), int | float) \
                and isinstance(out.get("amount_eur"), int | float):
            out["amount_eur"] = round(out["amount"] * rate + 1e-12, 2)
        _exact_unit_rates(out, rate)
        _exact_instance_rate(out, rate)
        added = out.get("added_cost")
        if isinstance(added, dict) and added.get("currency") == "USD" \
                and isinstance(out.get("added_cost_eur"), int | float):
            out["added_cost_eur"] = round(added["amount"] * rate + 1e-12, 2)
        return out
    if isinstance(node, list):
        return [_exact_conversions(v, rate) for v in node]
    if isinstance(node, str):
        return _exact_text(node, rate)
    return node


def _usd_parts_of_eur_rows(doc: dict, rate: float) -> None:
    for plan in doc["plans"]:
        if (plan.get("price") or {}).get("currency") != "EUR":
            continue
        egress = plan.get("with_1tb_egress") or {}
        added = egress.get("added_cost") or {}
        inst = plan["price"].get("amount")
        usd_egress = added.get("currency") == "USD" and added.get("amount")
        if usd_egress and isinstance(inst, int | float) \
                and egress.get("plan_plus_1tb_eur_month") is not None:
            eg = round(added["amount"] * rate, 2)
            tot = round(inst + eg, 2)
            egress.update(
                added_cost_eur=eg, plan_plus_1tb_eur_month=tot,
                plan_plus_1tb_formula=(f"EUR {inst:.2f} instance + EUR {eg:.2f} egress "
                                       f"= EUR {tot:.2f} per month"),
                egress_multiple_of_instance=round(eg / inst, 2),
                egress_multiple_formula=(f"EUR {eg:.2f} egress / EUR {inst:.2f} instance "
                                         f"= {eg / inst:.2f}x"),
                egress_share_of_total_pct=round(100 * eg / tot, 1))
        visits = plan.get("per_1k_visits") or {}
        cap = visits.get("marginal_rate_above_the_cap") or {}
        if cap.get("currency") == "USD" and visits.get("eur_per_1k_visits_now"):
            cap["eur"] = round(cap["amount"] * rate, 4)
            now = visits["eur_per_1k_visits_now"]
            cap["comparison_formula"] = (
                f"Overage EUR {cap['eur']:.4f} per 1,000 visits / average included "
                f"EUR {now:.4f} per 1,000 visits = {cap['eur'] / now:.2f}x")


def _refresh_rankings(n: dict, plans: dict) -> None:
    for name, metric in RANKINGS:
        for entry in n[name]:
            renewal = plans[entry["id"]]["normalised_on_renewal"]
            entry["value_eur"] = renewal[metric]
            entry["basis_price_eur_month"] = renewal["basis_price_eur_month"]
        n[name].sort(key=lambda e: (e["value_eur"] is None, e["value_eur"] or 0))
    for entry in n["vps_cheapest_per_provider_eur_per_gb_ram"]:
        rows = [r for r in n["vps_eur_per_gb_ram_ranking"]
                if r["provider"] == entry["provider"] and r["value_eur"] is not None]
        if rows:
            best = min(rows, key=lambda r: r["value_eur"])
            entry["cheapest_plan"], entry["eur_per_gb_ram_month"] = best["plan"], best["value_eur"]


def _copy_keys(entries: list, source, keys: tuple) -> None:
    for entry in entries:
        src = source(entry["id"])
        for k in keys:
            if k in entry and k in src:
                entry[k] = src[k]


def _refresh_normalised(doc: dict) -> None:
    n, plans = doc["normalised"], {p["id"]: p for p in doc["plans"]}
    _refresh_rankings(n, plans)
    _copy_keys(n["cost_with_1tb_egress"], lambda i: plans[i].get("with_1tb_egress") or {},
               ("added_cost_eur", "added_cost_eur_range", "plan_plus_1tb_eur_month",
                "egress_share_of_total_pct", "egress_multiple_of_instance",
                "arithmetic", "caveat"))
    _copy_keys(n["managed_wordpress_eur_per_1k_visits"],
               lambda i: plans[i].get("per_1k_visits") or {},
               ("eur_per_1k_visits_now", "formula_now", "eur_per_1k_visits_renewal",
                "formula_renewal", "marginal_rate_above_the_cap"))
    n["managed_wordpress_eur_per_1k_visits"].sort(
        key=lambda e: (e["eur_per_1k_visits_now"] is None, e["eur_per_1k_visits_now"] or 0))


def _strings(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k != "price_updates":
                yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def validate(doc: dict) -> list[str]:
    """Every conversion and per-unit formula in the file must match its own arithmetic."""
    rate = doc["fx"]["rate"]
    errors = []
    conv = re.compile(rf"USD ({NUM}) x {re.escape(repr(rate))} = EUR ({NUM})")
    div = re.compile(rf"({NUM}) EUR / ({NUM}) (?:GB RAM|vCPU) = ({NUM}) EUR")
    for s in _strings(doc):
        for m in conv.finditer(s):
            tolerance = 0.6 * 10 ** -_places(m.group(2)) + 1e-9
            if abs(_num(m.group(1)) * rate - _num(m.group(2))) > tolerance:
                errors.append(m.group(0))
        for m in div.finditer(s):
            if abs(_num(m.group(1)) / _num(m.group(2)) - _num(m.group(3))) > 0.00015:
                errors.append(m.group(0))
    return errors


def apply(doc: dict, new: float, day: str, source: str) -> dict:
    fx = doc["fx"]
    old = fx["rate"]
    new_inv = f"{1 / new:.6f}"
    rs = Rescaler(old, new, f"{fx['inverse_eur_to_usd']:.6f}", new_inv)
    ids = _usd_rows(doc)
    providers = {p["provider"] for p in doc["plans"] if p["id"] in ids}
    doc["plans"] = [rs.obj(p) if p["id"] in ids else p for p in doc["plans"]]
    _rescale_normalised(doc["normalised"], rs, ids, providers)
    for plan in doc["plans"]:
        if plan["id"] in ids:
            _exact_usd_row(plan, new)
    _usd_parts_of_eur_rows(doc, new)
    doc["plans"] = _exact_conversions(doc["plans"], new)
    for plan in doc["plans"]:
        if plan["id"] in ids:
            _exact_visits(plan, new)
    doc["normalised"] = _exact_conversions(doc["normalised"], new)
    _refresh_normalised(doc)
    fx["previous"] = {"rate": old, "captured_on": fx["captured_on"],
                      "inverse_eur_to_usd": fx["inverse_eur_to_usd"]}
    fx.update(rate=new, inverse_eur_to_usd=float(new_inv), captured_on=day, source=source)
    fx["status"] = rs.text(fx["status"])
    n = doc["normalised"]
    n["method"]["currency_axis"] = n["method"]["currency_axis"].replace(repr(old), repr(new))
    for comparison in doc["comparisons"]:
        for col in comparison.get("columns", []):
            if isinstance(col.get("note"), str):
                col["note"] = col["note"].replace(repr(old), repr(new))
    doc.setdefault("price_updates", []).append({"reviewed_on": day, "changes": [{
        "id": "fx", "field": "fx.rate", "old": old, "new": new, "currency": "USD to EUR",
        "why": "Weekly FX refresh from the dataset's source; "
               "every USD-derived EUR figure recomputed."}]})
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("rate", type=float, help="new USD to EUR rate, for example 0.90715")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--source", default=(
        "The currencyRates object in the inline JavaScript config of https://kinsta.com/pricing/, "
        "read at the weekly price review."))
    args = parser.parse_args()
    if not 0.5 < args.rate < 1.5:
        print(f"refusing implausible rate {args.rate}", file=sys.stderr)
        return 2
    doc = json.loads(COSTS.read_text())
    if doc["fx"]["rate"] == args.rate:
        print(f"fx already {args.rate}, nothing to do")
        return 0
    doc = apply(doc, args.rate, args.date, args.source)
    errors = validate(doc)
    if errors:
        print("validation failed, costs.json untouched:", *errors[:20], sep="\n  ", file=sys.stderr)
        return 1
    COSTS.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    print(f"fx {doc['fx']['previous']['rate']} -> {args.rate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
