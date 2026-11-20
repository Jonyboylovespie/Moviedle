from flask import Flask, render_template, jsonify, send_from_directory, request
import requests
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
import uuid
from queue import Queue
import shutil

load_dotenv()

app = Flask(__name__)

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
TMDB_BASE_URL = "https://api.themoviedb.org/3"
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

# Disk-space thresholds (bytes)
MIN_FREE_DOWNLOAD = 5 * 1024 ** 3   # 5 GB before starting a download
MIN_FREE_ENCODE = 2 * 1024 ** 3     # 2 GB before running ffmpeg
CRITICAL_FREE = 1 * 1024 ** 3       # 1 GB -> abort active download immediately

# Three workers keeps the queue moving while limiting simultaneous torrent and
# ffmpeg resource usage. Set DOWNLOAD_WORKERS to tune this for the host.
try:
    DOWNLOAD_WORKERS = max(1, min(int(os.getenv("DOWNLOAD_WORKERS", "3")), 8))
except ValueError:
    DOWNLOAD_WORKERS = 3

# ---------------------------------------------------------------------------
# Thread-safe download queue / state
# ---------------------------------------------------------------------------

download_queue = Queue()
active_downloads = set()
active_downloads_lock = threading.Lock()
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
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _api_request(opener, path, data=None, method=None):
    url = f"http://localhost:8080{path}"
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


def run_search(pattern):
    opener = _build_opener()
    search_id = start_search(opener, pattern)
    all_results = []
    status = "Running"
    while status == "Running":
        results, status = get_results(opener, search_id)
        if results:
            all_results.extend(results)
        time.sleep(1)
    stop_and_delete_search(opener, search_id)

    if not all_results:
        print(f"No search results for pattern: {pattern}")
        return None

    best = max(all_results, key=lambda r: r.get("nbSeeders", 0))
    raw_url = best.get("fileUrl", None)
    resolved = _resolve_search_link(raw_url)
    print(f"Best result for {pattern}: {best.get('fileName', 'unknown')} ({best.get('nbSeeders', 0)} seeders)")
    return resolved


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


def _add_movie_metadata(sanitized_name, title, year, poster_path, votes=None, rating=None, difficulty=None):
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


def _get_movie_count():
    return len(_get_downloaded_movies())


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


def _pick_movies_not_downloaded(count, difficulties=None):
    """Pick *count* random movies for each requested vote-count difficulty."""
    if difficulties is None:
        difficulties = list(DIFFICULTY_BANDS)

    downloaded = _get_downloaded_movies()
    with active_downloads_lock:
        in_progress = set(active_downloads)
    # Jobs waiting in the queue have not reached ``active_downloads`` yet, so
    # include them while selecting the next batch to avoid duplicate downloads.
    with download_queue.mutex:
        queued = {
            _sanitize_dirname(job.get("display_name", ""))
            for job in list(download_queue.queue)
            if isinstance(job, dict) and job.get("display_name")
        }
    excluded = downloaded | in_progress | queued

    picked = []

    query = (
        "SELECT movies.title, movies.year, ratings.rating, ratings.votes "
        "FROM movies JOIN ratings ON ratings.movie_id = movies.id "
        "WHERE ratings.votes >= ? AND (? IS NULL OR ratings.votes < ?) "
        "ORDER BY RANDOM()"
    )

    with _open_local_movies_db() as connection:
        for difficulty in difficulties:
            lower, upper = DIFFICULTY_BANDS[difficulty]
            rows = connection.execute(query, (lower, upper, upper)).fetchall()
            candidates = []
            seen = set()
            for row in rows:
                title = row["title"] or ""
                year = str(row["year"]) if row["year"] is not None else ""
                if not title:
                    continue
                display_name = f"{title} ({year})" if year else title
                sanitized = _sanitize_dirname(display_name)
                if sanitized in excluded or sanitized in seen:
                    continue
                seen.add(sanitized)
                candidates.append({
                    "display_name": display_name,
                    "title": title,
                    "year": year,
                    "poster_path": "",
                    "votes": int(row["votes"]),
                    "rating": row["rating"],
                    "difficulty": difficulty,
                })
                if len(candidates) >= count:
                    break

            for movie in candidates[:count]:
                sanitized = _sanitize_dirname(movie["display_name"])
                excluded.add(sanitized)
                picked.append(movie)
    return picked


