from flask import Flask, render_template, jsonify, send_from_directory, request, redirect, abort
import os
import random
import sqlite3
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from dotenv import load_dotenv
import threading
import re
import unicodedata
import uuid
import shutil

load_dotenv()

# systemd gives us a pipe, so stdout is block-buffered and print() output can
# sit unseen for a long time. Line-buffer it so worker logs reach journalctl.
sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_POSTER_SIZES = {"w92", "w185", "w342", "w500"}
# Poster paths looked up from TMDB, keyed by IMDb id: (poster_path or "", time).
# Hits are kept for the process lifetime; misses are retried after an hour.
_poster_cache = {}
_poster_cache_lock = threading.Lock()
POSTER_MISS_TTL = 3600
QBITTORRENT_URL = os.getenv("QBITTORRENT_URL", "http://localhost:8080").rstrip("/")
QBITTORRENT_USERNAME = os.getenv("QBITTORRENT_USERNAME", "")
QBITTORRENT_PASSWORD = os.getenv("QBITTORRENT_PASSWORD", "")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
METADATA_FILE = os.path.join(PROJECT_DIR, "movies_db.json")
# Clips are encoded here, then moved into Movies/ only once all six exist, so
# the game never sees a half-encoded or failed movie folder.
STAGING_DIR = os.path.join(PROJECT_DIR, ".encoding")
CLIP_NAMES = [f"{s}s.mp4" for s in range(1, 7)]
MOVIES_DB_FILE = os.getenv("MOVIES_DB_PATH", os.path.join(PROJECT_DIR, "data", "movies.db"))

# Vote-count bands used when selecting titles from the local CS50 database.
# The upper bound is exclusive so a title belongs to exactly one band.
DIFFICULTY_BANDS = {
    "hard": (100_000, 250_000),
    "medium": (250_000, 500_000),
    "easy": (500_000, None),
}



def _difficulty_for_votes(votes):
    """Map a vote count to its difficulty band, or None if it fits none."""
    if not isinstance(votes, int):
        return None
    for name, (low, high) in DIFFICULTY_BANDS.items():
        if votes >= low and (high is None or votes < high):
            return name
    return None


def _env_float(name, default):
    try:
        return max(0.0, float(os.getenv(name, default)))
    except ValueError:
        return float(default)


# Free space the downloader always leaves on the disk. A torrent only starts
# if it fits on top of this reserve (counting what running downloads still
# need), so the library grows until the disk is nearly full and refills as
# played movies are deleted.
DISK_RESERVE = int(_env_float("DISK_RESERVE_GB", 5) * 1024 ** 3)
MIN_FREE_ENCODE = 2 * 1024 ** 3     # 2 GB before running ffmpeg
CRITICAL_FREE = 1 * 1024 ** 3       # 1 GB -> abort active download immediately
# Assumed size of a release whose size the indexer didn't report.
UNKNOWN_RELEASE_BYTES = 4 * 1024 ** 3

# Workers download continuously while there is room. Three keeps the library
# filling while limiting simultaneous torrent and ffmpeg load.
try:
    DOWNLOAD_WORKERS = max(1, min(int(os.getenv("DOWNLOAD_WORKERS", "3")), 8))
except ValueError:
    DOWNLOAD_WORKERS = 3
# Set AUTO_DOWNLOAD=0 to run the game without downloading anything (tests).
AUTO_DOWNLOAD = os.getenv("AUTO_DOWNLOAD", "1").strip().lower() not in ("0", "false", "no", "off")

SPACE_POLL = 60          # seconds between checks while the disk is full
IDLE_POLL = 600          # seconds to wait when no movie is left to download
SKIP_TTL = 24 * 3600     # a movie that failed is not retried for this long
MAX_ERROR_BACKOFF = 600  # cap on the wait after errors (e.g. qBittorrent down)

# ---------------------------------------------------------------------------
# Download state
# ---------------------------------------------------------------------------

# sanitized name -> {"difficulty": str, "outstanding": bytes still to download}
active_downloads = {}
active_downloads_lock = threading.Lock()
# sanitized name -> time it failed; skipped by the picker until SKIP_TTL passes
skipped_movies = {}
metadata_lock = threading.RLock()
selection_lock = threading.Lock()

# ---------------------------------------------------------------------------
# qBittorrent helpers
# ---------------------------------------------------------------------------

