#!/usr/bin/env python3
"""Fill xSchedule AudioFile overrides from each fseq's media file.

Quit xSchedule before running with --write. It keeps the schedule in memory,
and the next Save replaces xlights.xschedule.

    python tools/fix_xschedule_audio.py --show "<show directory>" [--media DIR ...] [--write]

The default is a dry run. --write copies the schedule into
show/backup/media-path-fix-<timestamp>/ and then sets AudioFile on each
PLIFSEQ and PLIFSEQVideo item to the audio file found on this machine.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac")
ITEM_TAGS = ("PLIFSEQVideo", "PLIFSEQ")
SCHEDULE_NAME = "xlights.xschedule"

# Mac File Provider (~/Library/CloudStorage/Dropbox), legacy ~/Dropbox,
# "Dropbox (Personal)" / "Dropbox-Personal", and the same shapes with a
# Windows drive letter and a different user profile.
_DROPBOX_PREFIX = re.compile(
    r"^(?:[A-Za-z]:)?/Users/[^/]+/(?:Library/CloudStorage/)?"
    r"Dropbox(?: \([^)]+\)|-[^/]+)?/",
    re.IGNORECASE,
)
# The slash of a self-closing "/>" must stay out of the attribute text. A greedy
# match swallows it, and AudioFile was then inserted between that slash and ">".
_ITEM_TAG = re.compile(r"<(" + "|".join(ITEM_TAGS) + r")\b([^<>]*?)(/?)>")
_ATTR = re.compile(r"""([A-Za-z_][\w.-]*)\s*=\s*(["'])(.*?)\2""", re.DOTALL)


@dataclass
class ResolveResult:
    path: Path | None = None
    matches: list[Path] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.path is not None:
            return "found"
        if self.matches:
            return "ambiguous"
        return "missing"


@dataclass
class ItemResult:
    fseq_name: str
    old_audio: str
    status: str
    new_audio: str = ""
    matches: list[str] = field(default_factory=list)


def _norm(stored: str) -> str:
    return stored.replace("\\", "/")


def _is_absolute(stored: str) -> bool:
    norm = _norm(stored)
    return norm.startswith("/") or norm.startswith("//") or bool(re.match(r"^[A-Za-z]:", norm))


def strip_dropbox_prefix(stored: str) -> str | None:
    """Return the path inside the Dropbox folder, or None if this is not one."""
    match = _DROPBOX_PREFIX.match(_norm(stored))
    if match is None:
        return None
    return match.group(0) and _norm(stored)[match.end():]


def xml_unescape(value: str) -> str:
    return (
        value.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def discover_dropbox_roots(home: Path | None = None) -> list[Path]:
    """Local Dropbox folders: CloudStorage on macOS, Dropbox* in the user profile."""
    roots: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if not path.is_dir():
            return
        try:
            key = path.resolve()
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        roots.append(key)

    home = home if home is not None else Path.home()
    cloud = home / "Library" / "CloudStorage"
    if cloud.is_dir():
        for entry in cloud.iterdir():
            if entry.name.lower().startswith("dropbox"):
                add(entry)
    profile = os.environ.get("USERPROFILE")
    bases = [home]
    if profile:
        bases.append(Path(profile))
    for base in bases:
        if not base.is_dir():
            continue
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.lower().startswith("dropbox"):
                add(entry)
    return roots


def _join(root: Path, remainder: str) -> Path | None:
    parts = [part for part in remainder.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return root.joinpath(*parts)


def _existing_file(path: Path) -> Path | None:
    try:
        if path.is_file():
            return path.resolve()
    except OSError:
        return None
    return None


class Finder:
    def __init__(self, show: Path, media_dirs: list[Path], dropbox_roots: list[Path] | None = None):
        self.show = show.resolve()
        self.media = []
        for directory in media_dirs:
            try:
                if directory.is_dir():
                    self.media.append(directory.resolve())
            except OSError:
                continue
        self.dropbox = list(dropbox_roots) if dropbox_roots is not None else discover_dropbox_roots()
        self._by_name: dict[str, list[Path]] | None = None

    def by_name(self, filename: str) -> list[Path]:
        if self._by_name is None:
            index: dict[str, list[Path]] = {}
            for root in [self.show, *self.media]:
                self._index(root, index)
            self._by_name = index
        return list(self._by_name.get(filename.casefold(), []))

    def _index(self, root: Path, index: dict[str, list[Path]]) -> None:
        if not root.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name.casefold() != "backup"]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                bucket = index.setdefault(name.casefold(), [])
                if resolved not in bucket:
                    bucket.append(resolved)


def _unique(candidates: list[Path]) -> ResolveResult | None:
    found: list[Path] = []
    for candidate in candidates:
        if candidate is None:
            continue
        existing = _existing_file(candidate)
        if existing is not None and existing not in found:
            found.append(existing)
    if len(found) == 1:
        return ResolveResult(path=found[0])
    if len(found) > 1:
        return ResolveResult(matches=found)
    return None


def _anchored(root: Path, remainder: str) -> list[Path]:
    """If remainder contains root's folder name, try the path after that name."""
    parts = [part for part in remainder.split("/") if part]
    name = root.name.casefold()
    hits: list[Path] = []
    for index, part in enumerate(parts):
        if part.casefold() != name:
            continue
        joined = _join(root, "/".join(parts[index + 1:]))
        if joined is not None:
            hits.append(joined)
    return hits


def resolve_stored_path(stored: str, finder: Finder, filename_fallback: bool = True) -> ResolveResult:
    """Find stored on this machine.

    Show-folder hits win over a media directory, which wins over another
    Dropbox root, so a copy elsewhere in the home folder is not a second match.
    A filename is used only when no structured path exists, and only inside
    the show and media directories.
    """
    stored = stored.strip()
    if not stored:
        return ResolveResult()

    direct = _existing_file(Path(stored))
    if direct is None:
        direct = _existing_file(Path(_norm(stored)))
    if direct is not None:
        return ResolveResult(path=direct)

    remainder = strip_dropbox_prefix(stored)
    relative = remainder if remainder is not None else (None if _is_absolute(stored) else _norm(stored).lstrip("/"))

    def structured(roots: list[Path]) -> list[Path]:
        if relative is None:
            return []
        candidates: list[Path] = []
        for root in roots:
            joined = _join(root, relative)
            if joined is not None:
                candidates.append(joined)
            candidates.extend(_anchored(root, relative))
        return candidates

    for roots in ([finder.show], finder.media, finder.dropbox):
        unique = _unique(structured(roots))
        if unique is not None:
            return unique

    if not filename_fallback:
        return ResolveResult()
    filename = Path(_norm(stored)).name
    if not filename or filename in (".", ".."):
        return ResolveResult()
    matches = finder.by_name(filename)
    if len(matches) == 1:
        return ResolveResult(path=matches[0])
    if len(matches) > 1:
        return ResolveResult(matches=matches)
    return ResolveResult()


def read_media_filename(fseq_path: Path) -> str | None:
    """Read the inline fseq 'mf' header. Channel data is not touched.

    Variable headers start at the uint16 stored at offset 8 and stop at the
    channel-data offset stored at offset 4. That is the fixed-header length
    for v1 and the post-sparse-range offset for v2.
    """
    try:
        with open(fseq_path, "rb") as handle:
            prefix = handle.read(10)
            if len(prefix) < 10 or prefix[0:4] != b"PSEQ":
                return None
            channel_offset = int.from_bytes(prefix[4:6], "little")
            variable_start = int.from_bytes(prefix[8:10], "little")
            if channel_offset <= variable_start or variable_start < 10:
                return None
            handle.seek(0)
            header = handle.read(channel_offset)
    except OSError:
        return None

    position = variable_start
    limit = min(channel_offset, len(header))
    while position + 4 <= limit:
        length = int.from_bytes(header[position:position + 2], "little")
        if length < 4 or position + length > limit:
            break
        code = header[position + 2:position + 4]
        if code == b"mf":
            data = header[position + 4:position + length]
            end = data.find(b"\x00")
            if end >= 0:
                data = data[:end]
            if not data:
                return None
            return data.decode("utf-8", errors="replace")
        position += length
    return None


def audio_by_stem(fseq_path: Path, finder: Finder) -> ResolveResult:
    """One audio file with the fseq's stem, beside it or under the show."""
    stem = fseq_path.stem.casefold()
    matches: list[Path] = []
    parent = fseq_path.parent
    search_dirs = [parent]
    if finder.show not in search_dirs:
        search_dirs.append(finder.show)
    seen: set[Path] = set()
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = [name for name in dirnames if name.casefold() != "backup"]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix.casefold() not in AUDIO_EXTS:
                    continue
                if path.stem.casefold() != stem:
                    continue
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                matches.append(resolved)
            # The fseq's own directory is not walked into subfolders; the show is.
            if directory == parent and directory != finder.show:
                break
    if len(matches) == 1:
        return ResolveResult(path=matches[0])
    if len(matches) > 1:
        return ResolveResult(matches=matches)
    return ResolveResult()


def find_audio(fseq_path: Path, finder: Finder) -> ResolveResult:
    media = read_media_filename(fseq_path)
    if media:
        return resolve_stored_path(media, finder)
    return audio_by_stem(fseq_path, finder)


def _detach_unquoted_slash(inner: str) -> tuple[str, bool]:
    """Drop a self-closing slash that an earlier run left among the attributes.

    Slashes inside quoted paths are kept. The return is (attributes, had_slash).
    """
    kept: list[str] = []
    quote = ""
    found = False
    for char in inner:
        if quote:
            kept.append(char)
            if char == quote:
                quote = ""
            continue
        if char in ('"', "'"):
            quote = char
            kept.append(char)
            continue
        if char == "/":
            found = True
            continue
        kept.append(char)
    return "".join(kept), found


def _parse_attrs(inner: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR.finditer(inner):
        attrs[match.group(1)] = xml_unescape(match.group(3))
    return attrs


def _set_audio_attribute(inner: str, audio_path: str) -> str:
    escaped = xml_escape(audio_path)
    replaced = False

    def replacer(match: re.Match[str]) -> str:
        nonlocal replaced
        if match.group(1) != "AudioFile":
            return match.group(0)
        replaced = True
        quote = match.group(2)
        return f"AudioFile={quote}{escaped}{quote}"

    updated = _ATTR.sub(replacer, inner)
    if replaced:
        return updated
    trailing = re.search(r"\s*$", updated)
    assert trailing is not None
    head = updated[:trailing.start()]
    tail = updated[trailing.start():]
    return f'{head} AudioFile="{escaped}"{tail}'


def _fseq_label(stored: str, resolved: Path | None) -> str:
    if resolved is not None:
        return resolved.name
    name = Path(_norm(stored)).name
    return name or "(unknown)"


def apply_schedule(xml_text: str, finder: Finder) -> tuple[str, list[ItemResult]]:
    """Return the schedule text with AudioFile filled in, and one result per item."""
    results: list[ItemResult] = []
    pieces: list[str] = []
    cursor = 0
    for match in _ITEM_TAG.finditer(xml_text):
        pieces.append(xml_text[cursor:match.start()])
        name = match.group(1)
        inner, stray_slash = _detach_unquoted_slash(match.group(2))
        slash = "/" if stray_slash or match.group(3) == "/" else ""
        attrs = _parse_attrs(inner)
        old_audio = attrs.get("AudioFile", "")
        fseq_stored = attrs.get("FSEQFile", "")
        label = _fseq_label(fseq_stored, None)
        result = ItemResult(fseq_name=label, old_audio=old_audio, status="not_found")

        def emit(attribute_text: str) -> str:
            return f"<{name}{attribute_text}{slash}>"

        # A stale override must not count as present just because some other
        # file shares its name. Only a path that exists as stored is kept.
        # A slash left in front of AudioFile still has to be moved back to "/>".
        if old_audio and resolve_stored_path(old_audio, finder, filename_fallback=False).path is not None:
            if stray_slash:
                result.status = "update"
                result.new_audio = old_audio
                pieces.append(emit(inner))
            else:
                result.status = "unchanged"
                result.new_audio = old_audio
                pieces.append(match.group(0))
            results.append(result)
            cursor = match.end()
            continue

        fseq = resolve_stored_path(fseq_stored, finder)
        if fseq.status != "found":
            result.fseq_name = _fseq_label(fseq_stored, None)
            if fseq.status == "ambiguous":
                result.status = "ambiguous"
                result.matches = [str(path) for path in fseq.matches]
            else:
                result.status = "no_fseq"
            results.append(result)
            pieces.append(match.group(0) if not stray_slash else emit(inner))
            cursor = match.end()
            continue

        result.fseq_name = fseq.path.name
        audio = find_audio(fseq.path, finder)
        if audio.status == "found":
            assert audio.path is not None
            result.status = "update"
            result.new_audio = str(audio.path)
            inner = _set_audio_attribute(inner, result.new_audio)
        elif audio.status == "ambiguous":
            result.status = "ambiguous"
            result.matches = [str(path) for path in audio.matches]
        else:
            result.status = "not_found"
        results.append(result)
        pieces.append(emit(inner) if result.status == "update" or stray_slash else match.group(0))
        cursor = match.end()
    pieces.append(xml_text[cursor:])
    return "".join(pieces), results


def format_report(results: list[ItemResult]) -> str:
    lines: list[str] = []
    for result in results:
        old = result.old_audio or "(blank)"
        if result.status == "update":
            written = result.new_audio
        elif result.status == "unchanged":
            written = "(unchanged)"
        elif result.status == "ambiguous":
            written = "(ambiguous)"
        elif result.status == "no_fseq":
            written = "(fseq not found)"
        else:
            written = "(not found)"
        lines.append(f"{result.fseq_name}  old={old}  audio={written}")
        for match in result.matches:
            lines.append(f"  {match}")
    return "\n".join(lines)


def _backup(show: Path, schedule: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = show / "backup" / f"media-path-fix-{stamp}"
    suffix = 0
    while destination.exists():
        suffix += 1
        destination = show / "backup" / f"media-path-fix-{stamp}-{suffix}"
    destination.mkdir(parents=True)
    shutil.copy2(schedule, destination / SCHEDULE_NAME)
    return destination


def update_show(show: Path, media_dirs: list[Path], write: bool, dropbox_roots: list[Path] | None = None) -> tuple[list[ItemResult], Path | None]:
    show = show.resolve()
    schedule = show / SCHEDULE_NAME
    if not show.is_dir():
        raise FileNotFoundError(f"Show directory does not exist: {show}")
    if not schedule.is_file():
        raise FileNotFoundError(f"Schedule file does not exist: {schedule}")

    try:
        with open(schedule, "r", encoding="utf-8", newline="") as handle:
            original = handle.read()
    except UnicodeDecodeError as error:
        raise ValueError(f"{schedule} is not UTF-8: {error}") from error
    finder = Finder(show, media_dirs, dropbox_roots)
    updated, results = apply_schedule(original, finder)
    backup = None
    if write and updated != original:
        backup = _backup(show, schedule)
        with open(schedule, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
    return results, backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set xSchedule AudioFile overrides to the audio files on this machine."
    )
    parser.add_argument("--show", required=True, help="Show directory containing xlights.xschedule")
    parser.add_argument(
        "--media",
        action="append",
        default=[],
        help="Extra media directory to search. Repeat for each xLights media folder.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Update xlights.xschedule. Quit xSchedule first. Default is a dry run.",
    )
    args = parser.parse_args(argv)
    try:
        results, backup = update_show(Path(args.show), [Path(path) for path in args.media], args.write)
    except (FileNotFoundError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    report = format_report(results)
    if report:
        print(report)
    if args.write:
        if backup is None:
            print("No changes.")
        else:
            print(f"Updated {SCHEDULE_NAME}. Previous copy: {backup}")
            print("Start xSchedule again so it reloads the overrides.")
    else:
        print("Dry run. Pass --write to update the schedule.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
