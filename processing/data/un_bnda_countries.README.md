# UN BNDA country boundaries (ccode reference)

`un_bnda_countries.geojson` — United Nations Geospatial "BNDA_simplified"
(Boundaries of Administrative units, country level), the authoritative,
politically-neutral UN administrative boundary set.

- **Source:** UN Geoportal (https://geoportal.un.org/), public item
  `BNDA_simplified` (id `6d6fb235f64d47248a5e3b78ef4b6273`).
  Download: `https://geoportal.un.org/arcgis/sharing/rest/content/items/6d6fb235f64d47248a5e3b78ef4b6273/data`
- **Fetched:** 2026-07-14
- **Features:** 262 (250 distinct ISO2 codes); every feature carries `iso2cd`
  (ISO 3166-1 alpha-2), `iso3cd`, `m49_cd`, `nam_en`.
- **Why this over Natural Earth / OSM:** native ISO2 for all countries (no NE
  `-99` France/Norway/Kosovo quirk), dependent territories modelled as separate
  ISO features (PR/GF/PF/GU/AS/NC…), Antarctica (AQ) included, antimeridian
  handled, topologically coherent (no per-country slivers → correct at borders
  e.g. Strasbourg). Used by `processing.ccode_enrichment.UnCountryIndex`.
- **Licence:** UN Geodata terms — free to use with attribution and the standard
  UN non-endorsement-of-boundaries disclaimer. Refresh by re-downloading the
  item `data` endpoint above.
