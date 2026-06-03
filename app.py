from flask import Flask, render_template, jsonify, send_from_directory, request
import requests
import os
import random
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
from queue import Queue
import shutil

load_dotenv()

app = Flask(__name__)

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
TMDB_BASE_URL = "https://api.themoviedb.org/3"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
METADATA_FILE = os.path.join(PROJECT_DIR, "movies_db.json")

# ---------------------------------------------------------------------------
# Thread-safe download queue / state
# ---------------------------------------------------------------------------

download_queue = Queue()
active_downloads = set()
active_downloads_lock = threading.Lock()

# ---------------------------------------------------------------------------
# qBittorrent helpers
# ---------------------------------------------------------------------------

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
        all_results = results
    stop_and_delete_search(opener, search_id)

    if not all_results:
        return None

    best = max(all_results, key=lambda r: r.get("nbSeeders", 0))
    return best.get("fileUrl", None)


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
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _add_movie_metadata(sanitized_name, title, year, poster_path):
    meta = _load_metadata()
    meta[sanitized_name] = {
        "title": title,
        "year": year,
        "poster_path": poster_path,
        "display_name": f"{title} ({year})" if year else title,
    }
    _save_metadata(meta)


# ---------------------------------------------------------------------------
# Downloaded-movie helpers
# ---------------------------------------------------------------------------

def _get_downloaded_movies():
    movies_dir = os.path.join(PROJECT_DIR, "Movies")
    if not os.path.exists(movies_dir):
        return set()
    return {d for d in os.listdir(movies_dir) if os.path.isdir(os.path.join(movies_dir, d))}


def _get_movie_count():
    return len(_get_downloaded_movies())


def _delete_movie(sanitized_name):
    movie_path = os.path.join(PROJECT_DIR, "Movies", sanitized_name)
    if os.path.exists(movie_path):
        shutil.rmtree(movie_path)
    meta = _load_metadata()
    if sanitized_name in meta:
        del meta[sanitized_name]
        _save_metadata(meta)


# ---------------------------------------------------------------------------
# TMDB helpers
# ---------------------------------------------------------------------------

def _build_tmdb_params(page):
    return {
        "api_key": TMDB_API_KEY,
        "sort_by": "popularity.desc",
        "page": page,
        "include_adult": "false",
        "with_original_language": "en",
        "vote_average.gte": "5",
        "vote_average.lte": "10",
        "vote_count.gte": "500",
        "with_runtime.gte": "0",
        "with_runtime.lte": "360",
    }


def _pick_movies_not_downloaded(count):
    downloaded = _get_downloaded_movies()
    with active_downloads_lock:
        in_progress = set(active_downloads)
    excluded = downloaded | in_progress

    picked = []
    max_pages = 20
    for _ in range(max_pages):
        if len(picked) >= count:
            break
        url = f"{TMDB_BASE_URL}/discover/movie"
        params = _build_tmdb_params(random.randint(1, 50))
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            for movie in results:
                title = movie.get("title", "")
                release_date = movie.get("release_date", "")
                year = release_date[:4] if release_date and len(release_date) >= 4 else ""
                display_name = f"{title} ({year})" if year else title
                sanitized = _sanitize_dirname(display_name)
                if sanitized not in excluded:
                    picked.append({
                        "display_name": display_name,
                        "title": title,
                        "year": year,
                        "poster_path": movie.get("poster_path"),
                    })
                    excluded.add(sanitized)
                    if len(picked) >= count:
                        break
        except Exception:
            break
    return picked


# ---------------------------------------------------------------------------
# Single-movie download + process
# ---------------------------------------------------------------------------