def _resolve_search_link(url):
    """Turn HTML result pages (e.g. LimeTorrents) into a direct torrent/magnet link."""
    if not url or not url.startswith("http"):
        return url

    # LimeTorrents gives us a .html page; scrape it for the real download link.
    if "limetorrents" in url.lower() and url.lower().endswith(".html"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            # Look for a magnet link first
            magnet_match = re.search(r'href="(magnet:[^"]+)"', html)
            if magnet_match:
                print(f"Resolved LimeTorrents page to magnet: {magnet_match.group(1)[:80]}...")
                return magnet_match.group(1)

            # Otherwise look for a direct .torrent link
            torrent_match = re.search(r'href="([^"]+\.torrent[^"]*)"', html)
            if torrent_match:
                torrent_url = urllib.parse.urljoin(url, torrent_match.group(1))
                print(f"Resolved LimeTorrents page to torrent: {torrent_url[:100]}...")
                return torrent_url

            print(f"Could not find torrent/magnet on LimeTorrents page: {url}")
        except Exception as exc:
            print(f"Failed to resolve LimeTorrents page {url}: {exc}")

    return url


def _build_opener():
    """Return a cookie-aware opener, logged in to qBittorrent if configured.

    Credentials come from QBITTORRENT_USERNAME / QBITTORRENT_PASSWORD. When
    they are unset the opener relies on qBittorrent's localhost auth bypass.
    """
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    if QBITTORRENT_USERNAME:
        body = _api_request(
            opener,
            "/api/v2/auth/login",
            data={"username": QBITTORRENT_USERNAME, "password": QBITTORRENT_PASSWORD},
        )
        if body.strip() != "Ok.":
            raise RuntimeError("qBittorrent login failed; check QBITTORRENT_USERNAME/QBITTORRENT_PASSWORD")
    return opener


def _api_request(opener, path, data=None, method=None):
    url = f"{QBITTORRENT_URL}{path}"
    encoded_data = None
    if data is not None:
        encoded_data = urllib.parse.urlencode(data).encode("utf-8")

    if method == "GET" and encoded_data is not None:
        url = f"{url}?{encoded_data.decode('utf-8')}"
        req = urllib.request.Request(url, method=method)
    else:
        req = urllib.request.Request(url, data=encoded_data, method=method)

    try:
        with opener.open(req) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        raise RuntimeError(f"HTTP {e.code} for {url}: {body}") from e


def start_search(opener, pattern):
    body = _api_request(
        opener,
        "/api/v2/search/start",
        data={"pattern": pattern, "plugins": "enabled", "category": "all"},
    )
    result = json.loads(body)
    search_id = result.get("id")
    if search_id is None:
        raise RuntimeError(f"Unexpected start_search response: {body}")
    return search_id


def get_results(opener, search_id, limit=0, offset=0):
    body = _api_request(
        opener,
        "/api/v2/search/results",
        data={"id": search_id, "limit": str(limit), "offset": str(offset)},
        method="GET",
    )
    data = json.loads(body)
    return data.get("results", []), data.get("status", "")


def stop_and_delete_search(opener, search_id):
    for action in ("stop", "delete"):
        try:
            _api_request(
                opener,
                f"/api/v2/search/{action}",
                data={"id": search_id},
            )
        except RuntimeError:
            pass


# Release-name tokens that mark a non-English release. Matched as whole tokens
# (case-insensitive), so "ITA" never matches "Vita" and "Rus" never "Russell".
NON_ENGLISH_TOKENS = {
    "spanish", "español", "espanol", "castellano", "latino", "esp", "spa",
    "french", "truefrench", "subfrench", "vf", "vff", "vfq", "vostfr",
    "german", "deutsch", "ger",
    "italian", "italiano", "ita",
    "hindi", "tamil", "telugu", "malayalam", "kannada",
    "russian", "rus",
    "portuguese", "português", "portugues", "dublado", "legendado",
    "polish", "pl", "lektor",
    "turkish", "türkçe", "turkce",
    "korean", "kor", "japanese", "jap", "chinese", "mandarin", "cantonese",
    "arabic", "persian", "farsi", "dutch", "swedish", "danish", "norwegian",
    "finnish", "czech", "hungarian", "greek", "thai", "vietnamese", "indonesian",
    "ukrainian", "hebrew",
}
DUB_TOKENS = {"dubbed", "dub", "multi", "multisub", "dual"}
ENGLISH_TOKENS = {"english", "eng"}
# Quality multipliers applied to seeders, so a slightly less-seeded 1080p
# release beats a DVDRip, and camera rips are a last resort.
QUALITY_WEIGHTS = (
    # 4K files are several times larger and add nothing to six short clips.
    ({"2160p", "4k", "uhd"}, 0.5),
    ({"cam", "camrip", "hdcam", "ts", "hdts", "telesync", "tc", "telecine", "hdtc"}, 0.25),
    ({"dvdscr", "scr", "screener"}, 0.5),
    ({"dvdrip", "dvd"}, 0.9),
    ({"1080p"}, 1.25),
    ({"720p"}, 1.15),
)


def _release_tokens(text):
    return [t for t in re.split(r"[\W_]+", (text or "").lower()) if t]


def _non_title_tokens(file_name, title):
    """Release-name tokens left after removing one occurrence of each title
    token, so a film like "The Italian Job" isn't mistaken for an Italian release."""
    tokens = _release_tokens(file_name)
    for title_token in _release_tokens(title):
        if title_token in tokens:
            tokens.remove(title_token)
    return tokens


def _non_english_reason(file_name, title):
    tokens = set(_non_title_tokens(file_name, title))
    if tokens & ENGLISH_TOKENS:
        return None
    language = tokens & NON_ENGLISH_TOKENS
    if language:
        return f"language tag {sorted(language)}"
    dub = tokens & DUB_TOKENS
    if dub:
        return f"dub tag {sorted(dub)}"
    return None


def _result_score(result, title):
    seeders = result.get("nbSeeders", 0) or 0
    tokens = set(_non_title_tokens(result.get("fileName", ""), title))
    for markers, weight in QUALITY_WEIGHTS:
        if tokens & markers:
            return seeders * weight
    return seeders


# Anything smaller is a sample, ebook, or other non-movie file.
MIN_RELEASE_BYTES = 300 * 1024 ** 2
LEADING_ARTICLES = {"the", "a", "an"}


def _match_tokens(text):
    """Lowercase, accent-free word tokens for comparing titles to release names."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(char for char in text if not unicodedata.combining(char)).lower()
    text = re.sub(r"['’]", "", text).replace("&", " and ")
    # Drop a leading "[group]" / "(site)" tag such as "[YTS.MX] Movie 2010".
    text = re.sub(r"^\s*[\[({][^\])}]*[\])}]\s*", "", text)
    return [token for token in re.split(r"[\W_]+", text) if token]


def _year_offset(file_name, title, year):
    """How far the release's year is from *year* if the release name starts
    with the title and is followed by a year; None if it doesn't match.

    Release names are "Title Year ..." ("Beautiful.Boy.2018.1080p"), so this
    rejects other films that merely contain the title words, like
    "The Most Beautiful Boy In The World (2021)" or "Beautiful Boy (2010)".
    """
    name = _match_tokens(file_name)
    wanted = _match_tokens(title)
    variants = [wanted]
    if len(wanted) > 1 and wanted[0] in LEADING_ARTICLES:
        variants.append(wanted[1:])  # "Lord of the Rings ..." for "The Lord of the Rings ..."
    for title_tokens in variants:
        if name[:len(title_tokens)] != title_tokens:
            continue
        if not year:
            return 0
        # Allow a few edition words ("Extended Cut") between title and year.
        for token in name[len(title_tokens):len(title_tokens) + 4]:
            if re.fullmatch(r"(19|20)\d\d", token):
                return abs(int(token) - int(year))
        return None
    return None


def _pick_best_result(results, title, year=""):
    """Choose the best English release of this exact film, or None.

    Only results named "<title> <year>" are considered; an exact year is
    preferred and an off-by-one year (festival vs. release date) is accepted
    only when no exact match exists. Returns None rather than falling back to
    a foreign, dead or wrong release: skipping one movie is better than burning
    a download slot on the wrong one.
    """
    by_offset = {0: [], 1: []}
    mismatched = 0
    for result in results:
        name = result.get("fileName", "")
        offset = _year_offset(name, title, year)
        if offset not in by_offset:
            mismatched += 1
            continue
        size = result.get("fileSize", -1)
        reason = _non_english_reason(name, title)
        if not reason and (result.get("nbSeeders", 0) or 0) < 1:
            reason = "no seeders"
        if not reason and isinstance(size, (int, float)) and 0 <= size < MIN_RELEASE_BYTES:
            reason = "too small to be the film"
        if reason:
            print(f"Skipping result for {title} ({year}): {name} ({reason})", file=sys.stderr, flush=True)
            continue
        by_offset[offset].append(result)
    if mismatched:
        print(f"Ignored {mismatched} results not named \"{title} {year}\"", file=sys.stderr, flush=True)
    candidates = by_offset[0] or by_offset[1]
    if not candidates:
        return None
    return max(candidates, key=lambda r: _result_score(r, title))


def _split_display_name(display_name):
    match = re.fullmatch(r"(.*) \((\d{4})\)", display_name or "")
    return (match.group(1), match.group(2)) if match else (display_name or "", "")


def _search_queries(title):
    """Search for the bare title: indexers match names like "Title.2018.1080p"
    poorly when the query contains "(2018)". Punctuation is dropped since
    release names rarely keep it; the raw title is a fallback."""
    simplified = unicodedata.normalize("NFKD", title)
    simplified = "".join(char for char in simplified if not unicodedata.combining(char))
    simplified = re.sub(r"['’]", "", simplified)
    simplified = " ".join(re.sub(r"[^\w\s]", " ", simplified).split())
    return list(dict.fromkeys(q for q in (simplified, title) if q))


def _search_all(opener, query):
    search_id = start_search(opener, query)
    all_results = []
    status = "Running"
    try:
        while status == "Running":
            # Results are returned from offset 0 each time, so keep the latest
            # full list rather than appending duplicates.
            results, status = get_results(opener, search_id)
            if results:
                all_results = results
            time.sleep(1)
    finally:
        stop_and_delete_search(opener, search_id)
    return all_results


def run_search(display_name):
    """Find a torrent for "Title (Year)".

    Returns ``(link, size_bytes)``, or ``(None, 0)`` if no suitable release.
    """
    title, year = _split_display_name(display_name)
    opener = _build_opener()
    for query in _search_queries(title):
        results = _search_all(opener, query)
        print(f"Search \"{query}\" returned {len(results)} results", file=sys.stderr, flush=True)
        best = _pick_best_result(results, title, year)
        if best is not None:
            raw_url = best.get("fileUrl", None)
            size = best.get("fileSize", -1)
            if not isinstance(size, (int, float)) or size <= 0:
                size = UNKNOWN_RELEASE_BYTES
            print(
                f"Best result for {display_name}: {best.get('fileName', 'unknown')} "
                f"({best.get('nbSeeders', 0)} seeders, {size / 1024 ** 3:.1f} GB)",
                flush=True,
            )
            link = _resolve_search_link(raw_url)
            if link:
                return link, int(size)
    print(f"No usable English release with seeders found for {display_name}", file=sys.stderr, flush=True)
    return None, 0


def _get_free_space(path):
    """Return free disk space for the drive containing *path* (bytes)."""
    return shutil.disk_usage(path).free


def _delete_torrent(opener, torrent_hash, delete_files=False):
    """Remove a torrent from qBittorrent."""
    if not torrent_hash:
        return
    try:
        _api_request(
            opener,
            "/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"},
        )
    except RuntimeError:
        pass


def _sanitize_dirname(name):
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, '_')
    return name.strip('. ')


def _get_video_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# Clip encoding
# ---------------------------------------------------------------------------

# Transfer functions used by HDR10 (PQ) and HLG sources.
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
# Browsers reliably decode only 8-bit 4:2:0 H.264 (High 10 and 4:4:4 mostly
# fail), so every clip is converted to this before encoding.
CLIP_PIX_FMT = "yuv420p"
# Map HDR to SDR so PQ/HLG sources don't look washed out in an SDR browser.
# zscale tags the frames as BT.709, which libx264 writes into the clip.
TONEMAP_FILTER = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
)
_zscale_available = None


def _has_zscale():
    """True if this ffmpeg build has the zscale filter needed to tone-map."""
    global _zscale_available
    if _zscale_available is None:
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-filters"],
                capture_output=True, text=True, check=True,
            )
            _zscale_available = bool(re.search(r"^\s*\S+\s+zscale\s", result.stdout, re.MULTILINE))
        except (subprocess.CalledProcessError, FileNotFoundError):
            _zscale_available = False
    return _zscale_available


def _probe_video_stream(path):
    """Return the first video stream's ffprobe fields, or {} on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,profile,pix_fmt,color_transfer",
                "-of", "json", path,
            ],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(result.stdout).get("streams") or []
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return {}
    return streams[0] if streams else {}


