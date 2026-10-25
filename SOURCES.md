# Prior art, research, and licensing

Checked October 25, 2026.

## Historical Hoopla downloader

- Original name: `larsenv/Hoopla-Digital-Downloader`.
- Old archived source links are documented in: https://github.com/jo1gi/audiobook-dl/issues/70
- On Dec. 4, 2025, a contributor stated that a modified version was removed after a Hoopla DMCA complaint.
- **Decision:** Do not embed, mirror, copy, or run archived DRM circumvention tooling. We are not claiming this original project currently works.

## Reusable inspiration (not copied code)

- https://github.com/jo1gi/audiobook-dl — audiobook CLI architecture, metadata handling, FFmpeg packaging; GPL-3.0.
- https://github.com/rmcrackan/Libation — audiobook library workflow inspiration; GPL-3.0. It supports Audible, not Hoopla.
- https://github.com/underhax/audiobook-tools — general audiobook packaging ideas; verify license before copying any implementation.

This project's starter code is newly authored; architecture ideas are not the same as copying third-party code. If adding upstream GPL-3.0 source code, preserve copyright notices and follow GPL-3.0 requirements (including licensing the combined derivative accordingly).

## Hoopla source and policy

- Official audiobook page: https://www.hoopladigital.com/audiobook/kindred-octavia-e-butler/13507414
- Terms: https://www.hoopladigital.com/TERMS (modified June 10, 2026). Prohibits extracting/recording content outside the service and bypassing DRM.
- Public library guidance: https://library.austintexas.libanswers.com/faq/96443 (updated April 17, 2026). Mac browser does not offer offline media downloads.

## Scope boundary

No authentication bypass, media URL scraping, DRM bypass, permanent extraction of library loans, account-cookie exports, or recording of Hoopla streams.