def _download_and_process_movie(movie_name):
    """Full pipeline for one movie. Returns True on success."""
    if not movie_name:
        return False

    link = run_search(movie_name)
    if not link:
        return False

    opener = _build_opener()
    tag = f"moviedle_{int(time.time() * 1000)}"

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

    while elapsed < max_wait:
        try:
            body = _api_request(
                opener,
                "/api/v2/torrents/info",
                data={"tag": tag},
                method="GET",
            )
        except RuntimeError:
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

        if progress == 1.0:
            finished = True
            break
        if torrent_state in ("error", "missingFiles"):
            return False

        time.sleep(poll_interval)
        elapsed += poll_interval

    if not finished:
        return False

    if not content_path and save_path and torrent_name:
        content_path = os.path.join(save_path, torrent_name)

    # Delete torrent without removing files
    if torrent_hash:
        try:
            _api_request(
                opener,
                "/api/v2/torrents/delete",
                data={"hashes": torrent_hash, "deleteFiles": "false"},
            )
        except RuntimeError:
            pass

    # Post-processing
    if not content_path or not os.path.exists(content_path):
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
        return False

    largest = max(video_files, key=os.path.getsize)
    movies_dir = os.path.join(PROJECT_DIR, "Movies", _sanitize_dirname(movie_name))
    os.makedirs(movies_dir, exist_ok=True)

    try:
        duration = _get_video_duration(largest)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
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

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

    # Delete original downloaded files
    if content_path and os.path.exists(content_path):
        if os.path.isfile(content_path):
            os.remove(content_path)
        else:
            shutil.rmtree(content_path)

    return True


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _download_worker():
    while True:
        job = download_queue.get()
        if job is None:
            break
        sanitized = _sanitize_dirname(job["display_name"])
        with active_downloads_lock:
            active_downloads.add(sanitized)
        try:
            success = _download_and_process_movie(job["display_name"])
            if success:
                _add_movie_metadata(
                    sanitized,
                    job["title"],
                    job["year"],
                    job.get("poster_path", ""),
                )
        except Exception as exc:
            print(f"Download failed for {job['display_name']}: {exc}", file=sys.stderr)
        finally:
            with active_downloads_lock:
                active_downloads.discard(sanitized)
            download_queue.task_done()


threading.Thread(target=_download_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/download-movies', methods=["POST"])
def download_movies():
    if not TMDB_API_KEY:
        return jsonify({"error": "TMDB_API_KEY not configured."}), 500

    data = request.get_json() or {}
    count = data.get("count", 1)

    if not isinstance(count, int) or count < 1 or count > 100:
        return jsonify({"error": "count must be an integer 1-100"}), 400

    movies = _pick_movies_not_downloaded(count)
    for movie in movies:
        download_queue.put(movie)

    return jsonify({"queued": len(movies), "requested": count})


@app.route('/movie-count')
def movie_count():
    downloaded = _get_movie_count()
    with active_downloads_lock:
        downloading = len(active_downloads)
    return jsonify({"downloaded": downloaded, "downloading": downloading})


@app.route('/movies/list')
def movies_list():
    meta = _load_metadata()
    movies = []
    for key, info in meta.items():
        movies.append({
            "movie_dir": key,
            "display_name": info.get("display_name", key),
            "poster_path": info.get("poster_path"),
        })
    movies.sort(key=lambda m: m["display_name"])
    return jsonify(movies)


@app.route('/game/start')
def game_start():
    movies_dir = os.path.join(PROJECT_DIR, "Movies")
    if not os.path.exists(movies_dir):
        return jsonify({"error": "No movies available"}), 404

    dirs = [d for d in os.listdir(movies_dir) if os.path.isdir(os.path.join(movies_dir, d))]
    if not dirs:
        return jsonify({"error": "No movies available"}), 404

    movie_dir = random.choice(dirs)
    clips = [f"{s}s.mp4" for s in range(1, 7)]

    return jsonify({
        "movie_dir": movie_dir,
        "clips": clips,
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
        return jsonify({"correct": True, "movie_name": info["display_name"]})
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
    _delete_movie(movie_dir)
    return jsonify({"movie_name": movie_name})


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


@app.route('/movies/<path:subpath>')
def serve_movie(subpath):
    movies_dir = os.path.join(PROJECT_DIR, "Movies")
    return send_from_directory(movies_dir, subpath)


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000,debug=True)