def _clip_filter(stream):
    """Per-clip filter that makes the output browser-playable 8-bit SDR.

    It runs after ``fps`` so only the frames kept in the clip are tone-mapped,
    not every frame of the source movie.
    """
    if stream.get("color_transfer") in HDR_TRANSFERS and _has_zscale():
        return f"{TONEMAP_FILTER},format={CLIP_PIX_FMT}"
    return f"format={CLIP_PIX_FMT}"


def _clip_output_args(path):
    return [
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", CLIP_PIX_FMT,
        # Put the index at the front so playback starts without fetching the end.
        "-movflags", "+faststart",
        path,
    ]


def _encode_clips(source, out_dir, duration):
    """Encode the 1s-6s clips from *source* into *out_dir* in a single pass.

    Raises CalledProcessError or FileNotFoundError if ffmpeg fails.
    """
    post = _clip_filter(_probe_video_stream(source))
    labels = [chr(ord('a') + i) for i in range(len(CLIP_NAMES))]
    chains = [
        f"[{label}]setpts=PTS/{duration / sec},fps=24,{post}[{label}1]"
        for label, sec in zip(labels, range(1, len(CLIP_NAMES) + 1))
    ]
    filter_complex = (
        f"[0:v]split={len(labels)}" + "".join(f"[{label}]" for label in labels) + ";" + ";".join(chains)
    )

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", source, "-filter_complex", filter_complex]
    for label, clip in zip(labels, CLIP_NAMES):
        cmd.extend(["-map", f"[{label}1]", *_clip_output_args(os.path.join(out_dir, clip))])
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def _mp4_has_faststart(path):
    """True if the MP4 ``moov`` index comes before the ``mdat`` media data."""
    try:
        with open(path, "rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return False
                size = int.from_bytes(header[:4], "big")
                kind = header[4:8]
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    size = int.from_bytes(f.read(8), "big")
                    f.seek(size - 16, os.SEEK_CUR)
                elif size == 0:
                    return False
                else:
                    f.seek(size - 8, os.SEEK_CUR)
    except OSError:
        return False


def _clip_problems(path):
    """List the reasons a finished clip may not play well in a browser."""
    stream = _probe_video_stream(path)
    problems = []
    if not stream:
        return ["unreadable"]
    if stream.get("codec_name") != "h264":
        problems.append(f"codec {stream.get('codec_name')}")
    if stream.get("pix_fmt") != CLIP_PIX_FMT:
        problems.append(f"pix_fmt {stream.get('pix_fmt')}")
    if stream.get("color_transfer") in HDR_TRANSFERS:
        problems.append(f"HDR transfer {stream.get('color_transfer')}")
    if not _mp4_has_faststart(path):
        problems.append("no faststart")
    return problems


def _repair_clip(path):
    """Fix a clip in place. Returns the problems found (empty if none)."""
    problems = _clip_problems(path)
    if not problems or problems == ["unreadable"]:
        return problems
    temporary = f"{path}.{uuid.uuid4().hex}.tmp.mp4"
    if problems == ["no faststart"]:
        # Only the index is in the wrong place, so a remux is enough.
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0:v:0", "-c", "copy",
               "-movflags", "+faststart", temporary]
    else:
        post = _clip_filter(_probe_video_stream(path))
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0:v:0", "-vf", post,
               *_clip_output_args(temporary)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return problems