# ---------------------------------------------------------------------------
# Single-movie download + process
# ---------------------------------------------------------------------------

def _download_and_process_movie(movie_name, on_ready=None):
    """Full pipeline for one movie. Returns True on success.

    ``on_ready`` runs after all clips are encoded and just before the folder is
    published to Movies/, so metadata exists by the time a game can pick it.
    """
    if not movie_name:
        return False

    # --- Disk check before download ---
    if _get_free_space(PROJECT_DIR) < MIN_FREE_DOWNLOAD:
        print(f"Skipping {movie_name}: not enough free space for download.", file=sys.stderr)
        return False

    link = run_search(movie_name)
    if not link:
        print(f"No torrent link found for {movie_name}; aborting download.")
        return False

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
        return False

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
            return False

        try:
            body = _api_request(
                opener,
                "/api/v2/torrents/info",
                data={"tag": tag},
                method="GET",
            )
        except RuntimeError:
            _delete_torrent(opener, torrent_hash, delete_files=True)
            return False

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
            return False

        time.sleep(poll_interval)
        elapsed += poll_interval

    if not finished:
        _delete_torrent(opener, torrent_hash, delete_files=True)
        return False

    if not content_path and save_path and torrent_name:
        content_path = os.path.join(save_path, torrent_name)

    # Delete torrent without removing files (we still need them)
    _delete_torrent(opener, torrent_hash, delete_files=False)

    # Post-processing
    if not content_path or not os.path.exists(content_path):
        return False

    # --- Disk check before encoding ---
    if _get_free_space(PROJECT_DIR) < MIN_FREE_ENCODE:
        print(f"Skipping encode for {movie_name}: not enough free space.", file=sys.stderr)
        if os.path.isfile(content_path):
            os.remove(content_path)
        else:
            shutil.rmtree(content_path)
        return False

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
        return False

    largest = max(video_files, key=os.path.getsize)
    sanitized = _sanitize_dirname(movie_name)
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
        return False

    # Single-pass multi-output ffmpeg
    chains = []
    for i, sec in enumerate(range(1, 7), start=1):
        label = chr(ord('a') + i - 1)
        speed = duration / sec
        chains.append(f"[{label}]setpts=PTS/{speed},fps=24[{label}1]")

    filter_complex = f"[0:v]split=6[a][b][c][d][e][f];" + ";".join(chains)

    cmd = ["ffmpeg", "-y", "-i", largest, "-filter_complex", filter_complex]
    for i, sec in enumerate(range(1, 7), start=1):
        label = chr(ord('a') + i - 1)
        cmd.extend([
            "-an", "-r", "24", "-c:v", "libx264", "-preset", "ultrafast",
            "-map", f"[{label}1]", os.path.join(movies_dir, f"{sec}s.mp4")
        ])

    print(f"Starting ffmpeg encoding for {movie_name}...")
    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Finished ffmpeg encoding for {movie_name}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"ffmpeg encoding failed for {movie_name}")
        # Clean up partial clips and original files on encode failure
        shutil.rmtree(movies_dir, ignore_errors=True)
        if os.path.isfile(content_path):
            os.remove(content_path)
        else:
            shutil.rmtree(content_path)
        return False

    # Delete original downloaded files
    if content_path and os.path.exists(content_path):
        if os.path.isfile(content_path):
            os.remove(content_path)
        else:
            shutil.rmtree(content_path)

    if not _has_all_clips(movies_dir):
        print(f"ffmpeg produced incomplete clips for {movie_name}", file=sys.stderr)
        shutil.rmtree(movies_dir, ignore_errors=True)
        return False

    try:
        if on_ready:
            on_ready()
        os.makedirs(os.path.dirname(final_dir), exist_ok=True)
        shutil.rmtree(final_dir, ignore_errors=True)
        os.replace(movies_dir, final_dir)
    except Exception:
        shutil.rmtree(movies_dir, ignore_errors=True)
        raise

    return True


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _download_worker():
    while True:
        job = download_queue.get()
        if job is None:
            break

        print(f"Worker picked up job: {job['display_name']}")

        # Block until there is enough free space for a download.
        # If space is low, sleep and retry so the queue is preserved.
        while _get_free_space(PROJECT_DIR) < MIN_FREE_DOWNLOAD:
            print("Disk space low; waiting before next download...")
            time.sleep(30)

        sanitized = _sanitize_dirname(job["display_name"])
        with active_downloads_lock:
            active_downloads.add(sanitized)
        try:
            success = _download_and_process_movie(
                job["display_name"],
                on_ready=lambda: _add_movie_metadata(
                    sanitized,
                    job["title"],
                    job["year"],
                    job.get("poster_path", ""),
                    votes=job.get("votes"),
                    rating=job.get("rating"),
                    difficulty=job.get("difficulty"),
                ),
            )
            if success:
                print(f"Successfully downloaded and processed: {job['display_name']}")
            else:
                print(f"Download pipeline returned False for: {job['display_name']}")
        except Exception as exc:
            msg = f"Download failed for {job['display_name']}: {exc}"
            print(msg, file=sys.stderr)
            print(msg)
        finally:
            with active_downloads_lock:
                active_downloads.discard(sanitized)
            download_queue.task_done()


