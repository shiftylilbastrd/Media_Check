"""Issue-detection logic, ported from the old media-import-check bash script.

Ported 1:1 (no behavior changes):
  - no/missing audio track
  - missing English audio (foreign-only)
  - Dolby Vision without an HDR10 fallback
  - runtime far short of the listed runtime (possibly truncated/wrong release)
  - suspiciously small file size for its runtime
  - broken/zero-filled file detection (head-of-file zero check + ffprobe)
  - ffmpeg decode test over the first N seconds

NOT YET PORTED - needs the real source first, not a guess:
  - "DD+ 7.1 built without an AC-3 core" detection (the old script imported
    eac3_layout() from SABnzbd's sab_media_gate.py) and its auto-fix
    (fix_dd71.py, which muxes in a Shield-compatible DD+ 5.1 track). Those two
    files' exact logic were never pasted into this project, and I'd rather
    leave this check out than guess at ffmpeg args/stream-selection logic for
    something that's supposed to run unattended and rewrite media files.
    See README "Known gaps".
"""
import os
import subprocess
import time

RUNTIME_MIN_RATIO = 0.60
MIN_MB_PER_MIN = 5
DECODE_SECONDS = 300


def runtime_minutes_from_arr_string(rt):
    """Radarr/Sonarr mediaInfo.runTime is like "1:43:40" or "53:45"."""
    if not rt:
        return 0
    try:
        parts = [int(p) for p in str(rt).split(":")]
    except ValueError:
        return 0
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        return 0
    return h * 60 + m + (1 if s >= 30 else 0)


def check_media_info(media_info, size_bytes, listed_runtime_min, expect_languages):
    """Mirrors the old bash check_file(). Returns a list of (category, reason).
    expect_languages: e.g. ["eng"] or ["eng", "jpn"] for anime."""
    problems = []
    audio_streams = media_info.get("audioStreamCount", 0) or 0
    codec = media_info.get("audioCodec") or ""
    langs = (media_info.get("audioLanguages") or "").lower()
    dyn_range = media_info.get("videoDynamicRangeType") or ""
    actual_runtime = runtime_minutes_from_arr_string(media_info.get("runTime"))

    if audio_streams == 0 or not codec:
        problems.append(("no_audio", "no audio track"))

    if langs and audio_streams:
        have_expected = any(f"/{lang}/" in f"/{langs}/" for lang in expect_languages)
        if not have_expected and "und" not in langs:
            problems.append((
                "no_english_audio",
                f"no English audio (has: {langs}) - foreign-language only, "
                f"needs a dual-audio release",
            ))

    if dyn_range == "DV":
        problems.append(("dv_no_hdr10", "Dolby Vision without HDR10 fallback"))

    if listed_runtime_min and actual_runtime:
        if (actual_runtime < listed_runtime_min * RUNTIME_MIN_RATIO
                and listed_runtime_min - actual_runtime > 15):
            problems.append((
                "runtime",
                f"runtime {actual_runtime} min vs listed {listed_runtime_min} min - "
                f"possibly truncated or wrong release",
            ))

    if actual_runtime and size_bytes:
        mb_per_min = size_bytes / 1048576 / actual_runtime
        if mb_per_min < MIN_MB_PER_MIN:
            problems.append((
                "small_size", f"only {mb_per_min:.1f} MB/min - suspiciously small",
            ))

    return problems


def broken_reason(host_path):
    """Zero-filled-start or unreadable-video check. Safe 1:1 port - no
    external script dependency, just file bytes + ffprobe."""
    if not os.path.isfile(host_path) or os.path.getsize(host_path) == 0:
        return None  # missing file = path/mount problem, never "broken"

    head = b""
    for attempt in range(2):
        with open(host_path, "rb") as f:
            head = f.read(16)
        if head != b"\x00" * 16:
            break
        if attempt == 0:
            time.sleep(5)  # transient zero-read guard: may still be mid-copy
    if head == b"\x00" * 16:
        return "file starts with zeros - incomplete or fake download"

    combined = ""
    for attempt in range(2):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
             "-of", "csv=p=0", host_path],
            capture_output=True, text=True, timeout=60,
        )
        combined = (out.stdout or "") + (out.stderr or "")
        bad = any(s in combined for s in
                  ("Invalid data found", "EBML header parsing failed", "moov atom not found"))
        if not bad:
            return None
        if attempt == 0:
            time.sleep(5)
    return f"not a readable video ({combined.strip()[:160]})"


def decode_test(host_path, seconds=DECODE_SECONDS):
    """ffmpeg decode test over the first N seconds. Returns an error string,
    or None if it looks fine (or the test is disabled/file missing)."""
    if seconds <= 0 or not os.path.isfile(host_path):
        return None
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-t", str(seconds), "-i", host_path,
         "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
        capture_output=True, text=True, timeout=seconds + 30,
    )
    lines = [
        line for line in (out.stderr or "").splitlines()
        if line.strip() and "non monotonically increasing dts" not in line
    ]
    return lines[0][:160] if lines else None
