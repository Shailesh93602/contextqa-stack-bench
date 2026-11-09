import math
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import score  # noqa: E402

FIXTURES = HERE / "fixtures"
ROOT = HERE.parent.parent

# (k, n) -> Wilson 95% (lower, upper), the sample-size table of the method.
KNOWN = {(10, 12): (0.5520, 0.9530), (16, 20): (0.5840, 0.9193), (24, 30): (0.6269, 0.9050),
         (40, 50): (0.6696, 0.8876), (30, 30): (0.8865, 1.0), (0, 5): (0.0, 0.4345)}


def matches_known(fn):
    return all(abs(fn(k, n)[0] - lo) < 5e-4 and abs(fn(k, n)[1] - hi) < 5e-4 for (k, n), (lo, hi) in KNOWN.items())


def normal_approx(k, n, z=1.96):
    p = k / n
    h = z * math.sqrt(p * (1 - p) / n)
    return max(0.0, p - h), min(1.0, p + h)


def no_centre_shift(k, n, z=1.96):
    p = k / n
    denom = 1 + z * z / n
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, p / denom - half), min(1.0, p / denom + half)


def fixture_score(split="heldout"):
    apps = score.load_apps(FIXTURES / "apps")
    return score.score(score.read_results(FIXTURES / "results.jsonl"), apps, split, 5)


def row(lines, group, capability):
    hits = [l for l in lines if l.split()[:2] == [group, capability]]
    assert len(hits) == 1, (group, capability, hits)
    return hits[0]


class WilsonTest(unittest.TestCase):
    def test_known_values(self):
        self.assertTrue(matches_known(score.wilson))

    def test_mutation_is_caught(self):
        # A broken formula must fail the same check that the real one passes.
        self.assertFalse(matches_known(normal_approx))
        self.assertFalse(matches_known(no_centre_shift))
        with mock.patch.object(score, "wilson", normal_approx):
            self.assertNotEqual(score.fmt_ci(24, 30), "≥ 62% (24/30, CI 62–91%)")

    def test_never_prints_100_as_the_score(self):
        self.assertTrue(score.fmt_ci(30, 30).startswith("≥ 88% "))
        self.assertIsNone(score.wilson(0, 0))
        self.assertEqual(score.fmt_ci(0, 0), "not measured")


class BandTest(unittest.TestCase):
    def test_ladder(self):
        self.assertEqual(score.band(0.85, 0.9), "Works")
        self.assertEqual(score.band(0.85, 0.7), "Partial")      # consistency below 80%
        self.assertEqual(score.band(0.85, None), "Partial")     # consistency not measured
        self.assertEqual(score.band(0.85, 0.9, all_measured=False), "Partial")
        self.assertEqual(score.band(0.49, 1.0), "Not yet")
        self.assertEqual(score.band(None, None), "Not measured")


class LoaderTest(unittest.TestCase):
    def test_yaml_subset(self):
        key = score.load_yaml((FIXTURES / "apps/alpha/answer-key.yaml").read_text())
        b1, b2 = key["bugs"][0], key["bugs"][1]
        self.assertEqual(b1["symptom"], "Dates shift by one day # not a comment")
        self.assertEqual(b1["layer"], ["UI", "DB"])
        self.assertEqual(b1["fix_urls"], ["https://github.com/example/alpha/pull/2"])
        self.assertIs(b1["compare_verified"], True)
        self.assertEqual(b2["symptom"], 'Save does nothing, says "done"')
        self.assertEqual(key["bugs"][2]["split"], "dev")

    def test_validator_catches_errors(self):
        schema = score.json.loads((ROOT / "schema/answer-key.schema.json").read_text())
        key = score.load_yaml((FIXTURES / "apps/alpha/answer-key.yaml").read_text())
        self.assertEqual(score.validate(key, schema), [])
        del key["bugs"][0]["fixed_at"]
        key["bugs"][1]["split"] = "test"
        errors = score.validate(key, schema)
        self.assertTrue(any("missing fixed_at" in e for e in errors), errors)
        self.assertTrue(any("'test' not in" in e for e in errors), errors)

    def test_published_answer_keys_and_recipes_are_valid(self):
        apps = score.load_apps(ROOT / "apps")
        self.assertGreaterEqual(len(apps), 10)
        self.assertEqual(score.check_apps(apps, ROOT / "schema"), [])
        self.assertEqual(score.main(["--check"]), 0)


class ScoreTest(unittest.TestCase):
    def test_pair_and_consistency(self):
        lines, warnings = fixture_score()
        # alpha-1 correct; alpha-2 false reproduce on fixed; beta-1 correct on run 0 then flips.
        r = row(lines, "all", "repro")
        self.assertIn("(2/3,", r)
        self.assertIn(" 2/3 ", r)   # stable: alpha-1, alpha-2
        self.assertIn(" 1/3 ", r)   # pass^5: alpha-1 only
        self.assertIn("(1/1,", row(lines, "all", "verify_fix"))
        self.assertTrue(any("beta-1 verify_fix: runs [0] miss" in w for w in warnings), warnings)

    def test_rollups(self):
        lines, _ = fixture_score()
        self.assertIn("(2/3,", row(lines, "node", "repro"))         # tech shared by both apps
        self.assertIn("(1/2,", row(lines, "react", "repro"))        # alpha only
        self.assertIn("(1/1,", row(lines, "timezone", "repro"))     # feature tag
        self.assertIn("(2/3,", row(lines, "sign-in-password", "repro"))  # sign-in type from the recipe
        self.assertIn("(1/1,", row(lines, "beta", "repro"))

    def test_split_filter(self):
        lines, warnings = fixture_score("heldout")
        self.assertTrue(any("1 bugs have no split yet" in w for w in warnings), warnings)
        self.assertIn("(1/1,", row(fixture_score("dev")[0], "all", "repro"))
        lines, warnings = fixture_score("all")
        self.assertIn("(3/5,", row(lines, "all", "repro"))  # + alpha-3 correct, beta-2 INTERNAL_ERROR wrong
        self.assertTrue(any("correct=True disagrees" in w for w in warnings), warnings)

    def test_headline_counts_unmeasured_capabilities_as_zero(self):
        lines, _ = fixture_score()
        head = [l for l in lines if "HEADLINE" in l][0]
        self.assertIn("3/6 capabilities measured", head)
        self.assertTrue(head.endswith("Not yet"), head)

    def test_mixed_stamps_are_refused(self):
        rows = score.read_results(FIXTURES / "results.jsonl")
        rows[0]["model"] = "model-y"
        with self.assertRaises(ValueError):
            score.score(rows, score.load_apps(FIXTURES / "apps"))


if __name__ == "__main__":
    unittest.main()
