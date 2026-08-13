import json
from datetime import datetime

import mistune
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.config import STATIC_DIR, TEMPLATES_DIR, pulse_available
from app.text import canonical

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["from_json"] = lambda s: json.loads(s) if s else []
templates.env.filters["canonical"] = canonical

# Company Pulse hides itself when no web search is configured, rather than
# offering a button that can only fail. Resolved once at import: it depends
# only on environment settings, which don't change while the server runs.
templates.env.globals["pulse_available"] = pulse_available()


def _humandate(value) -> str:
    """Render a stored timestamp ('YYYY-MM-DD HH:MM:SS', UTC) as a short date
    like 'Jul 5, 2026'. Passes through anything it can't parse."""
    if not value:
        return "—"
    s = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).strftime("%b %-d, %Y")
        except ValueError:
            continue
    return s


templates.env.filters["humandate"] = _humandate

# LLM prose arrives as markdown; escape=True neutralizes any raw HTML in it.
_markdown = mistune.create_markdown(escape=True, plugins=["strikethrough", "table"])
templates.env.filters["md"] = lambda text: Markup(_markdown(text or ""))

# The word behind a 1–5 answer score, so a bare "2/5" reads as "2/5 Weak".
# Colours are driven separately by the score-chip s1–s5 classes (red/amber/green).
_ANSWER_BANDS = {1: "Poor", 2: "Weak", 3: "Fair", 4: "Strong", 5: "Excellent"}
templates.env.filters["answer_band"] = lambda score: _ANSWER_BANDS.get(score, "")


def _static_url(name: str) -> str:
    """/static/<name> plus an mtime cache-buster. HTML is no-store, so every
    page load carries current versions and an update busts browser caches
    immediately; stat() on each call (µs) keeps edits visible without a
    restart. Falls back to the bare path if the file is missing."""
    try:
        version = int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={version}"


templates.env.globals["static_url"] = _static_url


def _theme_pref(request=None) -> str:
    """The stored display preference ('system'|'light'|'dark'). base.html stamps
    it on <html> so the page paints in the right theme before any JS runs;
    app.js re-syncs localStorage from it (the database is the source of truth,
    localStorage only the anti-flash cache)."""
    from app.config import DEFAULT_USER_ID
    from app.db import get_conn

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT theme FROM users WHERE id = ?", (DEFAULT_USER_ID,)
        ).fetchone()
        return row["theme"] if row else ""
    except Exception:
        # Rendering must never die because the database is momentarily
        # unavailable; 'system' is a correct-looking fallback.
        return ""
    finally:
        conn.close()


templates.env.globals["theme_pref"] = _theme_pref


def _icon(name: str, size: int = 16, cls: str = "") -> Markup:
    # Lucide sprite (ISC license) at /static/icons.svg
    return Markup(
        f'<svg class="icon {cls}" width="{size}" height="{size}" aria-hidden="true">'
        f'<use href="{_static_url("icons.svg")}#{name}"/></svg>'
    )


templates.env.globals["icon"] = _icon
