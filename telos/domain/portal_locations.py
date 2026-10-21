"""Area names to try on property portals for a free-text location.

Portals key their area pages on a place name ("Marylebone") or a postcode
district ("W1"), not on both together: "Marylebone W1" is a 404 on Rightmove
and OnTheMarket. Callers try each candidate in order until one returns
recognised search results.
"""

import re

_DISTRICT_RE = re.compile(r"[a-z]{1,2}\d[a-z\d]?", re.IGNORECASE)  # W1, W1U, SW11, EC1A
_INWARD_RE = re.compile(r"\d[a-z]{2}", re.IGNORECASE)  # the "4AB" of "W2 4AB"
_NOISE = {"london", "uk", "england"}


def location_candidates(location: str) -> list[str]:
    """``location`` as given, then each postcode district in it and the place name without it.

    "Notting Hill, London" also tries the part before the first comma. A name that merely
    contains a noise word ("London Bridge") is never shortened.
    """
    words = re.findall(r"[^\W_]+", location)
    if not words:
        return []
    candidates = [" ".join(words)]
    districts = [w for w in words if _DISTRICT_RE.fullmatch(w)]
    if districts:
        candidates += districts
        place = [w for w in words if w not in districts and not _INWARD_RE.fullmatch(w) and w.lower() not in _NOISE]
        if place:
            candidates.append(" ".join(place))
    elif "," in location:
        head = re.findall(r"[^\W_]+", location.split(",", 1)[0])
        if head:
            candidates.append(" ".join(head))
    return list(dict.fromkeys(candidates))
