#!/usr/bin/env python3
"""HPL: public listing lookup and packaging of authorized local audio.

Does not authenticate, retrieve licensed streams, or circumvent DRM.
"""

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

VERSION = "0.2.3"
AUDIO_SUFFIXES = {".mp3", ".m4a", ".m4b", ".mp4", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
MAX_PUBLIC_PAGE_BYTES = 2_000_000
MAX_COVER_BYTES = 15_000_000


class ToolError(Exception):
    pass


def hpl_book_url(value):
    """Validate a public Hoopla audiobook listing, not arbitrary links."""
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"hoopladigital.com", "www.hoopladigital.com"}:
        raise ToolError("Expected an HTTPS hoopladigital.com audiobook URL")
    match = re.fullmatch(r"/audiobook/([^/]+)/([0-9]+)/*", parsed.path)
    if not match:
        raise ToolError("Expected a URL like https://www.hoopladigital.com/audiobook/title-author/12345")
    return {
        "provider": "hpl",
        "media_type": "audiobook",
        "id": match.group(2),
        "slug": match.group(1),
        "url": f"https://www.hoopladigital.com/audiobook/{match.group(1)}/{match.group(2)}",
    }


class PublicMetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.in_title = False
        self.title_chunks = []
        self.in_jsonld = False
        self.jsonld_chunks = []
        self.jsonld_items = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key and attrs.get("content"):
                self.fields.setdefault(key, attrs["content"].strip())
        elif tag == "title":
            self.in_title = True
        elif tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.in_jsonld = True
            self.jsonld_chunks = []

    def handle_data(self, data):
        if self.in_title:
            self.title_chunks.append(data)
        if self.in_jsonld:
            self.jsonld_chunks.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.in_jsonld:
            self.in_jsonld = False
            try:
                self.jsonld_items.append(json.loads("".join(self.jsonld_chunks)))
            except json.JSONDecodeError:
                pass


def _book_json_ld(value):
    if isinstance(value, list):
        for entry in value:
            result = _book_json_ld(entry)
            if result:
                return result
    elif isinstance(value, dict):
        if "@graph" in value:
            result = _book_json_ld(value["@graph"])
            if result:
                return result
        typ = value.get("@type", "")
        types = typ if isinstance(typ, list) else [typ]
        if any(t in {"Book", "Audiobook"} for t in types):
            return value
    return {}


def _person_names(value):
    if not value:
        return []
    people = value if isinstance(value, list) else [value]
    return [str(person.get("name", "") if isinstance(person, dict) else person).strip()
            for person in people if str(person.get("name", "") if isinstance(person, dict) else person).strip()]


