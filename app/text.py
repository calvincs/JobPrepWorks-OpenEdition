"""Small text helpers with no service dependencies (import-cycle safe)."""

import re
import unicodedata


def canonical(s: str) -> str:
    """Normalize a name for matching: lowercase, collapse whitespace."""
    return " ".join(s.lower().split())


def safe_external_url(url: str | None) -> str | None:
    """Return the URL only if it's a plain http(s) link, else None. Guards the
    stored job URL that gets rendered into an <a href> — without this a
    'javascript:'/'data:' scheme would survive into the DOM (A05/XSS); the CSP
    blocks execution today, but this removes the single point of failure."""
    if not url:
        return None
    from urllib.parse import urlsplit

    url = url.strip()
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None


# Trailing legal designators that don't distinguish employers. Deliberately
# conservative: "group"/"holdings"/"labs" stay, since those often ARE the
# distinguishing part of the name.
_COMPANY_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "plc", "pbc",
    "corp", "corporation", "co", "company", "gmbh", "ag", "sa", "srl", "bv", "nv",
})


def canonical_company(s: str) -> str:
    """Normalize an employer name for cache matching: unicode-fold, lowercase,
    drop punctuation (keeping & and +), strip a leading "the" and trailing
    legal suffixes — so "The Acme Co., Inc." and "acme" hit the same row."""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[^\w\s&+]", " ", s)
    words = s.split()
    if words and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def normalize_posting(text: str) -> str:
    """Tidy a pasted/parsed job posting for storage and display: normalize
    newlines, strip trailing whitespace per line, collapse 3+ blank lines to a
    single blank line, and trim leading/trailing blank lines. Preserves the
    paragraph/line structure so it round-trips well in a <pre>."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    blanks = 0
    for line in lines:
        line = line.rstrip()
        if line:
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks == 1:  # keep at most one blank line between paragraphs
                out.append("")
    return "\n".join(out).strip()
