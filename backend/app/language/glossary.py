"""Terms machine translation must not guess.

NLLB-200 mistranslates Kinyarwanda animal names ("imvubu" -> "buffalo", "imparage" -> "pages",
"intwiga" -> "lessons", "inkura" -> "adults") and turns video positions like "24:00" into clock
times ("6 pm"). Before translating we swap these terms for fixed translations or placeholders
("animals (A1)", "(T1)" survive NLLB unchanged) and restore them afterwards.

The Kinyarwanda names below should be reviewed by a native speaker; extend the table as needed.
"""

from __future__ import annotations

import re

RW_TO_EN = {
    "imvubu": "hippopotamus",
    "inzovu": "elephant",
    "imparage": "zebra",
    "intwiga": "giraffe",
    "inkura": "rhinoceros",
    "imbogo": "buffalo",
    "intare": "lion",
    "ingwe": "leopard",
}

# English name (and plural forms) -> Kinyarwanda. Longest first so "african elephant" wins over "elephant".
_EN_TO_RW = {
    "hippopotamuses": "imvubu", "hippopotami": "imvubu", "hippopotamus": "imvubu", "hippos": "imvubu", "hippo": "imvubu",
    "african elephants": "inzovu", "african elephant": "inzovu", "elephants": "inzovu", "elephant": "inzovu",
    "plains zebras": "imparage", "plains zebra": "imparage", "zebras": "imparage", "zebra": "imparage",
    "giraffes": "intwiga", "giraffe": "intwiga",
    "black rhinoceros": "inkura", "white rhinoceros": "inkura", "rhinoceroses": "inkura", "rhinoceros": "inkura", "rhinos": "inkura", "rhino": "inkura",
    "african buffaloes": "imbogo", "african buffalo": "imbogo", "buffaloes": "imbogo", "buffalo": "imbogo",
    "lions": "intare", "lion": "intare",
    "leopards": "ingwe", "leopard": "ingwe",
}
_EN_PATTERN = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(_EN_TO_RW, key=len, reverse=True)) + r")\b", re.IGNORECASE)
_RW_PATTERN = re.compile(r"\b(" + "|".join(RW_TO_EN) + r")\b", re.IGNORECASE)
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")


# NLLB turns "mu minota icumi ishize" (in the last ten minutes) into "ten minutes ago" and drops "abantu"
# (people) from "Hari abantu bangahe?" ("How many are there?"): these phrases are put in English first.
_RW_NUMBERS = {
    "rimwe": 1, "umwe": 1, "abiri": 2, "kabiri": 2, "atatu": 3, "gatatu": 3, "ane": 4, "kane": 4, "atanu": 5, "gatanu": 5,
    "atandatu": 6, "arindwi": 7, "umunani": 8, "icyenda": 9, "icumi": 10, "cumi n'itanu": 15, "makumyabiri": 20,
    "mirongo itatu": 30, "mirongo ine": 40, "mirongo itanu": 50, "mirongo itandatu": 60,
}
_NUM = r"(\d+|" + "|".join(re.escape(k) for k in sorted(_RW_NUMBERS, key=len, reverse=True)) + r")"
_PHRASES = [
    (re.compile(rf"\bmu (?:minota|munota) {_NUM} (?:ishize|ushize|iheruka)\b", re.IGNORECASE), "in the last {n} minutes"),
    (re.compile(r"\bmu munota (?:ushize|uheruka)\b", re.IGNORECASE), "in the last minute"),
    (re.compile(rf"\bmu masaha {_NUM} (?:ashize|aheruka)\b", re.IGNORECASE), "in the last {n} hours"),
    (re.compile(r"\bmu isaha (?:ishize|iheruka)\b", re.IGNORECASE), "in the last hour"),
    (re.compile(r"\babantu\b", re.IGNORECASE), "people"),
    (re.compile(r"\bumuntu\b", re.IGNORECASE), "person"),
]


def rw_terms_to_english(text: str) -> str:
    for pattern, english in _PHRASES:
        text = pattern.sub(lambda m, e=english: e.format(n=_RW_NUMBERS.get(m.group(1).lower(), m.group(1)) if m.groups() else ""), text)
    return _RW_PATTERN.sub(lambda m: RW_TO_EN[m.group(1).lower()], text)


def protect_english(text: str) -> tuple[str, dict[str, str]]:
    """Replace animal names and video times with placeholders; returns (text, placeholder -> Kinyarwanda/original)."""
    slots: dict[str, str] = {}

    def animal(m: re.Match[str]) -> str:
        key = f"A{sum(1 for k in slots if k.startswith('A')) + 1}"
        slots[key] = _EN_TO_RW[m.group(1).lower()]
        return f"animals ({key})"

    def time(m: re.Match[str]) -> str:
        key = f"T{sum(1 for k in slots if k.startswith('T')) + 1}"
        slots[key] = m.group(0)
        return f"({key})"

    return _TIME.sub(time, _EN_PATTERN.sub(animal, text)), slots


def restore(text: str, slots: dict[str, str]) -> str:
    for key, value in slots.items():
        text = re.sub(rf"\(\s*{key}\s*\)", f"({value})" if key.startswith("A") else value, text)
    return text
