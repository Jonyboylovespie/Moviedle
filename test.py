import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

def _build_opener():
    """Return an urllib opener that stores cookies (for the SID auth cookie)."""
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _api_request(opener, path, data=None, method=None):
    """Send a request to the qBittorrent Web API and return the response body."""
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


# ---------------------------------------------------------------------------
# Search workflow
# ---------------------------------------------------------------------------

def start_search(opener, pattern):
    """Start a search job and return the searchId."""
    body = _api_request(
        opener,
        "/api/v2/search/start",
        data={"pattern": pattern, "plugins": "enabled", "category": "all"},
    )
    # Response: {"id": <int>}
    result = json.loads(body)
    search_id = result.get("id")
    if search_id is None:
        raise RuntimeError(f"Unexpected start_search response: {body}")
    return search_id


def get_results(opener, search_id, limit=0, offset=0):
    """Fetch search results. Returns (results_list, status_string)."""
    body = _api_request(
        opener,
        "/api/v2/search/results",
        data={"id": search_id, "limit": str(limit), "offset": str(offset)},
        method="GET",
    )
    data = json.loads(body)
    return data.get("results", []), data.get("status", "")


def stop_and_delete_search(opener, search_id):
    """Clean up the search job on the server."""
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
    """Run the full search workflow and print the best result."""
    opener = _build_opener()

    search_id = start_search(opener, pattern)
    print(f"Started search (id={search_id}) for: {pattern}", file=sys.stderr)

    all_results = []

    status = "Running"
    while status == "Running":
        results, status = get_results(opener, search_id)
        all_results = results
    stop_and_delete_search(opener, search_id)

    if not all_results:
        print("No results found.")
        return

    best = max(all_results, key=lambda r: r.get("nbSeeders", 0))
    return best.get("fileUrl", None)


def _sanitize_dirname(name):
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, '_')
    return name.strip('. ')


def _get_video_duration(path):
    """Return the duration of *path* in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv(p=0)",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# Download workflow
# ---------------------------------------------------------------------------

def download(link, query=None):
    """Start a qBittorrent download for *link* in the current project directory.

    Waits until the download finishes, deletes the torrent without removing files,
    and optionally post-processes the largest video found.

    Returns True on full success, False otherwise.
    """
    if not link:
        return False

    opener = _build_opener()
    project_dir = os.path.dirname(os.path.abspath(__file__))
    tag = f"moviedle_{int(time.time() * 1000)}"

    # Add the torrent
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
    except RuntimeError:
        return False

    # Poll until completion, error, or timeout
    poll_interval = 2
    max_wait = 3600  # 1 hour
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
        state = torrent.get("state", "")
        torrent_hash = torrent.get("hash")
        content_path = torrent.get("content_path")
        save_path = torrent.get("save_path")
        torrent_name = torrent.get("name")

        if progress == 1.0:
            finished = True
            break
        if state in ("error", "missingFiles"):
            return False

        time.sleep(poll_interval)
        elapsed += poll_interval

    if not finished:
        return False

    # Resolve content path if the API didn't provide it
    if not content_path and save_path and torrent_name:
        content_path = os.path.join(save_path, torrent_name)

    # Delete the torrent without deleting files
    if torrent_hash:
        try:
            _api_request(
                opener,
                "/api/v2/torrents/delete",
                data={"hashes": torrent_hash, "deleteFiles": "false"},
            )
        except RuntimeError:
            pass  # continue even if removal fails

    # Post-processing
    if query and content_path and os.path.exists(content_path):
        content_path = os.path.normpath(content_path)

        # Gather all .mp4 and .mkv files
        video_files = []
        if os.path.isfile(content_path):
            if content_path.lower().endswith((".mp4", ".mkv")):
                video_files.append(content_path)
        else:
            for root, dirs, files in os.walk(content_path):
                for file in files:
                    if file.lower().endswith((".mp4", ".mkv")):
                        video_files.append(os.path.join(root, file))

        if video_files:
            largest = max(video_files, key=os.path.getsize)
            movies_dir = os.path.join(
                project_dir, "Movies", _sanitize_dirname(query)
            )
            os.makedirs(movies_dir, exist_ok=True)

            try:
                duration = _get_video_duration(largest)
            except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
                return False

            for seconds in range(1, 7):
                output_file = os.path.join(movies_dir, f"{seconds}s.mp4")
                speed = duration / seconds
                vf = f"setpts=(PTS-STARTPTS)/{speed}"

                try:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i", largest,
                            "-vf", vf,
                            "-an",
                            "-r", "24",
                            "-c:v", "libx264",
                            "-preset", "fast",
                            output_file,
                        ],
                        check=True,
                        capture_output=True,
                    )
                except (subprocess.CalledProcessError, FileNotFoundError):
                    return False

    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} SEARCHTERM", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]
    try:
        link = run_search(query)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if link:
        print("Downloading movie...")
        success = download(link, query)
        print(f"Download success: {success}")
        sys.exit(0 if success else 1)