_cleanup_incomplete_movies()

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


@app.route('/download-movies', methods=["POST"])
def download_movies():
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    count = data.get("count", 1)

    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 1000:
        return jsonify({"error": "count must be an integer 1-1000"}), 400

    raw_difficulties = data.get("difficulties", data.get("difficulty"))
    if raw_difficulties is None:
        difficulties = list(DIFFICULTY_BANDS)
    elif isinstance(raw_difficulties, str):
        if raw_difficulties.strip().lower() == "all":
            difficulties = list(DIFFICULTY_BANDS)
        else:
            difficulties = [item.strip().lower() for item in raw_difficulties.split(",") if item.strip()]
    elif isinstance(raw_difficulties, list):
        difficulties = [item.strip().lower() for item in raw_difficulties if isinstance(item, str) and item.strip()]
    else:
        return jsonify({"error": "difficulties must be a list or comma-separated string"}), 400

    # Preserve the configured order while removing duplicates.
    difficulties = list(dict.fromkeys(difficulties))
    invalid = [difficulty for difficulty in difficulties if difficulty not in DIFFICULTY_BANDS]
    if invalid or not difficulties:
        return jsonify({
            "error": "difficulties must contain only easy, medium, or hard",
            "invalid": invalid,
        }), 400

    with selection_lock:
        try:
            movies = _pick_movies_not_downloaded(count, difficulties)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 500
        except sqlite3.Error as exc:
            return jsonify({"error": f"Unable to read local movies database: {exc}"}), 500

        queued_by_difficulty = {
            difficulty: sum(movie.get("difficulty") == difficulty for movie in movies)
            for difficulty in difficulties
        }
        insufficient = {
            difficulty: queued_by_difficulty[difficulty]
            for difficulty in difficulties
            if queued_by_difficulty[difficulty] < count
        }
        if insufficient:
            return jsonify({
                "error": "Not enough eligible movies for one or more difficulty levels",
                "requested_per_difficulty": count,
                "available_by_difficulty": queued_by_difficulty,
                "difficulties": difficulties,
            }), 409

        for movie in movies:
            download_queue.put(movie)

    print(f"Queued {len(movies)} movies for download (requested {count}).")
    for m in movies:
        print(f"  - {m['display_name']}")

    return jsonify({
        "queued": len(movies),
        "requested": count,
        "requested_per_difficulty": count,
        "requested_total": count * len(difficulties),
        "difficulties": difficulties,
        "queued_by_difficulty": queued_by_difficulty,
    })


