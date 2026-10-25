import tempfile
import unittest
from pathlib import Path
from shutil import which

from hpl import (ToolError, hpl_book_url, natural_key, public_metadata_from_html,
                          make_chapter_metadata, package_audio, inspect_file, do_sample,
                 clean_chapter_name, folder_preview, infer_metadata, catalog_folder,
                 capability_report, embedded_chapters)


class UrlAndMetadataTests(unittest.TestCase):
    def test_hoopla_public_url(self):
        result = hpl_book_url("https://www.hoopladigital.com/audiobook/kindred-octavia-e-butler/13507414")
        self.assertEqual(result["id"], "13507414")
        self.assertEqual(result["media_type"], "audiobook")

    def test_rejects_non_hoopla_and_other_titles(self):
        for url in ["https://bad.example/audiobook/thing/123", "http://www.hoopladigital.com/audiobook/a/123",
                    "https://www.hoopladigital.com/my/hoopla", "https://www.hoopladigital.com/ebook/a/123"]:
            with self.subTest(url=url), self.assertRaises(ToolError):
                hpl_book_url(url)

    def test_public_metadata(self):
        sample = '<html><head><title>Kindred Audiobook by Octavia E. Butler | hoopla</title>' \
                 '<meta property="og:image" content="https://example.org/cover.jpg">' \
                 '<script type="application/ld+json">{"@type":"Audiobook","name":"Kindred",' \
                 '"duration":"PT10H55M","readBy":[{"name":"Kim Staunton"}]}</script></head></html>'
        result = public_metadata_from_html(sample, {"id": "13507414"})
        self.assertEqual(result["title"], "Kindred")
        self.assertEqual(result["author"], "Octavia E. Butler")
        self.assertEqual(result["cover_url"], "https://example.org/cover.jpg")
        self.assertEqual(result["narrator"], "Kim Staunton")
        self.assertEqual(result["duration_seconds"], 39300)

    def test_capabilities_do_not_claim_download_support(self):
        result = capability_report()
        self.assertTrue(result["metadata"]["supported"])
        self.assertFalse(result["hoopla_download"]["supported"])
        self.assertEqual(result["hoopla_download"]["status"], "blocked")

    def test_natural_sorting(self):
        paths = [Path("part10.mp3"), Path("part2.mp3"), Path("part1.mp3")]
        self.assertEqual([p.name for p in sorted(paths, key=natural_key)], ["part1.mp3", "part2.mp3", "part10.mp3"])

    def test_chapters(self):
        raw = make_chapter_metadata("Book", "Author", ["Chapter A", "Chapter B"], [1000, 2000])
        self.assertIn("START=1000\nEND=3000", raw)
        self.assertEqual(raw.count("[CHAPTER]"), 2)

    def test_embedded_chapters_are_scaled_to_encoded_duration(self):
        from unittest.mock import patch
        details = {"format": {"duration": "10"}, "chapters": [
            {"start_time": "0", "end_time": "4", "tags": {"title": "First"}},
            {"start_time": "4", "end_time": "10", "tags": {"title": "Second"}},
        ]}
        with patch("hpl.probe_details", return_value=details):
            self.assertEqual(embedded_chapters(Path("book.m4b"), 9000),
                             [(0, 3600, "First"), (3600, 9000, "Second")])

    @unittest.skipUnless(which("ffmpeg") and which("ffprobe"), "FFmpeg and FFprobe required")
    def test_end_to_end_sample(self):
        with tempfile.TemporaryDirectory() as d:
            do_sample(d)
            data = inspect_file(Path(d) / "Sample Audiobook.m4b")
            self.assertEqual(len(data["chapters"]), 3)
            self.assertEqual(data["tags"].get("title"), "Sample Audiobook")

    @unittest.skipUnless(which("ffmpeg") and which("ffprobe"), "FFmpeg and FFprobe required")
    def test_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            do_sample(d)
            with self.assertRaises(ToolError):
                package_audio(Path(d) / "parts", Path(d) / "Sample Audiobook.m4b", "duplicate")




