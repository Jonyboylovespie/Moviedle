import json
import os
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


# ---------------------------------------------------------------------------
# Download workflow
# ---------------------------------------------------------------------------

def download(link):
    """Start a qBittorrent download for *link* in the current project directory.

    Waits until the download finishes and returns True on success, False otherwise.
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
    max_wait = 7200  # 1 hour
    elapsed = 0

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

        if progress == 1.0:
            return True
        if state in ("error", "missingFiles"):
            return False

        time.sleep(poll_interval)
        elapsed += poll_interval

    return False


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
        print(f"Best result: {link}")
        success = download(link)
        if success:
            print("Download completed successfully.")
        else:
            print("Download failed.")
