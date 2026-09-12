"""Indian places that answer to two names.

"Baroda" and "Vadodara" are the same city, and a cost centre called one is not
found by a search for the other. Fuzzy matching does not help - the two strings
share almost no characters - so the pairs are listed.

Used only to *suggest*: an alias is never silently substituted, because a cost
centre named Baroda and one named Vadodara can legitimately both exist.
"""

from __future__ import annotations

#: Each tuple is one place. Order inside a tuple carries no meaning.
_PAIRS: tuple[tuple[str, ...], ...] = (
    ("Baroda", "Vadodara"),
    ("Bombay", "Mumbai"),
    ("Calcutta", "Kolkata"),
    ("Madras", "Chennai"),
    ("Bangalore", "Bengaluru"),
    ("Poona", "Pune"),
    ("Mysore", "Mysuru"),
    ("Mangalore", "Mangaluru"),
    ("Belgaum", "Belagavi"),
    ("Hubli", "Hubballi"),
    ("Gurgaon", "Gurugram"),
    ("Cochin", "Kochi"),
    ("Trivandrum", "Thiruvananthapuram"),
    ("Calicut", "Kozhikode"),
    ("Trichy", "Tiruchirappalli"),
    ("Pondicherry", "Puducherry"),
    ("Simla", "Shimla"),
    ("Cawnpore", "Kanpur"),
    ("Benares", "Varanasi"),
    ("Allahabad", "Prayagraj"),
    ("Orissa", "Odisha"),
    ("Gauhati", "Guwahati"),
    ("Jubbulpore", "Jabalpur"),
    ("Panjim", "Panaji"),
    ("Ahmedabad", "Amdavad"),
)

#: lower-cased name -> every other name for the same place.
OTHER_NAMES: dict[str, tuple[str, ...]] = {
    name.lower(): tuple(other for other in group if other != name)
    for group in _PAIRS
    for name in group
}


def other_names(name: str) -> tuple[str, ...]:
    """Every other name the same place goes by. Empty when there is none."""
    return OTHER_NAMES.get(name.strip().lower(), ())


def matches_known(name: str, known: set[str] | dict[str, object]) -> list[str]:
    """Which of ``known`` are the same place under a different name."""
    lowered = {str(k).lower(): str(k) for k in known}
    return [
        lowered[alias.lower()]
        for alias in other_names(name)
        if alias.lower() in lowered
    ]
