from flask import Flask, render_template, jsonify, send_from_directory, request
import requests
import os
import random
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from dotenv import load_dotenv
import shutil
import threading

load_dotenv()

app = Flask(__name__)

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
TMDB_BASE_URL = "https://api.themoviedb.org/3"

# ---------------------------------------------------------------------------
# Shared download state
# ---------------------------------------------------------------------------

download_state = {
    "status": "idle",
    "message": "",
    "progress": 0,
    "movie_name": "",
    "clips": []
}

# ---------------------------------------------------------------------------
# qBittorrent helpers (from test.py)
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
# Download + process with state updates
# ---------------------------------------------------------------------------

def _download_and_process(link, query, state):
    if not link:
        state["status"] = "error"
        state["message"] = "No torrent link found."
        return

    state["status"] = "downloading"
    state["message"] = "Starting download..."
    state["progress"] = 0

    opener = _build_opener()
    project_dir = os.path.dirname(os.path.abspath(__file__))
    tag = f"moviedle_{int(time.time() * 1000)}"

    try:
        _api_request(
            opener,
            "/api/v2/torrents/add",
            data={
                "urls": link,
                "savepath": project_dir,
                "tags": tag,
            },
        )
    except RuntimeError as exc:
        state["status"] = "error"
        state["message"] = f"Failed to add torrent: {exc}"
        return

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
        except RuntimeError as exc:
            state["status"] = "error"
            state["message"] = f"Error polling torrent: {exc}"
            return

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

        state["progress"] = int(progress * 100)
        state["message"] = f"Downloading... {state['progress']}%"

        if progress == 1.0:
            finished = True
            break
        if torrent_state in ("error", "missingFiles"):
            state["status"] = "error"
            state["message"] = "Torrent failed."
            return

        time.sleep(poll_interval)
        elapsed += poll_interval

    if not finished:
        state["status"] = "error"
        state["message"] = "Download timed out."
        return

    if not content_path and save_path and torrent_name:
        content_path = os.path.join(save_path, torrent_name)

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
    if query and content_path and os.path.exists(content_path):
        state["status"] = "processing"
        state["message"] = "Generating clips..."
        state["progress"] = 100

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
            state["status"] = "error"
            state["message"] = "No video files found in download."
            return

        largest = max(video_files, key=os.path.getsize)
        movies_dir = os.path.join(project_dir, "Movies", _sanitize_dirname(query))
        os.makedirs(movies_dir, exist_ok=True)

        try:
            duration = _get_video_duration(largest)
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as exc:
            state["status"] = "error"
            state["message"] = f"ffprobe failed: {exc}"
            return

        # Build single-pass multi-output ffmpeg command
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
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            state["status"] = "error"
            state["message"] = f"ffmpeg failed: {exc}"
            return

        # Delete the original downloaded files
        if content_path and os.path.exists(content_path):
            if os.path.isfile(content_path):
                os.remove(content_path)
            else:
                shutil.rmtree(content_path)

        state["clips"] = [f"{s}s.mp4" for s in range(1, 7)]
        state["status"] = "done"
        state["message"] = "Complete!"
    else:
        state["status"] = "error"
        state["message"] = "Download path not found."


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


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


@app.route('/random-movie')
def random_movie():
    if not TMDB_API_KEY:
        return jsonify({"error": "TMDB_API_KEY not configured. Set it as an environment variable."}), 500

    random_page = random.randint(1, 100)
    url = f"{TMDB_BASE_URL}/discover/movie"
    params = _build_tmdb_params(random_page)

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        results = data.get("results", [])
        if not results:
            return jsonify({"error": "No movies found"}), 404

        movie = random.choice(results)
        return jsonify({
            "title": movie.get("title"),
            "overview": movie.get("overview"),
            "release_date": movie.get("release_date"),
            "poster_path": movie.get("poster_path")
        })
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500


@app.route('/generate-movie')
def generate_movie():
    if not TMDB_API_KEY:
        return jsonify({"error": "TMDB_API_KEY not configured."}), 500

    random_page = random.randint(1, 10)
    url = f"{TMDB_BASE_URL}/discover/movie"
    params = _build_tmdb_params(random_page)

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        results = data.get("results", [])
        if not results:
            return jsonify({"error": "No movies found"}), 404

        movie = random.choice(results)
        title = movie.get("title", "")
        release_date = movie.get("release_date", "")
        year = release_date[:4] if release_date and len(release_date) >= 4 else ""
        display_name = f"{title} ({year})" if year else title

        return jsonify({
            "display_name": display_name,
            "title": title,
            "year": year,
            "overview": movie.get("overview"),
            "poster_path": movie.get("poster_path")
        })
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500


@app.route('/start-download', methods=["POST"])
def start_download():
    global download_state
    if download_state["status"] not in ("idle", "done", "error"):
        return jsonify({"error": "A download is already in progress."}), 409

    data = request.get_json() or {}
    movie_name = data.get("movie_name", "")

    if not movie_name:
        return jsonify({"error": "movie_name is required"}), 400

    download_state = {
        "status": "searching",
        "message": "Searching for torrents...",
        "progress": 0,
        "movie_name": movie_name,
        "movie_dir": _sanitize_dirname(movie_name),
        "clips": []
    }

    def worker():
        try:
            link = run_search(movie_name)
            if link:
                _download_and_process(link, movie_name, download_state)
            else:
                download_state["status"] = "error"
                download_state["message"] = "No torrents found."
        except Exception as exc:
            download_state["status"] = "error"
            download_state["message"] = str(exc)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"status": "started"})


@app.route('/download-status')
def download_status():
    return jsonify(download_state)


@app.route('/movies/<path:subpath>')
def serve_movie(subpath):
    movies_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Movies")
    return send_from_directory(movies_dir, subpath)


if __name__ == '__main__':
    app.run(debug=True)
