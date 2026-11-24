"""Playback checks that need no downloaded movie.

Clips come from ffmpeg's synthetic test pattern, so this runs in a few
seconds. Run from the repo root:

    python3 -m unittest discover -s tests -v

The browser test needs Node 22+ and Chrome/Chromium; it is skipped without them.
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
# Never start real downloads from tests.
os.environ["AUTO_DOWNLOAD"] = "0"

import app  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
CHROME = next((shutil.which(name) for name in ("google-chrome", "chromium-browser", "chromium") if shutil.which(name)), None)
NODE = shutil.which("node")


def make_source(path, *, ten_bit=False, hdr=False, size="1920x800", seconds=12):
    """Write a synthetic test-pattern movie, optionally 10-bit and/or HDR10-tagged."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
           "-i", f"testsrc2=size={size}:rate=24:duration={seconds}"]
    if ten_bit:
        tags = "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc," if hdr else ""
        cmd += ["-vf", f"{tags}format=yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast",
                "-x265-params", "log-level=error"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    subprocess.run(cmd + [path], check=True)
    return path


def make_old_style_clip(source, path):
    """Encode a clip the way the app did before the fix (no pix_fmt, no faststart)."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", source, "-t", "1", "-an", "-r", "24",
                    "-c:v", "libx264", "-preset", "ultrafast", path], check=True)
    return path


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,profile,pix_fmt,color_transfer,nb_frames:format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return {**data["streams"][0], "duration": float(data["format"]["duration"])}


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class EncodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="moviedle-encode-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def encode(self, source):
        out = os.path.join(self.tmp, "clips")
        os.makedirs(out)
        app._encode_clips(source, out, app._get_video_duration(source))
        return out

    def assert_browser_safe(self, out):
        for second, clip in enumerate(app.CLIP_NAMES, start=1):
            path = os.path.join(out, clip)
            info = probe(path)
            with self.subTest(clip=clip):
                self.assertEqual(app._clip_problems(path), [])
                self.assertEqual(info["pix_fmt"], "yuv420p")
                self.assertNotIn("10", info["profile"])
                self.assertTrue(app._mp4_has_faststart(path))
                self.assertAlmostEqual(info["duration"], second, delta=0.1)
                self.assertEqual(int(info["nb_frames"]), 24 * second)

    def test_10bit_source_gives_8bit_clips(self):
        source = make_source(os.path.join(self.tmp, "src.mkv"), ten_bit=True, size="640x266")
        self.assertEqual(probe(source)["pix_fmt"], "yuv420p10le")
        self.assert_browser_safe(self.encode(source))

    def test_hdr_source_is_tone_mapped(self):
        if not app._has_zscale():
            self.skipTest("ffmpeg built without zscale")
        source = make_source(os.path.join(self.tmp, "src.mkv"), ten_bit=True, hdr=True, size="640x266")
        self.assertEqual(probe(source)["color_transfer"], "smpte2084")
        out = self.encode(source)
        self.assert_browser_safe(out)
        self.assertEqual(probe(os.path.join(out, "1s.mp4"))["color_transfer"], "bt709")

    def test_old_10bit_clip_is_repaired(self):
        source = make_source(os.path.join(self.tmp, "src.mkv"), ten_bit=True, size="640x266")
        clip = make_old_style_clip(source, os.path.join(self.tmp, "1s.mp4"))
        self.assertEqual(probe(clip)["profile"], "High 10")  # the original bug
        self.assertEqual(app._clip_problems(clip), ["pix_fmt yuv420p10le", "no faststart"])
        app._repair_clip(clip)
        self.assertEqual(app._clip_problems(clip), [])
        self.assertNotIn("10", probe(clip)["profile"])

    def test_old_8bit_clip_only_gets_faststart(self):
        source = make_source(os.path.join(self.tmp, "src.mp4"), size="640x266")
        clip = make_old_style_clip(source, os.path.join(self.tmp, "1s.mp4"))
        before = probe(clip)
        self.assertEqual(app._repair_clip(clip), ["no faststart"])
        self.assertEqual(app._clip_problems(clip), [])
        self.assertEqual(probe(clip)["nb_frames"], before["nb_frames"])

    def test_repair_scans_movies_folder(self):
        source = make_source(os.path.join(self.tmp, "src.mkv"), ten_bit=True, size="640x266")
        movie = os.path.join(self.tmp, "Movies", "Old Movie (1999)")
        os.makedirs(movie)
        for clip in app.CLIP_NAMES:
            make_old_style_clip(source, os.path.join(movie, clip))
        with patched_project(self.tmp), contextlib.redirect_stdout(io.StringIO()):
            app._repair_existing_clips()
        for clip in app.CLIP_NAMES:
            self.assertEqual(app._clip_problems(os.path.join(movie, clip)), [])


@contextlib.contextmanager
def patched_project(directory):
    saved = app.PROJECT_DIR, app.METADATA_FILE
    app.PROJECT_DIR = directory
    app.METADATA_FILE = os.path.join(directory, "movies_db.json")
    try:
        yield
    finally:
        app.PROJECT_DIR, app.METADATA_FILE = saved


def add_movie(project, name, difficulty, clip_bytes=b"not really a video"):
    folder = os.path.join(project, "Movies", name)
    os.makedirs(folder, exist_ok=True)
    for clip in app.CLIP_NAMES:
        with open(os.path.join(folder, clip), "wb") as f:
            f.write(clip_bytes)
    meta = app._load_metadata()
    meta[name] = {"title": name, "year": "", "poster_path": "", "display_name": name, "difficulty": difficulty}
    app._save_metadata(meta)
    return folder


class ReleasePickTests(unittest.TestCase):
    @staticmethod
    def _results(*rows):
        return [{"fileName": n, "nbSeeders": s, "fileSize": 2 * 1024 ** 3} for n, s in rows]

    def test_search_names_title_and_year_without_parentheses(self):
        self.assertEqual(app._search_queries("Sonic the Hedgehog", "2020"), ["Sonic the Hedgehog 2020"])
        self.assertEqual(app._search_queries("Amélie", "2001"), ["Amelie 2001", "Amélie 2001"])
        self.assertEqual(app._search_queries("Sonic the Hedgehog"), ["Sonic the Hedgehog"])

    def test_picks_best_seeded_english_release(self):
        results = self._results(
            ("Sonic The Hedgehog 2020 1080p BluRay (Multi Audio) HEVC H265 HDR", 300),
            ("Sonic.the.Hedgehog.2020.HC.HDRip.XviD.AC3-EVO[TGx]", 9),
            ("Sonic.The.Hedgehog.2020.1080p.WEBRip.x264.AAC2.0", 191),
        )
        with contextlib.redirect_stderr(io.StringIO()):
            best = app._pick_best_result(results, "Sonic the Hedgehog", "2020")
        self.assertEqual(best["fileName"], "Sonic.The.Hedgehog.2020.1080p.WEBRip.x264.AAC2.0")

    def test_smaller_well_seeded_release_beats_most_seeded(self):
        gib, mib = 1024 ** 3, 1024 ** 2
        results = [
            {"fileName": n, "nbSeeders": s, "fileSize": int(z)} for n, s, z in (
                ("Sonic.The.Hedgehog.2020.1080p.WEBRip.x264.AAC2.0", 191, 1.65 * gib),
                ("Sonic.the.Hedgehog.2020.1080p.BluRay.x264-GECKOS[TGx]", 64, 9.74 * gib),
                ("Sonic.The.Hedgehog.2020.720p.BluRay.x264.AAC", 40, 908 * mib),
                ("Sonic.the.Hedgehog.2020.720p.WEBRip.800MB.x264-GalaxyRG", 2, 797 * mib),
                ("Sonic.the.Hedgehog.2020.HC.720p.HDRip.800MB.x264-GalaxyRG", 60, 797 * mib),
            )]
        with contextlib.redirect_stderr(io.StringIO()):
            best = app._pick_best_result(results, "Sonic the Hedgehog", "2020")
        self.assertEqual(best["fileName"], "Sonic.The.Hedgehog.2020.720p.BluRay.x264.AAC")


class GameTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="moviedle-game-")
        self.addCleanup(shutil.rmtree, self.tmp)
        project = patched_project(self.tmp)
        project.__enter__()
        self.addCleanup(project.__exit__, None, None, None)
        app.active_games.clear()
        app.retired_movies.clear()
        self.client = app.app.test_client()


class GameRoundTests(GameTestCase):
    def setUp(self):
        super().setUp()
        self.folder = add_movie(self.tmp, "Secret Title (2001)", "easy")

    def start(self):
        response = self.client.get("/game/start?difficulty=easy")
        self.assertEqual(response.status_code, 200, response.get_json())
        return response

    def clip(self, game_id, clip="1s.mp4", **kwargs):
        response = self.client.get(f"/game/{game_id}/clips/{clip}", **kwargs)
        response.close()
        return response

    def test_start_does_not_reveal_the_answer(self):
        response = self.start()
        self.assertEqual(set(response.get_json()), {"game_id", "clips", "difficulty"})
        self.assertNotIn("Secret", response.get_data(as_text=True))

    def test_clip_served_by_game_id_with_range_support(self):
        game_id = self.start().get_json()["game_id"]
        self.assertEqual(self.clip(game_id).status_code, 200)
        self.assertEqual(self.clip(game_id, headers={"Range": "bytes=0-3"}).status_code, 206)
        self.assertEqual(self.clip(game_id, "../movies_db.json").status_code, 404)
        self.assertEqual(self.clip("not-a-game").status_code, 404)
        # The old route exposed clips by title, letting anyone probe guesses.
        self.assertEqual(self.client.get("/movies/Secret%20Title%20(2001)/1s.mp4").status_code, 404)

    def test_finishing_a_round_keeps_the_movie_for_other_players(self):
        first = self.start().get_json()["game_id"]
        second = self.start().get_json()["game_id"]  # only one movie, so the same one

        reveal = self.client.post("/game/reveal", json={"game_id": first})
        self.assertEqual(reveal.get_json()["movie_name"], "Secret Title (2001)")

        # The other round can still play every clip and still guess.
        self.assertEqual(self.clip(second, "6s.mp4").status_code, 200)
        self.assertTrue(os.path.isdir(self.folder))
        # The finished movie is out of the pool for new rounds.
        self.assertEqual(self.client.get("/movie-count").get_json()["downloaded"], 0)
        self.assertEqual(self.client.get("/game/start?difficulty=easy").status_code, 404)
        self.assertEqual(self.clip(first).status_code, 404)

        guess = self.client.post("/game/guess", json={"game_id": second, "guess": "secret title (2001)"})
        self.assertTrue(guess.get_json()["correct"])
        self.assertFalse(os.path.exists(self.folder))  # deleted once nobody is playing it
        self.assertNotIn("Secret Title (2001)", app._load_metadata())

    def test_round_cannot_be_finished_twice(self):
        game_id = self.start().get_json()["game_id"]
        self.assertEqual(self.client.post("/game/reveal", json={"game_id": game_id}).status_code, 200)
        self.assertEqual(self.client.post("/game/reveal", json={"game_id": game_id}).status_code, 404)

    def test_abandoned_round_stops_pinning_a_finished_movie(self):
        abandoned = self.start().get_json()["game_id"]
        finisher = self.start().get_json()["game_id"]
        app.active_games[abandoned]["started"] -= app.GAME_TTL + 1
        self.client.post("/game/reveal", json={"game_id": finisher})
        self.assertFalse(os.path.exists(self.folder))

    def test_clip_error_is_logged(self):
        game_id = self.start().get_json()["game_id"]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            response = self.client.post("/game/clip-error", json={"game_id": game_id, "clip": "3s.mp4", "detail": "decode error"})
        self.assertEqual(response.status_code, 204)
        self.assertIn("Secret Title (2001)/3s.mp4: decode error", stderr.getvalue())


@unittest.skipUnless(HAVE_FFMPEG and CHROME and NODE, "needs ffmpeg, node and Chrome")
class AutoDownloadTests(GameTestCase):
    """The workers' choice of what to download next and whether it fits."""

    GB = 1024 ** 3

    def setUp(self):
        super().setUp()
        app.active_downloads.clear()
        app.skipped_movies.clear()
        self.addCleanup(app.active_downloads.clear)
        self.addCleanup(app.skipped_movies.clear)
        db = os.path.join(self.tmp, "movies.db")
        with contextlib.closing(sqlite3.connect(db)) as connection, connection:
            connection.executescript(
                "CREATE TABLE movies (id INTEGER PRIMARY KEY, title TEXT, year NUMERIC);"
                "CREATE TABLE ratings (movie_id INTEGER, rating REAL, votes INTEGER);")
            for movie_id, (title, votes) in enumerate([
                ("Easy One", 900_000), ("Easy Two", 800_000),
                ("Medium One", 300_000), ("Medium Two", 300_000),
                ("Hard One", 150_000), ("Hard Two", 150_000),
            ], start=1):
                connection.execute("INSERT INTO movies VALUES (?, ?, 2000)", (movie_id, title))
                connection.execute("INSERT INTO ratings VALUES (?, 7.0, ?)", (movie_id, votes))
        saved_db = app.MOVIES_DB_FILE
        app.MOVIES_DB_FILE = db
        self.addCleanup(setattr, app, "MOVIES_DB_FILE", saved_db)

    def test_vote_counts_map_to_difficulty_bands(self):
        cases = {24_999: None, 25_000: "impossible", 99_999: "impossible", 100_000: "hard",
                 249_999: "hard", 250_000: "medium", 500_000: "easy"}
        for votes, expected in cases.items():
            self.assertEqual(app._difficulty_for_votes(votes), expected, votes)

    def test_picks_the_least_stocked_difficulty(self):
        add_movie(self.tmp, "Easy One (2000)", "easy")
        add_movie(self.tmp, "Hard One (2000)", "hard")
        app.active_downloads["Medium One (2000)"] = {"difficulty": "medium", "outstanding": 0}
        # Easy, medium (in progress) and hard each have one, so any may come
        # next; after that the remaining two bands must be filled first.
        first = app._claim_next_movie()
        second = app._claim_next_movie()
        third = app._claim_next_movie()
        self.assertEqual(len({first["difficulty"], second["difficulty"], third["difficulty"]}), 3)
        self.assertEqual(set(app.active_downloads), {
            "Medium One (2000)", *(m["display_name"] for m in (first, second, third))})

    def test_skips_saved_active_and_failed_movies_and_exhausted_bands(self):
        add_movie(self.tmp, "Easy One (2000)", "easy")
        app.active_downloads["Easy Two (2000)"] = {"difficulty": "easy", "outstanding": 0}
        app.skipped_movies["Hard One (2000)"] = time.time()
        picked = [app._claim_next_movie() for _ in range(3)]
        self.assertEqual(sorted(m["display_name"] for m in picked),
                         ["Hard Two (2000)", "Medium One (2000)", "Medium Two (2000)"])
        self.assertIsNone(app._claim_next_movie())

    def test_failed_movie_is_retried_after_a_day(self):
        for name in ("Easy One", "Easy Two", "Medium One", "Medium Two", "Hard One"):
            app.skipped_movies[f"{name} (2000)"] = time.time()
        app.skipped_movies["Hard Two (2000)"] = time.time() - app.SKIP_TTL - 1
        self.assertEqual(app._claim_next_movie()["display_name"], "Hard Two (2000)")

    def test_downloads_only_fit_above_the_disk_reserve(self):
        app.active_downloads["Other (2000)"] = {"difficulty": "easy", "outstanding": 3 * self.GB}
        app.active_downloads["New (2000)"] = {"difficulty": "hard", "outstanding": 0}
        free = 10 * self.GB
        with unittest.mock.patch.object(app, "_get_free_space", lambda path: free), \
                unittest.mock.patch.object(app, "DISK_RESERVE", 5 * self.GB):
            self.assertFalse(app._claim_space("New (2000)", 2.5 * self.GB))
            self.assertTrue(app._claim_space("New (2000)", 2 * self.GB))
            self.assertEqual(app.active_downloads["New (2000)"]["outstanding"], 2 * self.GB)
            # Everything left (10 - 3 - 2 = 5 GB) is reserve, so no new download starts.
            self.assertFalse(app._has_room_for_download())
            app.active_downloads["Other (2000)"]["outstanding"] = 0
            self.assertTrue(app._has_room_for_download())

    def test_movie_count_reports_downloads_by_difficulty(self):
        app.active_downloads["Hard One (2000)"] = {"difficulty": "hard", "outstanding": 0}
        data = self.client.get("/movie-count").get_json()
        self.assertEqual(data["downloading"], 1)
        self.assertEqual(data["downloading_by_difficulty"], {"impossible": 0, "hard": 1, "medium": 0, "easy": 0})
        self.assertNotIn("queued", data)
        self.assertEqual(self.client.post("/download-movies", json={"count": 1}).status_code, 404)


class BrowserTests(GameTestCase):
    """Load the real page in headless Chrome and measure the player."""

    SCREENSHOT = os.path.join(tempfile.gettempdir(), "moviedle-player-check.png")

    def test_player_shows_whole_clip_and_reports_broken_ones(self):
        from werkzeug.serving import make_server

        # A wide 1920x800 clip made by the real encoder from a 10-bit source.
        folder = add_movie(self.tmp, "Wide Movie (2001)", "easy")
        shutil.rmtree(folder)
        os.makedirs(folder)
        source = make_source(os.path.join(self.tmp, "src.mkv"), ten_bit=True)
        app._encode_clips(source, folder, app._get_video_duration(source))
        add_movie(self.tmp, "Broken Movie (2002)", "medium", clip_bytes=b"garbage" * 100)

        server = make_server("127.0.0.1", 0, app.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = subprocess.run(
                [NODE, os.path.join(REPO, "tests", "browser_check.mjs"),
                 f"http://127.0.0.1:{server.server_port}/", CHROME, self.SCREENSHOT],
                capture_output=True, text=True, timeout=90)
            time.sleep(0.3)  # let the clip-error request land
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        good, broken = report["good"], report["broken"]
        print(f"\n  screen {good['screen']}\n  video  {good['element']}\n"
              f"  visible {good['visibleFraction']:.0%} of a {good['videoWidth']}x{good['videoHeight']} clip"
              f"\n  screenshot: {self.SCREENSHOT}\n  broken clip message: {broken['text']!r}")

        self.assertTrue(good["played"], good)
        self.assertIsNone(good["error"])
        self.assertEqual((good["videoWidth"], good["videoHeight"]), (1920, 800))
        self.assertIn("/game/", good["src"])
        for key in ("x", "y", "w", "h"):  # element exactly covers the screen
            self.assertAlmostEqual(good["element"][key], good["screen"][key], delta=1)
        self.assertAlmostEqual(good["visibleFraction"], 1.0, places=3)
        self.assertEqual(good["objectFit"], "contain")
        self.assertEqual((good["transform"], good["animationName"], good["filter"]), ("none", "none", "none"))
        self.assertTrue(good["messageHidden"])

        self.assertTrue(broken["shown"], broken)
        self.assertIn("couldn't play", broken["text"])
        self.assertEqual(broken["button"], "Retry")
        self.assertIn("Broken Movie (2002)/1s.mp4", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
