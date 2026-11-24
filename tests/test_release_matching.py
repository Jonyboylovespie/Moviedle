"""Release-name matching, using real release names seen on prod.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ["AUTO_DOWNLOAD"] = "0"

import app  # noqa: E402

GB = 1024 ** 3


def result(name, size_gb=1.5, seeders=50):
    return {"fileName": name, "fileSize": int(size_gb * GB), "nbSeeders": seeders}


def accepted(name, title, year):
    """True if the release would be considered at all (matching title and not a pack)."""
    return bool(app._title_match(name, title, year)) and app._pack_reason(name, title, year) is None


class WrongFilmsRejected(unittest.TestCase):
    """Wrong films that the prod library downloaded under cf4e07e."""

    CASES = [
        ("Too.Small.To.Take.It.All.9.2017.3rd.Degree.720p", "It", "2017"),
        ("IT Chapter 1 And 2 2017-2019 720p BONE", "It", "2017"),
        ("It Comes at Night (2017) 1080p", "It", "2017"),
        ("Bluey 2018 S03E49 The Sign 1080p AUBC WEB-DL", "2.0", "2018"),
        ("Danger.Close.The.Battle.of.Long.Tan.2019.1080p", "Close", "2019"),
        ("The.Clean.Up.Crew.2024.720p.WEBRip", "Crew", "2024"),
        ("Inception.The.Cobol.Job.2010.720p", "Inception", "2010"),
        ("Chris Rock Bring The Pain (1996) 720p", "The Rock", "1996"),
        ("The Boy, the Mole, the Fox and the Horse (2022) 1080p", "X", "2022"),
        ("Up In The Air 2009 720p", "Up", "2009"),
        ("48 Hrs 1982 + Another 48 Hrs 1990 Remastered 720p", "48 Hrs.", "1982"),
        ("Batman Begins 2005 1080p", "Batman", "1989"),
        ("Kids Sacrifice Special 1986 720p", "The Sacrifice", "1986"),
    ]

    def test_rejected(self):
        for name, title, year in self.CASES:
            with self.subTest(name=name):
                self.assertFalse(accepted(name, title, year))


class RightFilmsAccepted(unittest.TestCase):
    """Correct releases on prod, including the 11 an earlier, stricter check rejected."""

    CASES = [
        ("48.Hrs.48.Hours.1982.x264", "48 Hrs.", "1982"),
        ("ET The Extra Terrestrial (1982) 720p", "E.T. the Extra-Terrestrial", "1982"),
        ("El.Camino.A.Breaking.Bad.Movie.2019.1080p", "El Camino", "2019"),
        ("Rambo.First.Blood.1982.REMASTERED.1080p", "First Blood", "1982"),
        ("Jeepers Creepers II 2003 720p BluRay", "Jeepers Creepers 2", "2003"),
        ("Die Hard 4: Live Free or Die Hard (2007) 720p", "Live Free or Die Hard", "2007"),
        ("March Of The Penguins-720p x264 HDDVD 2005-CC", "March of the Penguins", "2005"),
        ("James Bond: Never Say Never Again (1983) 720p", "Never Say Never Again", "1983"),
        ("Starsky And Hutch - Ben Stiller Comedy 2004 720p", "Starsky & Hutch", "2004"),
        ("The Last Of The Mohicans DDC (1992) 720p", "The Last of the Mohicans", "1992"),
        ("It.2017.1080p.BRRip.6CH.MkvCage", "It", "2017"),
        ("IT.2017.1080p.10bit.BluRay.x265-PSA", "It", "2017"),
        ("[YTS.MX] It (2017) 720p", "It", "2017"),
        ("Inception 2010 1080p AV1 10Bit DKong", "Inception", "2010"),
        ("The Rock (1996) 720p BrRip x264 - 850MB - YIFY", "The Rock", "1996"),
        ("X (2022) [1080p] [WEBRip] [5.1]", "X", "2022"),
        ("2.0.2018.1080p.WEBRip", "2.0", "2018"),
        ("2012 (2009) 720p BrRip x264 - 900MB - YIFY", "2012", "2009"),
        ("3:10 to Yuma (2007) 720p BrRip x264 - YIFY", "3:10 to Yuma", "2007"),
        ("A.Bugs.Life.1998.PROPER.1080p.BluRay.H264.AAC-[TGx]", "A Bug's Life", "1998"),
        ("Ambulance.2022.2022.720p.WEBRip.900MB.x264-GalaxyRG", "Ambulance", "2022"),
        ("American Gangster (2007) UNRATED 720p BrRip x264 - YIFY", "American Gangster", "2007"),
        ("Amelie (2001) 720p BRrip_sujaidr", "Amélie", "2001"),
        ("Austin Powers: International Man of Mystery (1997) 720p", "Austin Powers: International Man of Mystery", "1997"),
        ("Babe Pig In The City 1998 REMASTERED 1080p BluRay HEVC x265 5.1 BONE", "Babe: Pig in the City", "1998"),
        ("Batman and Robin (1997) 1080p BluRay x264 - 1.5GB - YIFY", "Batman & Robin", "1997"),
        ("Beauty and the Beast (2017) English [720p]", "Beauty and the Beast", "2017"),
        ("Blade Runner The Final Cut 1982 1080p", "Blade Runner", "1982"),
        ("Blade.Runner.2049.2017.1080p", "Blade Runner 2049", "2017"),
        ("The Lord of the Rings The Fellowship of the Ring Extended Edition 2001 1080p",
         "The Lord of the Rings: The Fellowship of the Ring", "2001"),
        ("Andrej Tarkovskij's The Sacrifice (1986) - 1080p x265 HEVC - SWE", "The Sacrifice", "1986"),
        ("Lord.of.the.Rings.The.Two.Towers.2002.1080p", "The Lord of the Rings: The Two Towers", "2002"),
        ("Up.2009.720p.BluRay", "Up", "2009"),
        ("Her.2013.1080p", "Her", "2013"),
    ]

    def test_accepted(self):
        for name, title, year in self.CASES:
            with self.subTest(name=name):
                self.assertTrue(accepted(name, title, year))


class Picking(unittest.TestCase):
    def test_it_2017_picks_the_film_not_smaller_lookalikes(self):
        results = [
            result("Too.Small.To.Take.It.All.9.2017.3rd.Degree.720p", 1.35, 19),
            result("IT Chapter 1 And 2 2017-2019 720p BONE", 1.98, 71),
            result("It.2017.1080p.BRRip.6CH.MkvCage", 2.60, 154),
            result("It Comes at Night (2017) 1080p", 2.16, 34),
            result("IT.2017.1080p.10bit.BluRay.x265-PSA", 2.29, 31),
        ]
        best = app._pick_best_result(results, "It", "2017")
        self.assertEqual(best["fileName"], "It.2017.1080p.BRRip.6CH.MkvCage")

    def test_title_first_name_beats_title_elsewhere(self):
        results = [
            result("Rambo.First.Blood.1982.720p", 0.8, 100),
            result("First.Blood.1982.720p", 1.4, 20),
        ]
        best = app._pick_best_result(results, "First Blood", "1982")
        self.assertEqual(best["fileName"], "First.Blood.1982.720p")

    def test_title_elsewhere_used_when_nothing_better(self):
        results = [result("El.Camino.A.Breaking.Bad.Movie.2019.1080p", 2.0, 80)]
        best = app._pick_best_result(results, "El Camino", "2019")
        self.assertEqual(best["fileName"], "El.Camino.A.Breaking.Bad.Movie.2019.1080p")

    def test_off_by_one_year_only_without_exact_match(self):
        near = result("Some Film 2019 1080p", 1.0, 100)
        exact = result("Some Film 2018 1080p", 3.0, 20)
        self.assertEqual(app._pick_best_result([near, exact], "Some Film", "2018"), exact)
        self.assertEqual(app._pick_best_result([near], "Some Film", "2018"), near)

    def test_over_5_gb_rejected(self):
        results = [result("Jeepers Creepers 2 2003 1080p BluRay REMUX", 26.84, 8)]
        self.assertIsNone(app._pick_best_result(results, "Jeepers Creepers 2", "2003"))
        results = [result("The.Sacrifice.1986.REMASTERED.720p.BluRay", 5.47, 7)]
        self.assertIsNone(app._pick_best_result(results, "The Sacrifice", "1986"))

    def test_one_word_wrong_films_skip_instead(self):
        results = [result("The.Clean.Up.Crew.2024.720p.WEBRip", 1.0, 50)]
        self.assertIsNone(app._pick_best_result(results, "Crew", "2024"))


if __name__ == "__main__":
    unittest.main()