def _iso_duration_seconds(value):
    """Convert the simple ISO-8601 durations used by listing JSON-LD."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    if not match:
        return None
    days, hours, minutes, seconds = (float(item or 0) for item in match.groups())
    result = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return round(result, 3) if result > 0 else None


def public_metadata_from_html(page, basic):
    """Read public page information only. No login, media URLs, or cookies."""
    parser = PublicMetaParser()
    parser.feed(page)
    result = dict(basic)
    ld = _book_json_ld(parser.jsonld_items)
    browser_title = parser.fields.get("og:title") or "".join(parser.title_chunks).strip()
    m = re.match(r"^\s*(.+?)\s+Audiobook by\s+(.+?)\s*\|\s*hoopla\s*$", browser_title, re.I)
    if m:
        result["title"] = html.unescape(m.group(1))
        result["author"] = html.unescape(m.group(2))
    else:
        name = ld.get("name") or parser.fields.get("og:title")
        if name:
            result["title"] = html.unescape(name)
    authors = _person_names(ld.get("author"))
    if authors and "author" not in result:
        result["author"] = ", ".join(authors)
    narrators = _person_names(ld.get("readBy"))
    if narrators:
        result["narrator"] = ", ".join(narrators)
    duration = _iso_duration_seconds(ld.get("duration"))
    if duration:
        result["duration_seconds"] = duration
    description = ld.get("description") or parser.fields.get("og:description") or parser.fields.get("description")
    if description:
        result["description"] = html.unescape(description)
    image = ld.get("image") or parser.fields.get("og:image")
    if isinstance(image, dict):
        image = image.get("url")
    if isinstance(image, list):
        image = image[0] if image else None
    if image:
        result["cover_url"] = image
    result["source"] = "public_listing"
    return result


def fetch_public_listing(url):
    basic = hpl_book_url(url)
    req = Request(basic["url"], headers={"User-Agent": "HPL/0.2 (public book information only)", "Accept": "text/html"})
    try:
        with urlopen(req, timeout=15) as response:
            if response.status != 200:
                raise ToolError(f"Public listing returned HTTP {response.status}")
            if hpl_book_url(response.geturl())["id"] != basic["id"]:
                raise ToolError("Public listing redirected to an unexpected title")
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ToolError(f"Public listing returned unexpected content type: {content_type}")
            # Only read page HTML, capped to avoid excessive data retrieval.
            raw = response.read(MAX_PUBLIC_PAGE_BYTES + 1)
            if len(raw) > MAX_PUBLIC_PAGE_BYTES:
                raise ToolError("Public listing was unexpectedly large")
            encoding = response.headers.get_content_charset() or "utf-8"
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ToolError(f"Couldn't read public Hoopla listing: {exc}") from exc
    return public_metadata_from_html(raw.decode(encoding, "replace"), basic)


def download_cover(url, destination):
    """Download only public listing artwork, with size/type validation."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ToolError("Cover URL must use HTTPS")
    destination = Path(destination).expanduser().resolve()
    if destination.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ToolError("Cover destination must end with .jpg, .jpeg, .png, or .webp")
    if destination.exists():
        raise ToolError(f"Refusing to overwrite existing cover: {destination}")
    request = Request(url, headers={"User-Agent": f"HPL/{VERSION} (public cover artwork)", "Accept": "image/*"})
    try:
        with urlopen(request, timeout=20) as response:
            content_type = response.headers.get_content_type()
            if response.status != 200 or not content_type.startswith("image/"):
                raise ToolError(f"Cover returned HTTP {response.status} with content type {content_type}")
            data = response.read(MAX_COVER_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ToolError(f"Couldn't download public cover: {exc}") from exc
    if len(data) > MAX_COVER_BYTES:
        raise ToolError("Cover exceeds the 15 MB safety limit")
    if not data:
        raise ToolError("Cover download was empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(prefix=".hpl-cover-", dir=destination.parent, delete=False) as temporary:
            temporary.write(data)
            temporary_path = Path(temporary.name)
        os.link(temporary_path, destination)
    except FileExistsError as exc:
        raise ToolError(f"Refusing to overwrite existing cover: {destination}") from exc
    finally:
        if "temporary_path" in locals():
            temporary_path.unlink(missing_ok=True)
    return destination


def ffmpeg_binary(name):
    path = shutil.which(name)
    if not path:
        raise ToolError(f"{name} is not installed or not on PATH")
    return path


def run(args):
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise ToolError(f"Command failed: {message[-1600:]}")
    return completed.stdout.strip()


def duration_ms(path):
    ffprobe = ffmpeg_binary("ffprobe")
    out = run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        result = round(float(out) * 1000)
    except (ValueError, OverflowError) as exc:
        raise ToolError(f"No valid audio duration for {path.name}") from exc
    if result <= 0:
        raise ToolError(f"Invalid zero-length audio track: {path.name}")
    return result


def natural_key(path):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", path.name)]


def escape_ffmetadata(s):
    return re.sub(r"([\\;#=\n])", lambda m: "\\" + m.group(1), s)


def make_chapter_metadata(title, author, names, lengths):
    lines = [";FFMETADATA1", f"title={escape_ffmetadata(title)}", f"artist={escape_ffmetadata(author)}", "genre=Audiobook"]
    elapsed = 0
    for name, length in zip(names, lengths):
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={elapsed}", f"END={elapsed + length}", f"title={escape_ffmetadata(name)}"]
        elapsed += length
    return "\n".join(lines) + "\n"


def ffconcat_escape(path):
    return str(path).replace("'", "'\\''")


def safe_filename(s):
    out = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", s).strip(" .")
    return out[:160] or "audiobook"


def clean_chapter_name(filename):
    """Remove a track index while preserving numeric chapter titles."""
    match = re.match(r"^[0-9]{1,3}[ ._-]+(.+)$", filename)
    return match.group(1).strip() if match and match.group(1).strip() else filename


def input_files(source):
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ToolError(f"Source folder not found: {source}")
    files = sorted((x for x in source.iterdir() if x.is_file() and x.suffix.lower() in AUDIO_SUFFIXES), key=natural_key)
    if not files:
        raise ToolError(f"No supported audio files in {source}")
    return source, files


def probe_details(path):
    """Read local audio metadata and artwork disposition with ffprobe."""
    text = run([ffmpeg_binary("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", str(path)])
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(f"ffprobe returned invalid data for {Path(path).name}") from exc
    if not isinstance(result, dict):
        raise ToolError(f"ffprobe returned unexpected data for {Path(path).name}")
    return result


def infer_metadata(source, files):
    """Use album/artist from local source audio without changing it."""
    for path in files:
        tags = probe_details(path).get("format", {}).get("tags", {})
        tags = {k.casefold(): str(v) for k, v in tags.items()}
        title = tags.get("album") or tags.get("album_title") or ""
        author = tags.get("album_artist") or tags.get("artist") or ""
        narrator = tags.get("narrator") or tags.get("composer") or ""
        if title or author or narrator:
            return {"title": title or source.name, "author": author, "narrator": narrator}
    return {"title": source.name, "author": "", "narrator": ""}


def embedded_chapters(path, encoded_length):
    """Return scaled chapter ranges from a source file, if usable."""
    details = probe_details(path)
    chapters = details.get("chapters", [])
    try:
        source_length = float(details.get("format", {}).get("duration", 0)) * 1000
    except (TypeError, ValueError):
        source_length = 0
    if not chapters or source_length <= 0:
        return []
    scale = encoded_length / source_length
    result = []
    for index, chapter in enumerate(chapters, 1):
        try:
            start = max(0, round(float(chapter["start_time"]) * 1000 * scale))
            end = min(encoded_length, round(float(chapter["end_time"]) * 1000 * scale))
        except (KeyError, TypeError, ValueError):
            return []
        if end <= start:
            return []
        name = str(chapter.get("tags", {}).get("title") or f"Chapter {index}")
        result.append((start, end, name))
    return result


def make_timed_chapter_metadata(title, author, narrator, chapters):
    lines = [";FFMETADATA1", f"title={escape_ffmetadata(title)}", f"artist={escape_ffmetadata(author)}", "genre=Audiobook"]
    if narrator:
        lines.append(f"narrator={escape_ffmetadata(narrator)}")
    for start, end, name in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={escape_ffmetadata(name)}"]
    return "\n".join(lines) + "\n"


def discover_cover(source, files, temporary):
    """Find local artwork or extract embedded artwork into temporary storage."""
    for basename in ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "folder.jpg", "folder.png", "folder.webp", "front.jpg", "front.png", "front.webp"):
        cover = source / basename
        if cover.is_file():
            return cover
    for path in files:
        streams = probe_details(path).get("streams", [])
        if not any(s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") for s in streams):
            continue
        output = temporary / "embedded-cover.png"
        try:
            run([ffmpeg_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                 "-map", "0:v:0", "-frames:v", "1", str(output)])
        except ToolError:
            continue
        if output.exists() and output.stat().st_size:
            return output
    return None


def folder_preview(source):
    source, files = input_files(source)
    tags = infer_metadata(source, files)
    cover = next((str(source / name) for name in ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "folder.jpg", "folder.png", "folder.webp", "front.jpg", "front.png", "front.webp") if (source / name).is_file()), None)
    return {"source": str(source), "tracks": [x.name for x in files],
            "chapter_names": [clean_chapter_name(x.stem) for x in files],
            "suggested_title": tags["title"], "suggested_author": tags["author"],
            "cover_file": cover, "total_duration_seconds": round(sum(duration_ms(x) for x in files) / 1000, 3)}


def package_audio(source, output, title=None, author=None, cover=None, auto_cover=True, narrator=None):
    ffmpeg = ffmpeg_binary("ffmpeg")
    ffmpeg_binary("ffprobe")
    source, files = input_files(source)
    inferred = infer_metadata(source, files)
    title = title if title is not None else inferred.get("title", source.name)
    author = author if author is not None else inferred.get("author", "")
    narrator = narrator if narrator is not None else inferred.get("narrator", "")
    if not str(title).strip():
        raise ToolError("Audiobook title cannot be empty")
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != ".m4b":
        raise ToolError("Output filename must end with .m4b")
    if output.exists():
        raise ToolError(f"Refusing to overwrite existing audiobook: {output}")
    if cover:
        cover = Path(cover).expanduser().resolve()
        if not cover.is_file() or cover.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ToolError("Cover must be an existing JPG, PNG, or WebP file")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hpl-", dir=output.parent) as temporary:
        temp = Path(temporary)
        if not cover and auto_cover:
            cover = discover_cover(source, files, temp)
        if cover:
            print(f"Cover: {Path(cover).name}")
        encoded = []
        durations = []
        chapter_ranges = []
        print(f"Packaging {len(files)} local audio part(s) into M4B...")
        for index, source_file in enumerate(files, 1):
            part = temp / f"{index:05d}.m4a"
            print(f"  [{index}/{len(files)}] Encoding {source_file.name}...", flush=True)
            run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source_file),
                 "-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2", str(part)])
            encoded.append(part)
            durations.append(duration_ms(part))
            embedded = embedded_chapters(source_file, durations[-1])
            offset = sum(durations[:-1])
            if embedded:
                chapter_ranges.extend((offset + start, offset + end, name) for start, end, name in embedded)
            else:
                chapter_ranges.append((offset, offset + durations[-1], clean_chapter_name(source_file.stem)))
        listing = temp / "parts.txt"
        listing.write_text("".join(f"file '{ffconcat_escape(p)}'\n" for p in encoded), encoding="utf-8")
        chapters = temp / "chapters.ffmeta"
        chapters.write_text(make_timed_chapter_metadata(title, author, narrator, chapter_ranges), encoding="utf-8")
        staged = temp / "finished.m4b"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(listing),
                   "-i", str(chapters)]
        if cover:
            command += ["-i", str(cover)]
        command += ["-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1", "-c:a", "copy",
                    "-metadata", f"title={title}", "-metadata", f"artist={author}", "-metadata", "genre=Audiobook"]
        if narrator:
            command += ["-metadata", f"narrator={narrator}"]
        if cover:
            command += ["-map", "2:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
        command += ["-movflags", "+faststart", str(staged)]
        print("Finalizing chapters, metadata, and artwork...", flush=True)
        run(command)
        # A hard link makes the completed file visible atomically and never overwrites.
        try:
            os.link(staged, output)
        except FileExistsError as exc:
            raise ToolError(f"Refusing to overwrite existing audiobook: {output}") from exc
    print(f"Created: {output}")
    return output


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def catalog_folder(source):
    """Catalog local audio and group byte-identical duplicate files."""
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ToolError(f"Catalog folder not found: {source}")
    files = sorted((path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES), key=natural_key)
    if not files:
        raise ToolError(f"No supported audio files in {source}")
    items = []
    by_hash = {}
    for path in files:
        details = probe_details(path)
        tags = {key.casefold(): str(value) for key, value in details.get("format", {}).get("tags", {}).items()}
        try:
            duration = round(float(details.get("format", {}).get("duration", 0)), 3)
        except (TypeError, ValueError):
            duration = 0
        digest = file_sha256(path)
        item = {"path": str(path), "size_bytes": path.stat().st_size, "sha256": digest,
                "duration_seconds": duration, "title": tags.get("title", ""),
                "author": tags.get("album_artist") or tags.get("artist", ""),
                "narrator": tags.get("narrator", "")}
        items.append(item)
        by_hash.setdefault(digest, []).append(str(path))
    duplicates = [paths for paths in by_hash.values() if len(paths) > 1]
    return {"source": str(source), "file_count": len(items), "items": items, "duplicate_groups": duplicates}


def save_json_exclusive(data, destination):
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.write("\n")
    except FileExistsError as exc:
        raise ToolError(f"Refusing to overwrite existing file: {destination}") from exc
    return destination


def capability_report(url=None):
    result = {
        "metadata": {"supported": True, "authentication": "none", "source": "public listing"},
        "local_conversion": {"supported": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
                             "input": "authorized local audio", "output": "M4B"},
        "hoopla_download": {"supported": False, "status": "blocked",
                            "reason": "No documented patron export API; official downloads are temporary in-app access and playback is DRM-protected.",
                            "authentication": "No supported third-party OAuth or device flow found."},
        "references": ["https://www.hoopladigital.com/terms", "https://www.hoopladigital.com/help",
                       "https://developer.hoopladigital.com/getting-started"],
    }
    if url:
        metadata = fetch_public_listing(url)
        result["metadata"]["live_check"] = {"ok": True, "id": metadata["id"], "title": metadata.get("title", "")}
    return result


def inspect_file(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ToolError(f"File not found: {path}")
    ffprobe = ffmpeg_binary("ffprobe")
    raw = run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", str(path)])
    data = json.loads(raw)
    result = {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "duration_seconds": float(data.get("format", {}).get("duration", "0")),
        "tags": data.get("format", {}).get("tags", {}),
        "chapters": [{"start": c.get("start_time"), "end": c.get("end_time"), "title": c.get("tags", {}).get("title", "")}
                     for c in data.get("chapters", [])],
        "streams": [{"type": s.get("codec_type"), "codec": s.get("codec_name")} for s in data.get("streams", [])],
    }
    return result


def do_sample(folder):
    ffmpeg = ffmpeg_binary("ffmpeg")
    folder = Path(folder).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    parts = folder / "parts"
    parts.mkdir(exist_ok=True)
    for i, frequency in enumerate([440, 550, 660], 1):
        file = parts / f"{i:02d}-test-tone.wav"
        if file.exists():
            raise ToolError(f"Refusing to overwrite test sample: {file}")
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
             f"sine=frequency={frequency}:duration=2", "-c:a", "pcm_s16le", str(file)])
    result = package_audio(parts, folder / "Sample Audiobook.m4b", "Sample Audiobook", "Demo (synthetic test tones)")
    info = inspect_file(result)
    if len(info["chapters"]) != 3 or info["duration_seconds"] < 5.5:
        raise ToolError("Sample M4B verification failed: missing chapters or unexpected duration")
    print("PASS: Sample M4B verified with 3 chapters; no copyrighted audio used")


