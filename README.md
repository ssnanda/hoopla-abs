# Hoopla Audiobook Tools — 0.1.0

An original **macOS-first** command-line starter project for organizing public Hoopla audiobook listing information and packaging **your own authorized, DRM-free local audio** into Audiobookshelf-compatible `.m4b` files. Works on Intel and Apple Silicon Macs (and Linux).

**Limitations:** This project **does not** log into Hoopla, download Hoopla loans, extract streaming audio, record playback, decrypt content, or bypass DRM. A book's Hoopla page URL is a *listing*, not a direct media download link. No audiobook content is included in this repository.

## Requirements

- macOS, Python 3.10+, FFmpeg and FFprobe installed and on `PATH`.
- No third-party Python dependencies.
- No Audiobookshelf connection or server required.

## Quick start (on your Mac)

1. Clone this repository and open Terminal in that folder.
2. Run `./start.sh doctor` (or `sh start.sh doctor`).
3. Test the public listing URL (no login required):
   `./start.sh info 'https://www.hoopladigital.com/audiobook/kindred-octavia-e-butler/13507414' --fetch`
4. Create the synthetic audio sample:
   `./start.sh sample --output ~/Downloads/hoopla-book-tools-sample`
5. Inspect it:
   `./start.sh inspect ~/Downloads/hoopla-book-tools-sample/'Sample Audiobook.m4b'`
6. Run local tests:
   `python3 -m unittest discover -s tests -v`

If `--fetch` fails because Hoopla denies automated public-page requests, omit `--fetch`. That still parses and validates the public listing URL without logging in.

## Package DRM-free local audio

Place **authorized** MP3/M4A/WAV/FLAC/etc. files in one folder, named in playing order (like `01 Introduction.mp3`, `02 Chapter 1.mp3`). Then:

`./start.sh pack ~/Downloads/my-book-parts --title 'My Book' --author 'My Author' --output ~/Downloads/'My Book.m4b'`

Optional: `--cover ~/Pictures/cover.jpg`

- Sorts input files naturally (chapter 2 before chapter 10).
- Normalizes each track to AAC, builds a single M4B, embeds chapters/title/author and optional cover.
- Never alters input tracks. Never overwrites an existing output file.
- Can take time for longer books because it re-encodes parts.

## Project layout

- `hoopla_tools.py` — original Python CLI, public listing parsing, and local media packaging.
- `start.sh` — one-step CLI launcher.
- `tests/` — unit tests and local end-to-end FFmpeg smoke test.
- `examples/` — sample public-book metadata without audiobook content.
- `SOURCES.md` — upstream research and license considerations.
- `ROADMAP.md` — proposed development milestones.

## Safety and privacy

- No passwords, Hoopla cookies, tokens, or authentication are required or collected.
- Public listing lookup never requests protected audio.
- `pack` works on local audio you are authorized to keep and convert.
- Review Hoopla's applicable terms before creating new integrations.

## License

MIT. This repository is clean-room original implementation; no historical Hoopla-downloader code or GPL-licensed code has been copied. If GPL components are incorporated later, revisit distribution obligations before publication.
