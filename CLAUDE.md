# hostlaws

Static site: what hosting actually costs. Five pages, one chart each, built by
`site/build.py` from JSON in `research/`.

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
