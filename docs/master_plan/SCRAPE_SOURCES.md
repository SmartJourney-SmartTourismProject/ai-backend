# Scrape sources — vetting record

Every automated source the data pipeline reads from a website (not an API) is
recorded here with the evidence it was checked against, **before** a connector
was written for it. Vetted 2026-09-30. Re-check a source's robots.txt and terms
whenever its connector starts returning 0 rows (see `pipeline.py`'s scraper
breakage warning) — a layout change is often accompanied by a policy change.

## Accepted

### friday.lk — events (`web_events_friday` connector, weekly)
| Check | Finding |
|---|---|
| robots.txt | `User-Agent: * / Allow: /`; disallows only `/admin/`, `/api/`, `/login/`, submit/profile pages, `?q=`/`?search=` and non-English locales. The paths we read — `/events`, `/events/<city>`, `/events/<uuid>` — are allowed. |
| Terms (`/terms`) | No clause on automated access. Content is copyright My Smart Need (Pvt) Ltd → we store **facts only** (name, dates, venue, price), a truncated description, and always the `source_url` back to the event page. |
| Structure | Server-rendered (Next.js). `/events` links to each city page; each city page carries a schema.org **`ItemList`** JSON-LD of upcoming events; each event page carries a schema.org **`Event`** JSON-LD (`startDate`/`endDate` ISO-8601 UTC, `location.address` with `addressLocality`/`addressCountry: LK`, `offers.price`, `keywords`). Parsed from JSON-LD, never CSS classes (the MUI class names are generated and change on every build). |
| Stable id | Event URL is `/events/<uuid>` → the uuid is `external_ref`. |
| Data quirk | `offers.price` can be malformed — seen `"60008000.00"` for what reads as a 6,000–8,000 range. Prices above `MAX_PLAUSIBLE_TICKET_LKR` are stored as unknown, never trusted. |
| Politeness | ~20 city pages + one page per event, 2 s apart, weekly. |

### Central Cultural Fund — heritage-site entry fees (`entry_fees_ccf` connector, quarterly)
| Check | Finding |
|---|---|
| robots.txt | WordPress default: disallows `/wp-admin/` only. |
| Source | `https://ccf.gov.lk/is/index.php` — the price table iframed into the official "Ticket Issuance" page. Government body that sets the fees. |
| Structure | One plain HTML `<table>`: Site · Full Ticket (USD) · Full Ticket (LKR) · Half Ticket (USD) · Half Ticket (LKR). Prices include 18% VAT. **Foreign-visitor prices only** — no local rate is published, so `local_adult` stays NULL. |
| Data quirks | Group heading rows with no price ("Galle") followed by "- Shipwreck diving tour …" sub-rows — sub-rows are activities, not sites, and are skipped. Typos in site names ("lbbankatuwa") — matching to listings is fuzzy and admin-reviewed. |

### Wikivoyage — Sri Lanka travel guides (`knowledge_wikivoyage` connector, monthly)
| Check | Finding |
|---|---|
| robots.txt | `Disallow: /w/` (blanket, for the generic user-agent) covers page-scraping, not the MediaWiki API route this connector uses (`/w/api.php?action=query`). Same reading, and same route, this project's existing `wikidata_enrich.py`/`wikipedia_popularity.py` connectors already use for Wikipedia (same MediaWiki software) — the API is Wikimedia's own sanctioned machine-access path, documented at meta.wikimedia.org/wiki/User-Agent_policy, provided a descriptive User-Agent, `maxlag`, and a modest rate are used (all three here: `UA` header with a contact address, `maxlag=5`, 1 req/s). |
| License | CC BY-SA 3.0 — attribution required, stored per document (`knowledge_document.license`/`.url`) and surfaced as a citation link on every answer that uses it, never silently absorbed into an answer with no source shown. |
| Structure | `action=query&prop=extracts&explaintext=1&exsectionformat=wiki` returns plain text with `== Heading ==` wikitext section markers, parsed by `app/rag/chunking.py` the same way it parses a team note's Markdown headings. |
| Coverage, measured 2026-09-30 | 68 pages (country page + 9 province/region guides + 58 city/place guides), 928 chunks. Page → district placed via the existing `resolve_place()` geocoder, never dropped for a weak match — falls back to country-level (`district_id NULL`) rather than being discarded. |
| Live data quirk | A page can carry current-events prose that will date (e.g. the Ella page's live note on Cyclone Ditwah rail disruption, still present as of this ingest) — expected of a community-edited source, not a bug; `last_verified` stays NULL for Wikivoyage documents specifically because no one has actually checked this run's content against an official source, unlike the hand-verified team notes. |

## Rejected
| Source | Reason |
|---|---|
| allevents.in | Cloudflare bot challenge on every request, including robots.txt — automated access is clearly unwelcome. |
| Songkick, Bandsintown | Commercial aggregators whose terms prohibit scraping; Songkick's robots.txt blocks named crawlers. |
| Facebook Events, TripAdvisor, Google | Terms forbid scraping and they actively block it (standing project rule). |
| Department of Wildlife Conservation (dwc.gov.lk) | No fee table published on the official site. Third-party pages disagree with each other (e.g. Yala quoted at both USD 25 and USD 40 for 2026), so scraping them would put guesses into budgets. National-park fees stay admin-entered. |
| srilanka.travel "Festivals Year Around" | robots.txt allows everything, but the page gives festival *rules* ("10 days climaxing on Esala Poya"), not dates. Used as the citation for the festival seed's rules instead of being scraped. |

## Festival dates (not scraped)
`festival_seed` computes each festival's dates from its rule in `app/data/festival_seed.csv`
plus the official Poya calendar from the `holidays` package (`country_holidays("LK")`,
which follows the government gazette — including leap-month extras such as 2026's
Adhi Poson Poya that a plain full-moon calculation would miss). A year the gazette
hasn't covered yet is skipped with a warning, never guessed; re-run the connector
after upgrading `holidays`.
