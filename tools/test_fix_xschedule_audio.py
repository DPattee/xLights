#!/usr/bin/env python3
"""Tests for tools/fix_xschedule_audio.py."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fix_xschedule_audio as tool


MAC = "/Users/alice/Library/CloudStorage/Dropbox/Christmas"
OPTIONS = '<Options Foo="keep me"/>'


def build_fseq(media: str | None, *, blocks: int = 0, major: int = 2) -> bytes:
    """Minimal PSEQ header. Extra byte after the channel offset stands in for channel data."""
    payload = b""
    if media is not None:
        payload = media.encode("utf-8") + b"\x00"
    variable_start = (32 + blocks * 8) if major >= 2 else 28
    record = 4 + len(payload) if payload else 0
    channel_offset = variable_start + record
    channel_offset = (channel_offset + 3) & ~3
    buffer = bytearray(channel_offset + 1)
    buffer[0:4] = b"PSEQ"
    buffer[4:6] = channel_offset.to_bytes(2, "little")
    buffer[7] = major
    buffer[8:10] = variable_start.to_bytes(2, "little")
    buffer[18] = 50
    if major >= 2:
        buffer[21] = blocks & 0xFF
    if payload:
        buffer[variable_start:variable_start + 2] = record.to_bytes(2, "little")
        buffer[variable_start + 2:variable_start + 4] = b"mf"
        buffer[variable_start + 4:variable_start + 4 + len(payload)] = payload
    buffer[channel_offset] = 0xAB
    return bytes(buffer)


def write_schedule(show: Path, body: str) -> Path:
    schedule = show / "xlights.xschedule"
    schedule.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<xSchedule>\n"
        f"{body}\n"
        f"  {OPTIONS}\n"
        "</xSchedule>\n",
        encoding="utf-8",
        newline="\n",
    )
    return schedule


def audio_attr(schedule: Path, tag: str = "PLIFSEQ") -> str | None:
    text = schedule.read_text(encoding="utf-8")
    match = tool._ITEM_TAG.search(text)
    if match is None or match.group(1) != tag:
        # Fall through to the requested tag when several items are present.
        for found in tool._ITEM_TAG.finditer(text):
            if found.group(1) == tag:
                match = found
                break
        else:
            return None
    assert match is not None
    return tool._parse_attrs(match.group(2)).get("AudioFile")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.show = Path(self.tmp.name) / "Christmas"
        (self.show / "music").mkdir(parents=True)
        self.audio = self.show / "music" / "song.mp3"
        self.audio.write_bytes(b"audio")
        self.finder = tool.Finder(self.show, [], dropbox_roots=[])

    def tearDown(self):
        self.tmp.cleanup()

    def test_mac_cloudstorage_windows_and_personal_prefixes(self):
        stored = [
            f"{MAC}/music/song.mp3",
            r"C:\Users\bob\Dropbox\Christmas\music\song.mp3",
            "/Users/alice/Dropbox/Christmas/music/song.mp3",
            "/Users/alice/Dropbox (Personal)/Christmas/music/song.mp3",
            "/Users/alice/Library/CloudStorage/Dropbox-Personal/Christmas/music/song.mp3",
        ]
        for path in stored:
            with self.subTest(path=path):
                found = tool.resolve_stored_path(path, self.finder)
                self.assertEqual(found.status, "found")
                self.assertEqual(found.path, self.audio.resolve())

    def test_two_filename_matches_are_not_chosen(self):
        (self.show / "a").mkdir()
        (self.show / "b").mkdir()
        (self.show / "a" / "dupe.mp3").write_bytes(b"a")
        (self.show / "b" / "dupe.mp3").write_bytes(b"b")
        found = tool.resolve_stored_path(
            "/Users/alice/Library/CloudStorage/Dropbox/NotHere/dupe.mp3",
            self.finder,
        )
        self.assertEqual(found.status, "ambiguous")
        self.assertEqual(len(found.matches), 2)

    def test_filename_fallback_when_show_folder_name_differs(self):
        other = Path(self.tmp.name) / "ShowDir"
        other.mkdir()
        only = other / "only.fseq"
        only.write_bytes(b"fseq")
        finder = tool.Finder(other, [], dropbox_roots=[])
        found = tool.resolve_stored_path(
            "/Users/alice/Library/CloudStorage/Dropbox/Other/only.fseq",
            finder,
        )
        self.assertEqual(found.path, only.resolve())

    def test_mf_header_ignores_channel_byte(self):
        media = f"{MAC}/music/song.mp3"
        for kwargs in ({}, {"blocks": 1}, {"major": 1}):
            with self.subTest(**kwargs):
                blob = build_fseq(media, **kwargs)
                self.assertEqual(blob[-1], 0xAB)
                path = self.show / "header.fseq"
                path.write_bytes(blob)
                self.assertEqual(tool.read_media_filename(path), media)
        empty = self.show / "empty.fseq"
        empty.write_bytes(build_fseq(None, major=1))
        self.assertIsNone(tool.read_media_filename(empty))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.show = Path(self.tmp.name) / "Christmas"
        (self.show / "music").mkdir(parents=True)
        self.audio = self.show / "music" / "song.mp3"
        self.audio.write_bytes(b"audio")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, write: bool = True):
        return tool.update_show(self.show, [], write, dropbox_roots=[])

    def test_mac_fseq_path_sets_audio_override(self):
        fseq = self.show / "song.fseq"
        fseq.write_bytes(build_fseq(f"{MAC}/music/song.mp3"))
        other = self.show / "music" / "keep.mp3"
        other.write_bytes(b"keep")
        kept = f'<PLIFSEQ FSEQFile="{MAC}/other.fseq" AudioFile="{other}" Name="kept" Delay="0"/>'
        # other.fseq is not required: the existing AudioFile resolves, so the item is left alone.
        write_schedule(
            self.show,
            "  <PlayList Name=\"Main\">\n"
            f'    <PLIFSEQ FSEQFile="{MAC}/song.fseq" Name="song" Delay="0"/>\n'
            f"    {kept}\n"
            "    <PLIFSEQVideo "
            f'FSEQFile="{MAC}/song.fseq" Name="video" Delay="0"/>\n'
            "  </PlayList>",
        )
        schedule = self.show / "xlights.xschedule"
        before_kept = kept
        results, backup = self._run(write=True)
        text = schedule.read_text(encoding="utf-8")
        self.assertIn(before_kept, text)
        self.assertIn(OPTIONS, text)
        self.assertIsNotNone(backup)
        self.assertTrue((backup / "xlights.xschedule").is_file())
        written = tool._parse_attrs(tool._ITEM_TAG.search(text).group(2))["AudioFile"]
        self.assertEqual(Path(written), self.audio.resolve())
        video = None
        for match in tool._ITEM_TAG.finditer(text):
            if match.group(1) == "PLIFSEQVideo":
                video = tool._parse_attrs(match.group(2)).get("AudioFile")
        self.assertEqual(Path(video), self.audio.resolve())
        self.assertIn("&amp;", tool.xml_escape("a&b"))
        statuses = {result.fseq_name: result.status for result in results}
        self.assertEqual(statuses["song.fseq"], "update")
        self.assertEqual(statuses["other.fseq"], "unchanged")

        second, second_backup = self._run(write=True)
        self.assertIsNone(second_backup)
        self.assertTrue(all(result.status != "update" for result in second))

    def test_existing_audio_file_is_left_unchanged(self):
        keep = self.show / "keep.mp3"
        keep.write_bytes(b"keep")
        fseq = self.show / "song.fseq"
        fseq.write_bytes(build_fseq(f"{MAC}/music/song.mp3"))
        schedule = write_schedule(
            self.show,
            f'  <PLIFSEQ FSEQFile="{fseq}" AudioFile="{keep}" Name="song" Delay="0"/>',
        )
        original = schedule.read_bytes()
        results, backup = self._run(write=True)
        self.assertIsNone(backup)
        self.assertEqual(schedule.read_bytes(), original)
        self.assertEqual(results[0].status, "unchanged")
        self.assertEqual(results[0].old_audio, str(keep))

    def test_ambiguous_audio_is_not_written(self):
        (self.show / "a").mkdir()
        (self.show / "b").mkdir()
        (self.show / "a" / "dupe.mp3").write_bytes(b"a")
        (self.show / "b" / "dupe.mp3").write_bytes(b"b")
        fseq = self.show / "tune.fseq"
        fseq.write_bytes(build_fseq("/Users/alice/Library/CloudStorage/Dropbox/NotHere/dupe.mp3"))
        schedule = write_schedule(
            self.show,
            f'  <PLIFSEQ FSEQFile="{fseq}" Name="tune" Delay="0"/>',
        )
        original = schedule.read_text(encoding="utf-8")
        results, backup = self._run(write=True)
        self.assertIsNone(backup)
        self.assertEqual(schedule.read_text(encoding="utf-8"), original)
        self.assertNotIn("AudioFile", original)
        self.assertEqual(results[0].status, "ambiguous")
        self.assertEqual(len(results[0].matches), 2)

    def test_v1_fseq_uses_stem_beside_the_sequence(self):
        fseq = self.show / "carol.fseq"
        fseq.write_bytes(build_fseq(None, major=1))
        beside = self.show / "carol.mp3"
        beside.write_bytes(b"mp3")
        write_schedule(
            self.show,
            f'  <PLIFSEQ FSEQFile="{fseq}" Name="song" Delay="0"/>',
        )
        results, _backup = self._run(write=True)
        self.assertEqual(results[0].status, "update")
        self.assertEqual(Path(results[0].new_audio), beside.resolve())

    def test_ampersand_in_resolved_path_is_escaped(self):
        folder = self.show / "music & more"
        folder.mkdir()
        audio = folder / "song.mp3"
        audio.write_bytes(b"audio")
        fseq = self.show / "amp.fseq"
        stored = "/Users/alice/Library/CloudStorage/Dropbox/Christmas/music & more/song.mp3"
        fseq.write_bytes(build_fseq(stored))
        schedule = write_schedule(
            self.show,
            f'  <PLIFSEQ FSEQFile="{fseq}" Name="amp" Delay="0"/>',
        )
        self._run(write=True)
        text = schedule.read_text(encoding="utf-8")
        self.assertIn("music &amp; more", text)
        self.assertNotIn("music & more", text.split("AudioFile", 1)[1])
        written = audio_attr(schedule)
        self.assertEqual(Path(written), audio.resolve())

    def test_stale_audio_override_is_replaced(self):
        fseq = self.show / "song.fseq"
        fseq.write_bytes(build_fseq(f"{MAC}/music/song.mp3"))
        stale = "/Users/alice/Dropbox/Nope/song.mp3"
        write_schedule(
            self.show,
            f'  <PLIFSEQ FSEQFile="{fseq}" AudioFile="{stale}" Name="song" Delay="0"/>',
        )
        results, backup = self._run(write=True)
        self.assertIsNotNone(backup)
        self.assertEqual(results[0].status, "update")
        self.assertEqual(results[0].old_audio, stale)
        self.assertEqual(Path(results[0].new_audio), self.audio.resolve())

    def test_dry_run_does_not_write(self):
        fseq = self.show / "song.fseq"
        fseq.write_bytes(build_fseq(f"{MAC}/music/song.mp3"))
        schedule = write_schedule(
            self.show,
            f'  <PLIFSEQ FSEQFile="{MAC}/song.fseq" Name="song" Delay="0"/>',
        )
        original = schedule.read_bytes()
        results, backup = self._run(write=False)
        self.assertIsNone(backup)
        self.assertEqual(schedule.read_bytes(), original)
        self.assertEqual(results[0].status, "update")
        self.assertEqual(Path(results[0].new_audio), self.audio.resolve())

    def test_missing_schedule_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            tool.update_show(self.show, [], False, dropbox_roots=[])


if __name__ == "__main__":
    unittest.main()
