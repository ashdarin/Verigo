# Company catalogue normalization

Implemented on 2026-08-16 for the read-only Company Finder catalogue.

## Data profile

- Rows: 35,828,989
- Countries/territories: 249
- Industries: 147 (LinkedIn legacy taxonomy)
- Company-size bands: 8
- Missing country: 3,985,628
- Missing industry: 6,127,714
- Missing website: 12,139,517
- Names with edge decoration: 21,345
- Names with repeated whitespace: 67,236

Raw catalogue fields remain unchanged. API responses add normalized display
fields so cleaning remains reversible and filter keys remain stable.

## Response contract

The search response retains `name`, `country`, `region`, `locality`,
`industry`, `size`, `website`, and `linkedin_url`. It also exposes:

- `name_display`
- `country_label`, `region_label`, `locality_label`, `location_label`
- `industry_label`, `size_label`
- `website_url`, `website_domain`

Company names are Unicode-normalized, HTML-decoded, stripped of edge capture
artifacts, whitespace-normalized, and conservatively title-cased. Brand names
are not machine-translated.

Countries use CLDR Simplified Chinese labels. The most frequent subdivisions
for the twelve largest country catalogues have curated Chinese labels; other
subdivisions and localities use normalized source names. All 147 industries
and all eight size bands have stable Chinese labels.

Website links only accept HTTP(S)-compatible hostnames, remove fragments and
common tracking parameters, and display the domain. LinkedIn links only accept
LinkedIn company, showcase, or school paths and are rendered as an accessible
brand icon.

## Query and performance

The browser submits raw stable facet values while displaying Chinese labels.
Country and industry facets load sequentially to avoid concurrent DuckDB scans.
The dedicated catalogue service caches up to 64 facet/filter combinations;
the measured industry facet fell from 0.447 seconds on the first scan to 0.003
seconds on a cache hit.

The application normalizes only the bounded response page (maximum 50 rows).
It does not update or duplicate all 35.8 million source rows during requests.

## Verification

- `python tests/company_catalog_smoke.py`
- `node tests/company_catalog_ui.js`
- Live desktop and mobile Playwright checks against `/admin/credits`
- Live route checks: 249 countries, 147 industries, 8 size bands

The application and dedicated catalogue service are deployed independently.
Update the dedicated service first because the application facet adapter
expects its `/facets/{facet}` endpoint.
