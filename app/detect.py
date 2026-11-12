"""Issue-detection logic, ported from the old media-import-check bash script.

Ported 1:1 (no behavior changes):
  - no/missing audio track
  - missing English audio (foreign-only)
  - Dolby Vision without an HDR10 fallback
  - runtime far short of the listed runtime (possibly truncated/wrong release) -
    NOTE: inactive on Ryan's Radarr/Sonarr version, see check_media_info()
  - suspiciously small file size for its runtime - same caveat as above
  - broken/zero-filled file detection (head-of-file zero check + ffprobe
    format check) - runs on every file, every path
  - ffmpeg decode test over the first N seconds - only runs when
    broken_reason() is called with deep=True: the daily-poll fallback and
    the backlog scanner (both background threads) use it; the live webhook
    path stays shallow/fast on purpose, since that's a real HTTP response
    Radarr/Sonarr are waiting on and a 300s decode test has no place there

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
import shutil
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


def _normalize_langs(raw):
    """Radarr/Sonarr webhooks have been observed sending audioLanguages as a
    list (e.g. ["eng"]) on current versions. Tolerate an older/different
    version that sends a slash-delimited string instead, rather than assume
    either shape and crash on the other."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p.strip().lower() for p in raw.split("/") if p.strip()]
    return [str(p).strip().lower() for p in raw if str(p).strip()]


def check_media_info(media_info, size_bytes, listed_runtime_min, expect_languages):
    """Mirrors the old bash check_file(). Returns a list of (category, reason).
    expect_languages: e.g. ["eng"] or ["eng", "jpn"] for anime.

    audioStreamCount and runTime are NOT assumed present - a real webhook
    payload from Ryan's Radarr showed neither field in mediaInfo at all (only
    audioChannels/audioCodec/audioLanguages/video*). "Has audio" is judged
    from audioCodec being present instead; the runtime/small_size checks
    below simply don't fire when runTime is absent, same as before, rather
    than crash or guess."""
    problems = []
    codec = media_info.get("audioCodec") or ""
    langs = _normalize_langs(media_info.get("audioLanguages"))
    dyn_range = media_info.get("videoDynamicRangeType") or ""
    actual_runtime = runtime_minutes_from_arr_string(media_info.get("runTime"))

    if not codec:
        problems.append(("no_audio", "no audio track"))
    elif langs:
        have_expected = any(lang in langs for lang in expect_languages)
        if not have_expected and "und" not in langs:
            problems.append((
                "no_english_audio",
                f"no English audio (has: {'/'.join(langs)}) - foreign-language only, "
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


def broken_reason(host_path, deep=False):
    """Zero-filled-start, unreadable-container, and (deep=True only) a real
    ffmpeg decode test. Safe 1:1 port - no external script dependency, just
    file bytes + ffprobe/ffmpeg.

    `deep` controls whether decode_test() runs: it's a genuine ffmpeg decode
    over the first N seconds, CPU/disk-intensive (seconds-of-video, not
    milliseconds) and can take minutes on a large file under a busy array.
    webhooks.py calls this with deep=False (the default) to stay fast, since
    that path is a live HTTP response Radarr/Sonarr are waiting on. The
    scheduler's daily-poll fallback and the backlog scanner both run on their
    own background thread with nothing waiting on them, so they pass
    deep=True - the backlog scanner in particular exists specifically to run
    this real decode test against the library that predates this app."""
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
            break
        if attempt == 0:
            time.sleep(5)
    else:
        return f"not a readable video ({combined.strip()[:160]})"

    if deep:
        decode_err = decode_test(host_path)
        if decode_err:
            return f"decode error: {decode_err}"

    return None


def _niced(cmd):
    """Prefix a command with nice/ionice if they're available, so this
    workload yields CPU and disk I/O to everything else on the box (Plex
    transcodes, Radarr/Sonarr, the array itself) instead of competing with
    it. This matters a lot more now that the decode test runs continuously
    (queue + backlog-fill worker) rather than a few throttled files a day.
    Falls back to the bare command if either binary's missing - never an
    error, just no niceing."""
    prefix = []
    if shutil.which("ionice"):
        prefix += ["ionice", "-c3"]  # idle I/O class - only runs when nothing else wants the disk
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]  # lowest CPU scheduling priority
    return prefix + cmd if prefix else cmd


def decode_test(host_path, seconds=DECODE_SECONDS):
    """ffmpeg decode test over the first N seconds. Returns an error string,
    or None if it looks fine (or the test is disabled/file missing).

    Matches sab_media_gate.py's decode_errors() exactly: audio is mapped as
    "0:a:0?" (optional) so a file with genuinely no audio track doesn't
    misreport a decode error here - that case is already handled separately
    as the no_audio soft issue. Timeout multiplier (seconds*4+120, not just
    seconds+30) matches the original too, to tolerate a slow/busy array
    rather than timing out on a file that's just reading slowly."""
    if seconds <= 0 or not os.path.isfile(host_path):
        return None
    timeout = seconds * 4 + 120
    try:
        out = subprocess.run(
            _niced(["ffmpeg", "-nostdin", "-v", "error", "-t", str(seconds), "-i", host_path,
                    "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"]),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"decode test timed out after {timeout}s - slow read, or a very large/corrupt file"
    lines = [
        line for line in (out.stderr or "").splitlines()
        if line.strip() and "non monotonically increasing dts" not in line
    ]
    return lines[0][:160] if lines else None
