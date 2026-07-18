# Font bank sources (for the synthetic style corpus)

The font binaries are gitignored (mixed licenses, ~3 MB); rebuild the bank from these sources.
All are redistributable; each renders one or more OS-axis treatments.

| File | Source | License | OS axis it stands in for |
|------|--------|---------|--------------------------|
| FreeSerif.ttf / -Italic / -Bold | GNU FreeFont (system: `fonts-freefont-ttf`) | GPL+font-exception | serif upright / italic |
| FreeSans.ttf / -Oblique | GNU FreeFont | GPL+font-exception | sans |
| DejaVuSerif.ttf / -Italic | DejaVu (system: `fonts-dejavu`) | permissive (Bitstream Vera) | serif upright / italic |
| NimbusRoman-Regular/-Italic.otf | URW base35 (system: `fonts-urw-base35`) | AGPL / URW | Times-like serif (RP) |
| NimbusSans-Regular.otf | URW base35 | AGPL / URW | Helvetica-like sans |
| RobotoSlab.ttf | Google Fonts `apache/robotoslab` | Apache-2.0 | **slab / Egyptian (EC)** |
| ZillaSlab-Italic.ttf / -MediumItalic | Google Fonts `ofl/zillaslab` | OFL-1.1 | **italic slab** (SG-flagged; larger features) |
| Cinzel.ttf | Google Fonts `ofl/cinzel` | OFL-1.1 | inscriptional / engraved caps |
| PirataOne.ttf | Google Fonts `ofl/pirataone` | OFL-1.1 | **blackletter (GermanText)** |
| UnifrakturCook.ttf | Google Fonts `ofl/unifrakturcook` | OFL-1.1 | blackletter |

System fonts: `apt-get install fonts-freefont-ttf fonts-dejavu fonts-urw-base35`.
Google fonts: `curl -sL https://github.com/google/fonts/raw/main/<path>/<File>.ttf`.
Outline caps and wide tracking are **synthesised** at render time (`fonts.py`), not separate faces.
