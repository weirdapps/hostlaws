# hostlaws.com

What hosting actually costs, as opposed to what it is advertised at.

A static site comparing hosting providers on the numbers that decide a real bill:
cost per unit of RAM, what outbound traffic costs once you exceed the included
allowance, and what the price becomes in year two when the promotional rate ends.

Five pages:

| Page | Question it answers |
|---|---|
| Total cost (landing) | What is the actual monthly bill: base plan plus a terabyte of traffic |
| Cost per GB | What does a gigabyte of RAM cost per month, across nine providers |
| Egress | What does 1 TB of monthly outbound traffic cost, from zero to EUR 149 |
| Year two | How much does the bill multiply when the introductory rate ends |
| Jurisdiction | Which legal system can compel each provider, and who has published a statement about it |

## How the numbers are sourced

Every figure carries the date it was captured and links to the provider's own page.

Prices for Akamai, Vultr, Scaleway and Microsoft Azure are refreshed daily from
those providers' public pricing APIs. Everything else is a dated manual capture.
Each figure is marked with which it is and how old it is, so a stale number is
visible rather than silently wrong. A failed refresh keeps the previous value and
marks it stale; it never blanks a figure.

Where a value could not be established it says so. A blank is never rendered as a
zero, and a provider whose figures we could not obtain never ranks first by virtue
of being undocumented.

## Running it

```
uv sync
uv run python site/build.py        # writes site/dist
uv run python pricing/fetch_live.py # refreshes research/live-prices.json
```

Set `HOSTLAWS_GOATCOUNTER` to emit the counting markup; leave it unset and the
build produces no analytics at all, which is what you want locally.

The build needs Python 3.12 and Jinja2. No JavaScript, no framework, no build
toolchain. The site is complete and readable with JavaScript disabled.

## Layout

| Path | Contents |
|---|---|
| `site/build.py` | The generator |
| `site/templates/` | Jinja2 templates |
| `site/static/` | Stylesheet and self-hosted fonts |
| `research/` | The data the site is built from |
| `pricing/fetch_live.py` | Daily refresh from provider pricing APIs |

## Corrections

Figures change and pages move. If something here is wrong, open an issue with the
provider, the figure, and a link to the page that contradicts it.

## Licence

Code is MIT. The compiled data in `research/` is CC-BY-4.0: use it with
attribution to hostlaws.com. Underlying prices and terms belong to the providers
and are cited to their own pages throughout.
