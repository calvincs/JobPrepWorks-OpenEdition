"""Curated, user-facing error copy for the async pipelines.

These strings are the ONLY thing that may reach a status/`error` column that a
template renders. Exception text, stack traces, SQL fragments, upstream API
bodies, and internal hostnames go to the logs (`log.exception(...)`), never to
the browser — that class of leak is A10 (Mishandling of Exceptional Conditions)
/ LLM02 (Sensitive Information Disclosure). Company Pulse established this split
(`app/services/pulse.py`, `USER_ERROR_*`); this module lets every other pipeline
reuse the same discipline. Dependency-free leaf module (no import cycles).
"""

# Generic terminal-failure copy for a pipeline that hit an unexpected error.
USER_ERROR_GENERIC = "Something went wrong on our side — try again."

# Document parsing / extraction.
USER_ERROR_PARSE = (
    "Couldn't read this file — it may be corrupt, empty, or password-protected. "
    "Try re-saving it as a PDF, DOCX, or plain text."
)

# A versioned insert (fit analysis, study guide) lost every retry to a race.
USER_ERROR_RACE = "This raced with another run — please try again."

# The stale-pipeline reaper (services/reaper.py) found work stranded in-flight
# by a crashed or redeployed server. Always retryable.
USER_ERROR_INTERRUPTED = "This was interrupted by a restart — try again."

# Upload rejected for being too large (see MAX_UPLOAD_BYTES in app/config.py).
USER_ERROR_TOO_LARGE = "That file is too large. Uploads must be under {mb} MB."

# The optional local daily brake (LLM_DAILY_LIMIT in .env, off by default).
# Nothing here is a plan or an entitlement — it is a spend guard the person
# running the app set for themselves, so the copy says exactly that.
USER_ERROR_QUOTA = (
    "You've hit the daily AI limit you set (LLM_DAILY_LIMIT). "
    "It resets at UTC midnight — or raise it in your .env and restart."
)