class HplEnhancements(unittest.TestCase):
    def test_cleanup_keeps_numeric_book_titles(self):
        self.assertEqual(clean_chapter_name("01 - Prologue"), "Prologue")
        self.assertEqual(clean_chapter_name("002.Chapter 2"), "Chapter 2")
        self.assertEqual(clean_chapter_name("1984"), "1984")
        self.assertEqual(clean_chapter_name("1000 Ways"), "1000 Ways")

    @unittest.skipUnless(which("ffmpeg") and which("ffprobe"), "FFmpeg required")
    def test_auto_metadata_and_preview(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "Parts"
            folder.mkdir()
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=1", "-metadata", "album=Tagged Title",
                            "-metadata", "artist=Tagged Author", str(folder / "01 - Introduction.mp3")], check=True)
            info = infer_metadata(folder, list(folder.iterdir()))
            self.assertEqual(info["title"], "Tagged Title")
            self.assertEqual(info["author"], "Tagged Author")
            preview = folder_preview(folder)
            self.assertEqual(preview["chapter_names"], ["Introduction"])
            dest = Path(d) / "result.m4b"
            package_audio(folder, dest)
            tags = inspect_file(dest)
            self.assertEqual(tags["tags"]["title"], "Tagged Title")
            self.assertEqual(tags["chapters"][0]["title"], "Introduction")

    @unittest.skipUnless(which("ffmpeg") and which("ffprobe"), "FFmpeg required")
    def test_cover_auto_detection_and_opt_out(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "parts"
            folder.mkdir()
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=1", str(folder / "01.mp3")], check=True)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "color=c=red:s=40x40:d=1", "-frames:v", "1", "-update", "1",
                            str(folder / "cover.png")], check=True)
            with_cover = Path(d) / "cover.m4b"
            package_audio(folder, with_cover)
            self.assertTrue(any(s["type"] == "video" for s in inspect_file(with_cover)["streams"]))
            no_cover = Path(d) / "no-cover.m4b"
            package_audio(folder, no_cover, auto_cover=False)
            self.assertFalse(any(s["type"] == "video" for s in inspect_file(no_cover)["streams"]))

    @unittest.skipUnless(which("ffmpeg") and which("ffprobe"), "FFmpeg required")
    def test_embedded_cover_keeps_input_untouched(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "parts"
            folder.mkdir()
            image = Path(d) / "art.jpg"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "color=c=blue:s=40x40:d=1", "-frames:v", "1", "-update", "1",
                            str(image)], check=True)
            audio = folder / "01.mp3"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=1", "-i", str(image),
                            "-map", "0:a:0", "-map", "1:v:0", "-c:a", "libmp3lame",
                            "-c:v", "mjpeg", "-disposition:v", "attached_pic", str(audio)], check=True)
            packaged = Path(d) / "book.m4b"
            before = audio.stat().st_size
            package_audio(folder, packaged)
            self.assertTrue(any(s["type"] == "video" for s in inspect_file(packaged)["streams"]))
            self.assertEqual(audio.stat().st_size, before)
            self.assertFalse((folder / "cover.png").exists())

    @unittest.skipUnless(which("ffmpeg") and which("ffprobe"), "FFmpeg required")
    def test_catalog_duplicates(self):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "source"
            source.mkdir()
            one = source / "01 - One.wav"
            two = source / "02 - Two.wav"
            for frequency, path in ((440, one), (550, two)):
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                                "-i", f"sine=frequency={frequency}:duration=1", str(path)], check=True)
            duplicate = root / "duplicate.wav"
            shutil.copyfile(one, duplicate)
            data = catalog_folder(root)
            self.assertEqual(data["file_count"], 3)
            self.assertEqual(len(data["duplicate_groups"]), 1)

if __name__ == "__main__":
    unittest.main()