def main(argv=None):
    parser = argparse.ArgumentParser(description="HPL: audiobook catalog lookup and local M4B packaging")
    parser.add_argument("--version", action="version", version=VERSION)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="Check local tools")
    info = subs.add_parser("info", help="Read public HPL audiobook listing metadata")
    info.add_argument("url", help="Public audiobook listing URL")
    info.add_argument("--fetch", action="store_true", help="Fetch public page title/author/cover URL; no authentication")
    info.add_argument("--save", metavar="FILE", help="Save information as JSON without overwriting")
    info.add_argument("--download-cover", metavar="FILE", help="Download public cover artwork without overwriting")
    preview = subs.add_parser("preview", help="Review authorized local tracks and inferred metadata")
    preview.add_argument("source")
    pack = subs.add_parser("pack", help="Build M4B from local authorized audio files")
    pack.add_argument("source", help="Folder of existing audio tracks")
    pack.add_argument("--title", help="Override title; otherwise inferred from local tags/folder")
    pack.add_argument("--author", help="Override author; otherwise inferred from local tags")
    pack.add_argument("--narrator", help="Set narrator metadata")
    pack.add_argument("--output", help="M4B destination (defaults to input folder name in current directory)")
    pack.add_argument("--cover", help="Override cover with a local JPG/PNG file")
    pack.add_argument("--no-auto-cover", action="store_true", help="Disable cover auto-detection")
    inspect = subs.add_parser("inspect", help="Inspect an existing local audio file")
    inspect.add_argument("file")
    sample = subs.add_parser("sample", help="Generate and verify synthetic sample M4B")
    sample.add_argument("--output", default="./hpl-test-sample", help="Directory for test audio and M4B")
    catalog = subs.add_parser("catalog", help="Catalog local audio and identify exact duplicates")
    catalog.add_argument("source", help="Folder to scan recursively")
    catalog.add_argument("--save", metavar="FILE", help="Save catalog as JSON without overwriting")
    capabilities = subs.add_parser("capabilities", help="Report supported metadata, conversion, and download capabilities")
    capabilities.add_argument("url", nargs="?", help="Optional public listing URL for a live metadata check")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            print(f"HPL v{VERSION}\nPython: {sys.version.split()[0]}\nPlatform: {sys.platform}")
            for name in ("ffmpeg", "ffprobe"):
                print(f"{name}: {shutil.which(name) or 'NOT FOUND'}")
            print("HPL account: not required; no login or protected-audio extraction")
            print("Audiobookshelf connection: not required")
            if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                raise ToolError("FFmpeg and FFprobe are required for local conversion")
        elif args.command == "info":
            data = fetch_public_listing(args.url) if args.fetch or args.download_cover else hoopla_book_url(args.url)
            print(json.dumps(data, indent=2, ensure_ascii=False))
            if args.save:
                destination = save_json_exclusive(data, args.save)
                print(f"Saved: {destination}")
            if args.download_cover:
                if not data.get("cover_url"):
                    raise ToolError("Public listing does not provide a cover URL")
                print(f"Cover saved: {download_cover(data['cover_url'], args.download_cover)}")
        elif args.command == "preview":
            print(json.dumps(folder_preview(args.source), indent=2, ensure_ascii=False))
        elif args.command == "pack":
            output = args.output or f"./{safe_filename(args.title or Path(args.source).name)}.m4b"
            package_audio(args.source, output, args.title, args.author, args.cover, not args.no_auto_cover, args.narrator)
        elif args.command == "inspect":
            print(json.dumps(inspect_file(args.file), indent=2, ensure_ascii=False))
        elif args.command == "sample":
            do_sample(args.output)
        elif args.command == "catalog":
            data = catalog_folder(args.source)
            print(json.dumps(data, indent=2, ensure_ascii=False))
            if args.save:
                print(f"Saved: {save_json_exclusive(data, args.save)}")
        elif args.command == "capabilities":
            print(json.dumps(capability_report(args.url), indent=2, ensure_ascii=False))
    except (ToolError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
