"""Official ratings -> minimum age, for profile ceilings (Vesper kids profiles).

Jellyfin stores whatever the metadata provider returned: "PG-13", "TV-MA", "12", "NL-12",
"FSK 16", "GB-15", "12A", "AL"... This normalises the common systems (US MPA and TV
guidelines, NL Kijkwijzer, DE FSK, UK BBFC, FR, AU) to an age. Unknown or unrated -> None,
which a ceiling treats as "not allowed" unless allow_unrated is set.
"""

import re

_TABLE = {
    # US MPA + TV parental guidelines
    "G": 0, "PG": 8, "PG-13": 13, "R": 17, "NC-17": 18,
    "TV-Y": 0, "TV-Y7": 7, "TV-Y7-FV": 7, "TV-G": 0, "TV-PG": 8, "TV-14": 14, "TV-MA": 17,
    # NL Kijkwijzer / BE
    "AL": 0, "KT": 0, "KNT": 12, "ALL": 0,
    # UK BBFC
    "U": 0, "UC": 0, "12A": 12, "R18": 18,
    # AU
    "E": 0, "M": 15, "MA15+": 15, "R18+": 18, "X18+": 18,
    # FR
    "TP": 0, "TOUS PUBLICS": 0,
    # misc
    "APPROVED": 0, "PASSED": 0, "ALL AGES": 0,
}
_UNRATED = {"NR", "UR", "UNRATED", "NOT RATED", "N/A", ""}
_PREFIX = re.compile(r"^(?:[A-Z]{2}|FSK|KIJKWIJZER|BBFC|CNC|MPAA|MPA|OFLC|ACB)[\s\-/:]+")


def min_age(rating: str | None) -> int | None:
    if rating is None:
        return None
    r = rating.strip().upper()
    if r in _UNRATED:
        return None
    for _ in range(2):  # "DE-FSK 12" has two prefixes
        if r in _TABLE:
            return _TABLE[r]
        stripped = _PREFIX.sub("", r)
        if stripped == r:
            break
        r = stripped
    if r in _TABLE:
        return _TABLE[r]
    m = re.fullmatch(r"(\d{1,2})\s*\+?A?", r)
    if m:
        return int(m.group(1))
    return None


def within(rating: str | None, max_age: int | None, allow_unrated: bool = False) -> bool:
    if max_age is None:
        return True
    age = min_age(rating)
    if age is None:
        return allow_unrated
    return age <= max_age
