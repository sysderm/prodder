"""Locale robustness: on a comma-decimal locale (de_DE, fr_FR, …) `ps` prints
pcpu as "0,4", and a bare float() would throw and drop every agent. pfloat is
the parse-side belt to the LC_ALL=C suspenders in the scan subprocesses."""
import pytest

import prodtop


@pytest.mark.parametrize("text,expected", [
    ("0.4", 0.4),      # normal dot decimal (LC_ALL=C output)
    ("0,4", 0.4),      # comma decimal (de_DE/fr_FR ps output)
    ("12,5", 12.5),
    ("100", 100.0),    # integer, no separator
    ("0", 0.0),
])
def test_pfloat_parses(text, expected):
    assert prodtop.pfloat(text) == expected


@pytest.mark.parametrize("bad", ["abc", "", "1,2,3"])
def test_pfloat_rejects_garbage(bad):
    # Must raise like float() so the caller's `except ValueError` still fires.
    with pytest.raises(ValueError):
        prodtop.pfloat(bad)
