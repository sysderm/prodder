"""Locale robustness: on a comma-decimal locale (de_DE, fr_FR, …) `ps` prints
pcpu as "0,4", and a bare float() would throw and drop every agent. pfloat is
the parse-side belt to the LC_ALL=C suspenders in the scan subprocesses.

Stdlib only (unittest) — no third-party deps, so CI needs no install step."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import prodtop  # noqa: E402


class Pfloat(unittest.TestCase):
    def test_parses_dot_comma_and_int(self):
        cases = {"0.4": 0.4, "0,4": 0.4, "12,5": 12.5, "100": 100.0, "0": 0.0}
        for text, expected in cases.items():
            self.assertEqual(prodtop.pfloat(text), expected, text)

    def test_rejects_garbage_like_float(self):
        # Must raise ValueError so callers' `except ValueError` still fires.
        for bad in ("abc", "", "1,2,3"):
            with self.assertRaises(ValueError, msg=bad):
                prodtop.pfloat(bad)


if __name__ == "__main__":
    unittest.main()