def _repair_existing_clips():
    """Re-encode clips made by older versions that browsers can't play
    (10-bit, HDR) and add faststart to the rest."""
    movies_dir = os.path.join(PROJECT_DIR, "Movies")
    if not os.path.isdir(movies_dir):
        return
    fixed = 0
    for name in sorted(os.listdir(movies_dir)):
        for clip in CLIP_NAMES:
            path = os.path.join(movies_dir, name, clip)
            if not os.path.isfile(path):
                continue
            try:
                problems = _repair_clip(path)
            except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
                print(f"Could not repair clip {name}/{clip}: {exc}", file=sys.stderr)
                continue
            if problems:
                fixed += 1
                print(f"Repaired clip {name}/{clip}: {', '.join(problems)}")
    if fixed:
        print(f"Clip repair finished: {fixed} clips fixed.")


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _load_metadata():
    with metadata_lock:
        if os.path.exists(METADATA_FILE):
            with open(METADATA_FILE, "r", encoding="utf-8") as f:
                meta = json.load(f)
        else:
            meta = {}

        # Auto-populate any existing directories not yet in metadata
        movies_dir = os.path.join(PROJECT_DIR, "Movies")
        if os.path.exists(movies_dir):
            for d in os.listdir(movies_dir):
                dpath = os.path.join(movies_dir, d)
                if os.path.isdir(dpath) and d not in meta:
                    meta[d] = {
                        "title": d,
                        "year": "",
                        "poster_path": "",
                        "display_name": d,
                    }
            if meta and not os.path.exists(METADATA_FILE):
                _save_metadata(meta)

        return meta


def _save_metadata(meta):
    with metadata_lock:
        temporary_file = f"{METADATA_FILE}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary_file, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            os.replace(temporary_file, METADATA_FILE)
        finally:
            if os.path.exists(temporary_file):
                os.remove(temporary_file)


def _add_movie_metadata(sanitized_name, title, year, poster_path, votes=None, rating=None, difficulty=None, imdb_id=None):
    with metadata_lock:
        meta = _load_metadata()
        entry = {
            "title": title,
            "year": year,
            "poster_path": poster_path,
            "display_name": f"{title} ({year})" if year else title,
        }
        if votes is not None:
            entry["votes"] = votes
        if rating is not None:
            entry["rating"] = rating
        if difficulty:
            entry["difficulty"] = difficulty
        if imdb_id:
            entry["imdb_id"] = imdb_id
        meta[sanitized_name] = entry
        _save_metadata(meta)


# ---------------------------------------------------------------------------
# Downloaded-movie helpers
# ---------------------------------------------------------------------------

def _has_all_clips(path):
    return all(
        os.path.isfile(os.path.join(path, clip)) and os.path.getsize(os.path.join(path, clip)) > 0
        for clip in CLIP_NAMES
    )


def _get_downloaded_movies():
    movies_dir = os.path.join(PROJECT_DIR, "Movies")
    if not os.path.exists(movies_dir):
        return set()
    return {d for d in os.listdir(movies_dir) if _has_all_clips(os.path.join(movies_dir, d))}


def _cleanup_incomplete_movies():
    """Remove leftovers from interrupted or failed encodes.

    Runs once at startup, before any worker starts, so nothing is mid-encode.
    """
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    movies_dir = os.path.join(PROJECT_DIR, "Movies")
    if not os.path.isdir(movies_dir):
        return
    for name in os.listdir(movies_dir):
        path = os.path.join(movies_dir, name)
        if os.path.isdir(path) and not _has_all_clips(path):
            print(f"Removing incomplete movie folder: {name}", file=sys.stderr)
            _delete_movie(name)


def _get_playable_movies():
    """Downloaded movies that a new round may pick (finished ones excluded)."""
    with games_lock:
        retired = set(retired_movies)
    return _get_downloaded_movies() - retired


def _get_movie_count():
    return len(_get_playable_movies())


def _movie_difficulty(info):
    return info.get("difficulty") or _difficulty_for_votes(info.get("votes"))


def _downloaded_by_difficulty(meta=None):
    """Group playable movie folders by difficulty band."""
    if meta is None:
        meta = _load_metadata()
    groups = {name: [] for name in DIFFICULTY_BANDS}
    for movie_dir in _get_playable_movies():
        difficulty = _movie_difficulty(meta.get(movie_dir, {}))
        if difficulty in groups:
            groups[difficulty].append(movie_dir)
    return groups


