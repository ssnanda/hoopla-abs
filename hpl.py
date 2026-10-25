#!/usr/bin/env python3
"""HPL: public listing lookup and packaging of authorized local audio.

Does not authenticate, retrieve licensed streams, or circumvent DRM.
"""

import argparse
import html
from html.parser import HTMLParser
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

VERSION = "0.2.1"
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}


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
    author = ld.get("author")
    if author and "author" not in result:
        if isinstance(author, list):
            author = author[0]
        result["author"] = author.get("name", "") if isinstance(author, dict) else str(author)
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
            # Only read page HTML, capped to avoid excessive data retrieval.
            raw = response.read(2_000_000)
            encoding = response.headers.get_content_charset() or "utf-8"
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ToolError(f"Couldn't read public Hoopla listing: {exc}") from exc
    return public_metadata_from_html(raw.decode(encoding, "replace"), basic)


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
    except ValueError as exc:
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
    text = run([ffmpeg_binary("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    return json.loads(text)


def infer_metadata(source, files):
    """Use album/artist from local source audio without changing it."""
    for path in files:
        tags = probe_details(path).get("format", {}).get("tags", {})
        tags = {k.casefold(): str(v) for k, v in tags.items()}
        title = tags.get("album") or tags.get("album_title") or ""
        author = tags.get("album_artist") or tags.get("artist") or ""
        if title or author:
            return {"title": title or source.name, "author": author}
    return {"title": source.name, "author": ""}


def discover_cover(source, files, temporary):
    """Find local artwork or extract embedded artwork into temporary storage."""
    for basename in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "front.png"):
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
    cover = next((str(source / name) for name in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "front.png") if (source / name).is_file()), None)
    return {"source": str(source), "tracks": [x.name for x in files],
            "chapter_names": [clean_chapter_name(x.stem) for x in files],
            "suggested_title": tags["title"], "suggested_author": tags["author"],
            "cover_file": cover, "total_duration_seconds": round(sum(duration_ms(x) for x in files) / 1000, 3)}


def package_audio(source, output, title=None, author=None, cover=None, auto_cover=True):
    ffmpeg = ffmpeg_binary("ffmpeg")
    ffmpeg_binary("ffprobe")
    source, files = input_files(source)
    inferred = infer_metadata(source, files) if title is None or author is None else {}
    title = title if title is not None else inferred.get("title", source.name)
    author = author if author is not None else inferred.get("author", "")
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != ".m4b":
        raise ToolError("Output filename must end with .m4b")
    if output.exists():
        raise ToolError(f"Refusing to overwrite existing audiobook: {output}")
    if cover:
        cover = Path(cover).expanduser().resolve()
        if not cover.is_file() or cover.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise ToolError("Cover must be an existing JPG or PNG file")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hpl-") as temporary:
        temp = Path(temporary)
        if not cover and auto_cover:
            cover = discover_cover(source, files, temp)
        if cover:
            print(f"Cover: {Path(cover).name}")
        encoded = []
        durations = []
        names = []
        print(f"Packaging {len(files)} local audio part(s) into M4B...")
        for index, source_file in enumerate(files, 1):
            part = temp / f"{index:05d}.m4a"
            run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source_file),
                 "-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2", str(part)])
            encoded.append(part)
            durations.append(duration_ms(part))
            names.append(clean_chapter_name(source_file.stem))
            print(f"  [{index}/{len(files)}] {source_file.name}")
        listing = temp / "parts.txt"
        listing.write_text("".join(f"file '{ffconcat_escape(p)}'\n" for p in encoded), encoding="utf-8")
        chapters = temp / "chapters.ffmeta"
        chapters.write_text(make_chapter_metadata(title, author, names, durations), encoding="utf-8")
        staged = temp / "finished.m4b"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(listing),
                   "-i", str(chapters)]
        if cover:
            command += ["-i", str(cover)]
        command += ["-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1", "-c:a", "copy",
                    "-metadata", f"title={title}", "-metadata", f"artist={author}", "-metadata", "genre=Audiobook"]
        if cover:
            command += ["-map", "2:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
        command += ["-movflags", "+faststart", str(staged)]
        run(command)
        # Never overwrite existing files; retain all original input tracks.
        with output.open("xb") as dest, staged.open("rb") as src:
            shutil.copyfileobj(src, dest)
    print(f"Created: {output}")
    return output


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
    preview = subs.add_parser("preview", help="Review authorized local tracks and inferred metadata")
    preview.add_argument("source")
    pack = subs.add_parser("pack", help="Build M4B from local authorized audio files")
    pack.add_argument("source", help="Folder of existing audio tracks")
    pack.add_argument("--title", help="Override title; otherwise inferred from local tags/folder")
    pack.add_argument("--author", help="Override author; otherwise inferred from local tags")
    pack.add_argument("--output", help="M4B destination (defaults to input folder name in current directory)")
    pack.add_argument("--cover", help="Override cover with a local JPG/PNG file")
    pack.add_argument("--no-auto-cover", action="store_true", help="Disable cover auto-detection")
    inspect = subs.add_parser("inspect", help="Inspect an existing local audio file")
    inspect.add_argument("file")
    sample = subs.add_parser("sample", help="Generate and verify synthetic sample M4B")
    sample.add_argument("--output", default="./hpl-test-sample", help="Directory for test audio and M4B")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            print(f"HPL v{VERSION}\nPython: {sys.version.split()[0]}\nPlatform: {sys.platform}")
            for name in ("ffmpeg", "ffprobe"):
                print(f"{name}: {shutil.which(name) or 'NOT FOUND'}")
            print("HPL account: not required; no login or protected-audio extraction")
            print("Audiobookshelf connection: not required")
        elif args.command == "info":
            data = fetch_public_listing(args.url) if args.fetch else hoopla_book_url(args.url)
            print(json.dumps(data, indent=2, ensure_ascii=False))
            if args.save:
                destination = Path(args.save).expanduser()
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("x", encoding="utf-8") as file:
                    json.dump(data, file, indent=2, ensure_ascii=False)
                    file.write("\n")
                print(f"Saved: {destination}")
        elif args.command == "preview":
            print(json.dumps(folder_preview(args.source), indent=2, ensure_ascii=False))
        elif args.command == "pack":
            output = args.output or f"./{safe_filename(args.title or Path(args.source).name)}.m4b"
            package_audio(args.source, output, args.title, args.author, args.cover, not args.no_auto_cover)
        elif args.command == "inspect":
            print(json.dumps(inspect_file(args.file), indent=2, ensure_ascii=False))
        elif args.command == "sample":
            do_sample(args.output)
    except (ToolError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
