# hostlaws

Static site: what hosting actually costs. Six pages built by `site/build.py`
from JSON in `research/`. The landing page is total cost, base plan plus a
terabyte of traffic, because that is the question someone choosing a host
actually has. Cost per GB of RAM is a normalisation for comparing unlike
plans, not an answer, so it lives at `/vps.html`.

**Two widths, and only two.** `--measure` (40rem) for anything read as prose,
`--measure-data` (64rem) for charts and tables. A chart and the table beneath
it must share a right edge. Before this rule there were five different edges on
one page. `.page p/ul/ol/h2/h3` default to `--measure` so an unclassed
paragraph cannot silently run to full width; do not add a third value.

Build: `uv run python site/build.py` (writes `site/dist`). Jinja2 and the standard
library only. No JavaScript dependency: the site must be complete with JS off.

## Rules

- Every figure carries its capture date and links to the provider's own page.
- A null renders its recorded reason. Never a blank in a ranked column, never a
  zero-length bar, never a dash that could read as free, and it sorts last.
- Derived figures are labelled derived. Hourly-to-monthly conversions state the
  multiplier used.
- Observations, never verdicts, about named companies. "No statement found at the
  URLs we checked on this date", not "does not comply".
- No em-dashes anywhere, including generated HTML.
- Live prices come from `research/live-prices.json`. A failed refresh keeps the
  previous value and marks it stale; it never blanks a figure.
- The daily price commit must NOT carry `[skip ci]`. That push is what triggers
  `deploy.yml`, and suppressing it publishes nothing: the data refreshes in the
  repository while the served pages stay frozen at the last human push.
- Per-page takeaway sentences are computed in `build_takeaway`, never written by
  hand, so the sentence cannot contradict the chart above it. It returns None
  rather than guess, and it refuses a superlative across two currencies.
- `og.png`, `favicon.ico` and `apple-touch-icon.png` are committed build products
  of `site/static/og.svg` and `site/static/favicon.svg`. The card carries a
  capture date, so regenerate it when the figures on it change:

  ```
  cd site/static
  rsvg-convert -w 1200 -h 630 og.svg -o og.png
  rsvg-convert -w 180 -h 180 favicon.svg -o apple-touch-icon.png
  rsvg-convert -w 32 -h 32 favicon.svg -o /tmp/f32.png
  rsvg-convert -w 16 -h 16 favicon.svg -o /tmp/f16.png
  magick /tmp/f16.png /tmp/f32.png favicon.ico
  ```

  These tools are not a build dependency. The build only copies the committed
  binaries, so CI needs nothing installed.
- Analytics is GoatCounter, enabled only when `HOSTLAWS_GOATCOUNTER` is set, so
  a local build never counts its own author. The script tag is pinned to a
  versioned URL with an SRI hash; the unversioned `count.js` changes in place
  and cannot be hashed. The `<noscript>` pixel is the point, not an afterthought:
  this site is read by people who block scripts.
- Plan matching between live API data and the captured rows is deliberately
  conservative. Do not loosen it to raise the live count: a live price joined onto
  the wrong row is worse than an honest snapshot.

## Layout

| Path | Contents |
|---|---|
| `site/build.py` | Generator |
| `site/templates/` | Jinja2 templates |
| `site/static/` | Stylesheet, self-hosted fonts, CNAME |
| `research/` | Source data |
| `pricing/fetch_live.py` | Daily refresh from provider pricing APIs |