def _delete_movie(sanitized_name):
    movie_path = os.path.join(PROJECT_DIR, "Movies", sanitized_name)
    if os.path.exists(movie_path):
        shutil.rmtree(movie_path)
    with metadata_lock:
        meta = _load_metadata()
        if sanitized_name in meta:
            del meta[sanitized_name]
            _save_metadata(meta)


# ---------------------------------------------------------------------------
# Game rounds
# ---------------------------------------------------------------------------

# Rounds are kept in memory; the app runs as a single process (the download
# queue relies on that too). The browser only gets an opaque game id, so the
# answer never reaches it before the round ends. A finished movie is retired
# at once and deleted only when no other round is still playing it.
GAME_TTL = 3 * 3600  # abandoned rounds stop pinning their movie after this
active_games = {}  # game_id -> {"movie_dir": str, "started": float}
retired_movies = set()
games_lock = threading.Lock()


def _delete_unused_retired_locked():
    """Expire stale rounds, then delete retired movies nobody is playing.

    Caller must hold ``games_lock``.
    """
    now = time.time()
    for game_id in [g for g, game in active_games.items() if now - game["started"] > GAME_TTL]:
        del active_games[game_id]
    in_play = {game["movie_dir"] for game in active_games.values()}
    for movie_dir in [m for m in retired_movies if m not in in_play]:
        retired_movies.discard(movie_dir)
        _delete_movie(movie_dir)


def _start_game(candidates):
    """Register a round for a random playable candidate; None if none left."""
    with games_lock:
        _delete_unused_retired_locked()
        candidates = [m for m in candidates if m not in retired_movies]
        if not candidates:
            return None, None
        movie_dir = random.choice(candidates)
        game_id = uuid.uuid4().hex
        active_games[game_id] = {"movie_dir": movie_dir, "started": time.time()}
    return game_id, movie_dir


def _game_movie(game_id):
    with games_lock:
        game = active_games.get(game_id) if isinstance(game_id, str) else None
        return game["movie_dir"] if game else None


def _finish_game(game_id):
    """End a round and retire its movie. Returns False if it already ended."""
    with games_lock:
        game = active_games.pop(game_id, None)
        if not game:
            return False
        retired_movies.add(game["movie_dir"])
        _delete_unused_retired_locked()
    return True


# ---------------------------------------------------------------------------
# Local CS50 movie database helpers
# ---------------------------------------------------------------------------

def _local_db_available():
    return os.path.isfile(MOVIES_DB_FILE)