@app.route('/movie-count')
def movie_count():
    downloaded = _get_movie_count()
    with active_downloads_lock:
        downloading = len(active_downloads)
    return jsonify({
        "downloaded": downloaded,
        "downloading": downloading,
        "queued": download_queue.qsize(),
        "workers": DOWNLOAD_WORKERS,
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
    movies_dir = os.path.join(PROJECT_DIR, "Movies")
    if not os.path.exists(movies_dir):
        return jsonify({"error": "No movies available"}), 404

    dirs = sorted(_get_downloaded_movies())
    if not dirs:
        return jsonify({"error": "No movies available"}), 404

    movie_dir = random.choice(dirs)
    clips = list(CLIP_NAMES)

    meta = _load_metadata()
    info = meta.get(movie_dir, {})
    return jsonify({
        "movie_dir": movie_dir,
        "clips": clips,
        "movie_name": info.get("display_name", movie_dir),
        "poster_path": info.get("poster_path", ""),
    })


@app.route('/game/guess', methods=["POST"])
def game_guess():
    data = request.get_json() or {}
    movie_dir = data.get("movie_dir", "")
    guess = data.get("guess", "")

    if not movie_dir or not guess:
        return jsonify({"error": "movie_dir and guess are required"}), 400

    meta = _load_metadata()
    info = meta.get(movie_dir)
    if not info:
        return jsonify({"error": "Movie not found"}), 404

    correct = guess.lower() == info["display_name"].lower()

    if correct:
        _delete_movie(movie_dir)
        return jsonify({"correct": True, "movie_name": info["display_name"], "poster_path": info.get("poster_path", "")})
    return jsonify({"correct": False})


@app.route('/game/reveal', methods=["POST"])
def game_reveal():
    data = request.get_json() or {}
    movie_dir = data.get("movie_dir", "")

    if not movie_dir:
        return jsonify({"error": "movie_dir is required"}), 400

    meta = _load_metadata()
    info = meta.get(movie_dir)
    if not info:
        return jsonify({"error": "Movie not found"}), 404

    movie_name = info["display_name"]
    poster_path = info.get("poster_path", "")
    _delete_movie(movie_dir)
    return jsonify({"movie_name": movie_name, "poster_path": poster_path})


@app.route('/search-movies')
def search_movies():
    if not TMDB_API_KEY:
        return jsonify({"error": "TMDB_API_KEY not configured."}), 500

    query = request.args.get('q', '')
    if not query:
        return jsonify([])

    url = f"{TMDB_BASE_URL}/search/movie"
    params = {
        "api_key": TMDB_API_KEY,
        "query": query,
        "include_adult": "false",
        "language": "en-US",
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])

        movies = []
        for movie in results[:10]:
            title = movie.get("title", "")
            release_date = movie.get("release_date", "")
            year = release_date[:4] if release_date and len(release_date) >= 4 else ""
            display_name = f"{title} ({year})" if year else title
            movies.append({
                "display_name": display_name,
                "poster_path": movie.get("poster_path"),
            })
        return jsonify(movies)
    except requests.RequestException:
        return jsonify([])


@app.route('/disk-status')
def disk_status():
    total, used, free = shutil.disk_usage(PROJECT_DIR)
    return jsonify({
        "total_gb": round(total / (1024 ** 3), 2),
        "used_gb": round(used / (1024 ** 3), 2),
        "free_gb": round(free / (1024 ** 3), 2),
        "low_space": free < MIN_FREE_DOWNLOAD,
    })


@app.route('/movies/<path:subpath>')
def serve_movie(subpath):
    movies_dir = os.path.join(PROJECT_DIR, "Movies")
    return send_from_directory(movies_dir, subpath)


if __name__ == '__main__':
    # The Werkzeug debugger allows arbitrary code execution, so it is opt-in
    # for local development only. Never enable it on a reachable server.
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=5000, debug=debug)
