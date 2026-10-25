import tempfile
import unittest
from pathlib import Path
from shutil import which

from hoopla_tools import (ToolError, hoopla_book_url, natural_key, public_metadata_from_html,
                          make_chapter_metadata, package_audio, inspect_file, do_sample)


class UrlAndMetadataTests(unittest.TestCase):
    def test_hoopla_public_url(self):
        result = hoopla_book_url("https://www.hoopladigital.com/audiobook/kindred-octavia-e-butler/13507414")
        self.assertEqual(result["id"], "13507414")
        self.assertEqual(result["media_type"], "audiobook")

    def test_rejects_non_hoopla_and_other_titles(self):
        for url in ["https://bad.example/audiobook/thing/123", "http://www.hoopladigital.com/audiobook/a/123",
                    "https://www.hoopladigital.com/my/hoopla", "https://www.hoopladigital.com/ebook/a/123"]:
            with self.subTest(url=url), self.assertRaises(ToolError):
                hoopla_book_url(url)

    def test_public_metadata(self):
        sample = '<html><head><title>Kindred Audiobook by Octavia E. Butler | hoopla</title>' \
                 '<meta property="og:image" content="https://example.org/cover.jpg"></head></html>'
        result = public_metadata_from_html(sample, {"id": "13507414"})
        self.assertEqual(result["title"], "Kindred")
        self.assertEqual(result["author"], "Octavia E. Butler")
        self.assertEqual(result["cover_url"], "https://example.org/cover.jpg")

    def test_natural_sorting(self):
        paths = [Path("part10.mp3"), Path("part2.mp3"), Path("part1.mp3")]
        self.assertEqual([p.name for p in sorted(paths, key=natural_key)], ["part1.mp3", "part2.mp3", "part10.mp3"])

    def test_chapters(self):
        raw = make_chapter_metadata("Book", "Author", ["Chapter A", "Chapter B"], [1000, 2000])
        self.assertIn("START=1000\nEND=3000", raw)
        self.assertEqual(raw.count("[CHAPTER]"), 2)

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


if __name__ == "__main__":
    unittest.main()