def _open_local_movies_db():
    if not _local_db_available():
        raise FileNotFoundError(
            f"Local movies database not found at {MOVIES_DB_FILE}. "
            "Copy the CS50 movies.db into the project's data directory or set MOVIES_DB_PATH."
        )
    connection = sqlite3.connect(MOVIES_DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def _imdb_id(movie_id):
    """The CS50 database uses IMDb's numeric ids as movie ids."""
    return f"tt{int(movie_id):07d}"


def _imdb_id_for(info):
    """IMDb id for a metadata entry, looking it up by title/year for entries
    saved before ids were recorded."""
    if info.get("imdb_id"):
        return info["imdb_id"]
    if not info.get("title") or not _local_db_available():
        return None
    with _open_local_movies_db() as connection:
        row = connection.execute(
            "SELECT movies.id FROM movies LEFT JOIN ratings ON ratings.movie_id = movies.id "
            "WHERE movies.title = ? AND (? = '' OR CAST(movies.year AS TEXT) = ?) "
            "ORDER BY ratings.votes DESC LIMIT 1",
            (info["title"], str(info.get("year") or ""), str(info.get("year") or "")),
        ).fetchone()
    return _imdb_id(row["id"]) if row else None


def _tmdb_poster_path(imdb_id):
    """TMDB poster path for an IMDb id, or "" if there is none or TMDB is unreachable."""
    if not TMDB_API_KEY:
        return ""
    now = time.time()
    with _poster_cache_lock:
        cached = _poster_cache.get(imdb_id)
    if cached and (cached[0] or now - cached[1] < POSTER_MISS_TTL):
        return cached[0]

    url = f"https://api.themoviedb.org/3/find/{urllib.parse.quote(imdb_id)}?" + urllib.parse.urlencode(
        {"api_key": TMDB_API_KEY, "external_source": "imdb_id"}
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.load(response)
    except (OSError, ValueError) as exc:
        # Transient failure: don't cache, so the next request retries.
        print(f"TMDB poster lookup failed for {imdb_id}: {exc}", file=sys.stderr, flush=True)
        return ""
    results = data.get("movie_results") or []
    path = (results[0].get("poster_path") or "") if results else ""
    with _poster_cache_lock:
        _poster_cache[imdb_id] = (path, now)
    return path


def _poster_url(info, size="w500"):
    imdb_id = _imdb_id_for(info)
    return f"/poster/{imdb_id}?size={size}" if imdb_id else ""


def _pick_movie(connection, difficulty, excluded):
    """A random movie from *difficulty*'s vote band not in *excluded*, or None."""
    lower, upper = DIFFICULTY_BANDS[difficulty]
    rows = connection.execute(
        "SELECT movies.id, movies.title, movies.year, ratings.rating, ratings.votes "
        "FROM movies JOIN ratings ON ratings.movie_id = movies.id "
        "WHERE ratings.votes >= ? AND (? IS NULL OR ratings.votes < ?) "
        "ORDER BY RANDOM()",
        (lower, upper, upper),
    )
    for row in rows:
        title = row["title"] or ""
        if not title:
            continue
        year = str(row["year"]) if row["year"] is not None else ""
        display_name = f"{title} ({year})" if year else title
        if _sanitize_dirname(display_name) in excluded:
            continue
        return {
            "display_name": display_name,
            "title": title,
            "year": year,
            "poster_path": "",
            "imdb_id": _imdb_id(row["id"]),
            "votes": int(row["votes"]),
            "rating": row["rating"],
            "difficulty": difficulty,
        }
    return None


def _claim_next_movie():
    """Pick the next movie to download and register it as active.

    The difficulty with the fewest saved plus in-progress movies goes first,
    keeping easy, medium and hard roughly level. A band with nothing left to
    download is passed over. Returns None if every band is exhausted.
    """
    with selection_lock:
        counts = {name: len(dirs) for name, dirs in _downloaded_by_difficulty().items()}
        now = time.time()
        with active_downloads_lock:
            for job in active_downloads.values():
                counts[job["difficulty"]] += 1
            excluded = _get_downloaded_movies() | set(active_downloads)
            for name, failed_at in list(skipped_movies.items()):
                if now - failed_at > SKIP_TTL:
                    del skipped_movies[name]
            excluded |= set(skipped_movies)

        with _open_local_movies_db() as connection:
            for difficulty in sorted(counts, key=lambda d: (counts[d], random.random())):
                movie = _pick_movie(connection, difficulty, excluded)
                if movie:
                    with active_downloads_lock:
                        active_downloads[_sanitize_dirname(movie["display_name"])] = {
                            "difficulty": difficulty,
                            "outstanding": 0,
                        }
                    return movie
    return None


# ---------------------------------------------------------------------------
# Single-movie download + process
# ---------------------------------------------------------------------------

def _outstanding_bytes(exclude=None):
    """Bytes that running downloads (other than *exclude*) still need."""
    with active_downloads_lock:
        return sum(job["outstanding"] for name, job in active_downloads.items() if name != exclude)


def _has_room_for_download():
    """True if at least a small release fits above the disk reserve."""
    room = _get_free_space(PROJECT_DIR) - _outstanding_bytes() - DISK_RESERVE
    return room >= MIN_RELEASE_BYTES


def _claim_space(sanitized, size):
    """Reserve *size* bytes for a download if it fits above the disk reserve."""
    with active_downloads_lock:
        others = sum(job["outstanding"] for name, job in active_downloads.items() if name != sanitized)
        if _get_free_space(PROJECT_DIR) - others - size < DISK_RESERVE:
            return False
        if sanitized in active_downloads:
            active_downloads[sanitized]["outstanding"] = size
        return True


def _set_outstanding(sanitized, size):
    with active_downloads_lock:
        if sanitized in active_downloads:
            active_downloads[sanitized]["outstanding"] = max(0, int(size))


# Results of _download_and_process_movie.
DOWNLOAD_OK = "ok"
DOWNLOAD_NO_RELEASE = "no-release"  # nothing suitable to download
DOWNLOAD_NO_SPACE = "no-space"      # the release doesn't fit above the reserve yet
DOWNLOAD_FAILED = "failed"          # torrent, file or encode problem


def _download_and_process_movie(movie_name, on_ready=None):
    """Full pipeline for one movie. Returns one of the DOWNLOAD_* results.

    ``on_ready`` runs after all clips are encoded and just before the folder is
    published to Movies/, so metadata exists by the time a game can pick it.
    """
    if not movie_name:
        return DOWNLOAD_FAILED
    sanitized = _sanitize_dirname(movie_name)

    link, size = run_search(movie_name)
    if not link:
        print(f"No torrent link found for {movie_name}; aborting download.")
        return DOWNLOAD_NO_RELEASE

    # --- Disk check before download ---
    if not _claim_space(sanitized, size):
        print(
            f"Not downloading {movie_name}: {size / 1024 ** 3:.1f} GB would cut into the "
            f"{DISK_RESERVE / 1024 ** 3:.0f} GB disk reserve.",
            file=sys.stderr,
        )
        return DOWNLOAD_NO_SPACE

    print(f"Adding torrent for {movie_name}: {link[:100]}...")
    opener = _build_opener()
    tag = f"moviedle_{uuid.uuid4().hex}"

    # Add torrent
    try:
        _api_request(
            opener,
            "/api/v2/torrents/add",
            data={
                "urls": link,
                "savepath": PROJECT_DIR,
                "tags": tag,
            },
        )
    except RuntimeError:
        return DOWNLOAD_FAILED

    # Poll
    poll_interval = 2
    max_wait = 3600
    elapsed = 0
    torrent_hash = None
    content_path = None
    save_path = None
    torrent_name = None
    finished = False

    last_logged_progress = -1.0
    while elapsed < max_wait:
        # Critical disk check during download
        if _get_free_space(PROJECT_DIR) < CRITICAL_FREE:
            print(f"Aborting download for {movie_name}: critical disk space.", file=sys.stderr)
            _delete_torrent(opener, torrent_hash, delete_files=True)
            return DOWNLOAD_FAILED

        try:
            body = _api_request(
                opener,
                "/api/v2/torrents/info",
                data={"tag": tag},
                method="GET",
            )
        except RuntimeError:
            _delete_torrent(opener, torrent_hash, delete_files=True)
            return DOWNLOAD_FAILED

        torrents = json.loads(body)
        if not torrents:
            time.sleep(poll_interval)
            elapsed += poll_interval
            continue

        torrent = torrents[0]
        progress = torrent.get("progress", 0)
        torrent_state = torrent.get("state", "")
        torrent_hash = torrent.get("hash")
        content_path = torrent.get("content_path")
        save_path = torrent.get("save_path")
        torrent_name = torrent.get("name")
        total = torrent.get("size") or size
        _set_outstanding(sanitized, total * (1 - progress))

        # Log progress every ~10%
        if progress - last_logged_progress >= 0.1:
            print(f"Download progress for {movie_name}: {progress*100:.0f}% (state: {torrent_state})")
            last_logged_progress = progress

        if progress == 1.0:
            print(f"Download finished for {movie_name}")
            finished = True
            break
        if torrent_state in ("error", "missingFiles"):
            print(f"Torrent entered bad state for {movie_name}: {torrent_state}")
            _delete_torrent(opener, torrent_hash, delete_files=True)
            return DOWNLOAD_FAILED

        time.sleep(poll_interval)
        elapsed += poll_interval

    if not finished:
        _delete_torrent(opener, torrent_hash, delete_files=True)
        return DOWNLOAD_FAILED

    if not content_path and save_path and torrent_name:
        content_path = os.path.join(save_path, torrent_name)

    # Delete torrent without removing files (we still need them)
    _delete_torrent(opener, torrent_hash, delete_files=False)

    # Post-processing
    if not content_path or not os.path.exists(content_path):
        return DOWNLOAD_FAILED

    # --- Disk check before encoding ---
    if _get_free_space(PROJECT_DIR) < MIN_FREE_ENCODE:
        print(f"Skipping encode for {movie_name}: not enough free space.", file=sys.stderr)
        if os.path.isfile(content_path):
            os.remove(content_path)
        else:
            shutil.rmtree(content_path)
        return DOWNLOAD_FAILED

    content_path = os.path.normpath(content_path)
    video_files = []
    if os.path.isfile(content_path):
        if content_path.lower().endswith((".mp4", ".mkv")):
            video_files.append(content_path)
    else:
        for root, dirs, files in os.walk(content_path):
            for file in files:
                if file.lower().endswith((".mp4", ".mkv")):
                    video_files.append(os.path.join(root, file))

    if not video_files:
        if os.path.isfile(content_path):
            os.remove(content_path)
        else:
            shutil.rmtree(content_path)
        return DOWNLOAD_FAILED

    largest = max(video_files, key=os.path.getsize)
    final_dir = os.path.join(PROJECT_DIR, "Movies", sanitized)
    movies_dir = os.path.join(STAGING_DIR, sanitized)
    shutil.rmtree(movies_dir, ignore_errors=True)
    os.makedirs(movies_dir)

    try:
        duration = _get_video_duration(largest)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        shutil.rmtree(movies_dir, ignore_errors=True)
        if os.path.isfile(content_path):
            os.remove(content_path)
        else:
            shutil.rmtree(content_path)
        return DOWNLOAD_FAILED

    print(f"Starting ffmpeg encoding for {movie_name}...")
    try:
        _encode_clips(largest, movies_dir, duration)
        print(f"Finished ffmpeg encoding for {movie_name}")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = getattr(exc, "stderr", None) or exc
        print(f"ffmpeg encoding failed for {movie_name}: {str(detail).strip()[-500:]}")
        # Clean up partial clips and original files on encode failure
        shutil.rmtree(movies_dir, ignore_errors=True)
        if os.path.isfile(content_path):
            os.remove(content_path)
        else:
            shutil.rmtree(content_path)
        return DOWNLOAD_FAILED

    # Delete original downloaded files
    if content_path and os.path.exists(content_path):
        if os.path.isfile(content_path):
            os.remove(content_path)
        else:
            shutil.rmtree(content_path)

    if not _has_all_clips(movies_dir):
        print(f"ffmpeg produced incomplete clips for {movie_name}", file=sys.stderr)
        shutil.rmtree(movies_dir, ignore_errors=True)
        return DOWNLOAD_FAILED

    try:
        if on_ready:
            on_ready()
        os.makedirs(os.path.dirname(final_dir), exist_ok=True)
        shutil.rmtree(final_dir, ignore_errors=True)
        os.replace(movies_dir, final_dir)
    except Exception:
        shutil.rmtree(movies_dir, ignore_errors=True)
        raise

    return DOWNLOAD_OK


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _download_worker():
    """Keep downloading movies, lowest-stocked difficulty first, while the
    disk has room above DISK_RESERVE."""
    errors = 0
    while True:
        if not _has_room_for_download():
            time.sleep(SPACE_POLL)
            continue

        try:
            job = _claim_next_movie()
        except (OSError, sqlite3.Error) as exc:
            print(f"Could not pick a movie to download: {exc}", file=sys.stderr)
            time.sleep(IDLE_POLL)
            continue
        if job is None:
            time.sleep(IDLE_POLL)
            continue

        sanitized = _sanitize_dirname(job["display_name"])
        print(f"Worker picked up {job['difficulty']} movie: {job['display_name']}")
        try:
            result = _download_and_process_movie(
                job["display_name"],
                on_ready=lambda: _add_movie_metadata(
                    sanitized,
                    job["title"],
                    job["year"],
                    job.get("poster_path", ""),
                    votes=job.get("votes"),
                    rating=job.get("rating"),
                    difficulty=job.get("difficulty"),
                    imdb_id=job.get("imdb_id"),
                ),
            )
        except Exception as exc:
            # Usually qBittorrent is unreachable; back off rather than skip the movie.
            errors += 1
            delay = min(MAX_ERROR_BACKOFF, 15 * 2 ** errors)
            print(f"Download failed for {job['display_name']}: {exc}; retrying in {delay}s", file=sys.stderr)
            result = None
        else:
            errors = 0
            delay = 0
        finally:
            with active_downloads_lock:
                active_downloads.pop(sanitized, None)

        if result == DOWNLOAD_OK:
            print(f"Successfully downloaded and processed: {job['display_name']}")
        elif result in (DOWNLOAD_NO_RELEASE, DOWNLOAD_FAILED):
            print(f"Skipping {job['display_name']} for a day ({result})")
            with active_downloads_lock:
                skipped_movies[sanitized] = time.time()
        elif result == DOWNLOAD_NO_SPACE:
            delay = SPACE_POLL
        if delay:
            time.sleep(delay)


_cleanup_incomplete_movies()


def _run_clip_repair():
    try:
        _repair_existing_clips()
    except Exception as exc:  # never let a repair bug take down the app
        print(f"Clip repair failed: {exc}", file=sys.stderr)


threading.Thread(target=_run_clip_repair, name="moviedle-clip-repair", daemon=True).start()

if AUTO_DOWNLOAD:
    for worker_number in range(1, DOWNLOAD_WORKERS + 1):
        threading.Thread(
            target=_download_worker,
            name=f"moviedle-download-{worker_number}",
            daemon=True,
        ).start()


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/movie-count')
def movie_count():
    downloaded = _get_movie_count()
    with active_downloads_lock:
        downloading = len(active_downloads)
        downloading_by_difficulty = {name: 0 for name in DIFFICULTY_BANDS}
        for job in active_downloads.values():
            downloading_by_difficulty[job["difficulty"]] += 1
    by_difficulty = {name: len(dirs) for name, dirs in _downloaded_by_difficulty().items()}
    return jsonify({
        "downloaded": downloaded,
        "downloaded_by_difficulty": by_difficulty,
        "downloading": downloading,
        "downloading_by_difficulty": downloading_by_difficulty,
        "disk_full": AUTO_DOWNLOAD and not _has_room_for_download(),
        "workers": DOWNLOAD_WORKERS if AUTO_DOWNLOAD else 0,
    })


@app.route('/movies/list')
def movies_list():
    meta = _load_metadata()
    movies = []
    for key, info in meta.items():
        movies.append({
            "movie_dir": key,
            "display_name": info.get("display_name", key),
            "poster_path": info.get("poster_path"),
            "votes": info.get("votes"),
            "rating": info.get("rating"),
            "difficulty": info.get("difficulty"),
        })
    movies.sort(key=lambda m: m["display_name"])
    return jsonify(movies)


@app.route('/game/start')
def game_start():
    difficulty = (request.args.get("difficulty") or "").strip().lower()
    if difficulty and difficulty not in DIFFICULTY_BANDS:
        return jsonify({"error": f"Unknown difficulty: {difficulty}"}), 400

    meta = _load_metadata()
    if difficulty:
        dirs = sorted(_downloaded_by_difficulty(meta)[difficulty])
    else:
        dirs = sorted(_get_playable_movies())
    game_id, movie_dir = _start_game(dirs)
    if not game_id:
        label = f"{difficulty} movies" if difficulty else "movies"
        return jsonify({"error": f"No {label} available"}), 404

    # The title is deliberately left out: it is only sent once the round ends.
    return jsonify({
        "game_id": game_id,
        "clips": list(CLIP_NAMES),
        "difficulty": _movie_difficulty(meta.get(movie_dir, {})),
    })


@app.route('/game/<game_id>/clips/<clip>')
def game_clip(game_id, clip):
    movie_dir = _game_movie(game_id)
    if not movie_dir or clip not in CLIP_NAMES:
        abort(404)
    return send_from_directory(os.path.join(PROJECT_DIR, "Movies", movie_dir), clip)


@app.route('/game/clip-error', methods=["POST"])
def game_clip_error():
    """Log a clip the browser failed to play so it shows up in the journal."""
    data = request.get_json(silent=True) or {}
    movie_dir = _game_movie(data.get("game_id")) or "unknown game"
    clip = str(data.get("clip", ""))[:20]
    detail = " ".join(str(data.get("detail", "")).split())[:300]
    print(f"Browser could not play {movie_dir}/{clip}: {detail}", file=sys.stderr)
    return "", 204


@app.route('/game/guess', methods=["POST"])
def game_guess():
    data = request.get_json(silent=True) or {}
    game_id = data.get("game_id", "")
    guess = data.get("guess", "")

    if not game_id or not isinstance(guess, str) or not guess.strip():
        return jsonify({"error": "game_id and guess are required"}), 400

    movie_dir = _game_movie(game_id)
    info = _load_metadata().get(movie_dir) if movie_dir else None
    if not info:
        return jsonify({"error": "Game not found"}), 404

    if guess.strip().lower() != info["display_name"].lower():
        return jsonify({"correct": False})
    if not _finish_game(game_id):
        return jsonify({"error": "Game not found"}), 404
    return jsonify({"correct": True, "movie_name": info["display_name"], "poster_url": _poster_url(info)})


@app.route('/game/reveal', methods=["POST"])
def game_reveal():
    data = request.get_json(silent=True) or {}
    game_id = data.get("game_id", "")

    if not game_id:
        return jsonify({"error": "game_id is required"}), 400

    movie_dir = _game_movie(game_id)
    info = _load_metadata().get(movie_dir) if movie_dir else None
    if not info or not _finish_game(game_id):
        return jsonify({"error": "Game not found"}), 404
    return jsonify({"movie_name": info["display_name"], "poster_url": _poster_url(info)})


@app.route('/poster/<imdb_id>')
def poster(imdb_id):
    """Redirect to the TMDB poster image for an IMDb id, or 404 if none."""
    if not re.fullmatch(r"tt\d{7,10}", imdb_id):
        abort(404)
    size = request.args.get("size", "w185")
    if size not in TMDB_POSTER_SIZES:
        size = "w185"
    path = _tmdb_poster_path(imdb_id)
    if not path:
        abort(404)
    response = redirect(f"https://image.tmdb.org/t/p/{size}{path}")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route('/search-movies')
def search_movies():
    """Suggest titles from the local database, in the same "Title (Year)" form
    the game checks guesses against. Only titles from the playable vote pool
    are suggested, since nothing else can be an answer."""
    query = request.args.get('q', '').strip()
    # Allow typing a picked suggestion back in, e.g. "Inception (20".
    match = re.match(r"^(.*?)\s*\((\d{0,4})\)?$", query)
    year_prefix = ""
    if match and match.group(1):
        query, year_prefix = match.group(1), match.group(2)
    if not query or not _local_db_available():
        return jsonify([])

    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    min_votes = min(low for low, _ in DIFFICULTY_BANDS.values())
    sql = (
        "SELECT movies.id, movies.title, movies.year FROM ratings "
        "JOIN movies ON movies.id = ratings.movie_id "
        "WHERE ratings.votes >= ? AND movies.title LIKE ? ESCAPE '\\' "
        "AND (? = '' OR CAST(movies.year AS TEXT) LIKE ?) "
        "ORDER BY (movies.title LIKE ? ESCAPE '\\') DESC, ratings.votes DESC "
        "LIMIT 10"
    )
    with _open_local_movies_db() as connection:
        rows = connection.execute(
            sql, (min_votes, f"%{escaped}%", year_prefix, f"{year_prefix}%", f"{escaped}%")
        ).fetchall()

    movies = []
    for row in rows:
        year = str(row["year"]) if row["year"] is not None else ""
        display_name = f"{row['title']} ({year})" if year else row["title"]
        movies.append({"display_name": display_name, "imdb_id": _imdb_id(row["id"])})
    return jsonify(movies)


@app.route('/disk-status')
def disk_status():
    total, used, free = shutil.disk_usage(PROJECT_DIR)
    return jsonify({
        "total_gb": round(total / (1024 ** 3), 2),
        "used_gb": round(used / (1024 ** 3), 2),
        "free_gb": round(free / (1024 ** 3), 2),
        "reserve_gb": round(DISK_RESERVE / (1024 ** 3), 2),
        "low_space": free < DISK_RESERVE,
    })


if __name__ == '__main__':
    # The Werkzeug debugger allows arbitrary code execution, so it is opt-in
    # for local development only. Never enable it on a reachable server.
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=5000, debug=debug)
