import logging
import uuid
from pathlib import Path

from app.config import MAX_UPLOAD_BYTES
from app.db import get_conn
from app.services.storage import get_storage
from app.user_errors import USER_ERROR_PARSE

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


class UnsupportedFileType(Exception):
    pass


class FileTooLarge(Exception):
    pass


def save_upload(filename: str, content: bytes, purpose: str = "profile", *, user_id: int) -> int:
    """Store the raw file (storage seam: local disk or DO Spaces) and create a
    document row (status 'uploaded'). documents.path holds the storage KEY."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise FileTooLarge(
            f"That file is too large. Uploads must be under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileType(f"Unsupported file type {ext!r}; allowed: pdf, docx, txt, md")
    key = f"{uuid.uuid4().hex}{ext}"
    get_storage().save(key, content)

    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO documents (user_id, purpose, filename, path, mime_type, busy_since) "
            "VALUES (?, ?, ?, ?, ?, datetime('now')) RETURNING id",
            (user_id, purpose, Path(filename).name, key, MIME_BY_EXT[ext]),
        )
        document_id = cur.fetchone()[0]
        conn.commit()
        return document_id
    finally:
        conn.close()


def parse_text(key: str, content: bytes) -> str:
    """Extract text from an upload's raw bytes (extension decides the parser)."""
    import io

    ext = Path(key).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if ext == ".docx":
        import docx

        document = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in document.paragraphs).strip()
    return content.decode(errors="replace").strip()


def parse_document(document_id: int) -> str:
    """Parse a stored document to text and persist it. Returns the text."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            raise ValueError(f"document {document_id} not found")
        conn.execute(
            "UPDATE documents SET status = 'parsing', error = NULL, "
            "busy_since = datetime('now') WHERE id = ?",
            (document_id,),
        )
        conn.commit()
        try:
            text = parse_text(row["path"], get_storage().read(row["path"]))
            if not text:
                raise ValueError("no text could be extracted from this file")
        except Exception:
            # The raw error (filesystem paths, pypdf/library internals) goes to
            # the log; the user-rendered `error` column gets curated copy only.
            log.exception("document %s failed to parse", document_id)
            conn.execute(
                "UPDATE documents SET status = 'error', error = ? WHERE id = ?",
                (USER_ERROR_PARSE, document_id),
            )
            conn.commit()
            raise
        conn.execute("UPDATE documents SET parsed_text = ? WHERE id = ?", (text, document_id))
        conn.commit()
        return text
    finally:
        conn.close()


def recompute_orphaned(conn, user_id: int) -> None:
    """Sync the `orphaned` flag to provenance: a machine fact with no fact_sources
    row is orphaned; one with a source is not. user_edited facts are left alone —
    the user adopted them (update_fact sets user_edited=1, orphaned=0). Does not
    commit; the caller owns the transaction. Lives here (not profile.py) so both
    process_document and delete_document can reuse it without an import cycle.
    Scoped to user_id so it stays correct once more than user 1 exists."""
    conn.execute(
        """UPDATE profile_facts SET orphaned = 1
           WHERE user_id = ? AND user_edited = 0
             AND id NOT IN (SELECT fact_id FROM fact_sources)""",
        (user_id,),
    )
    conn.execute(
        """UPDATE profile_facts SET orphaned = 0
           WHERE user_id = ? AND user_edited = 0
             AND id IN (SELECT fact_id FROM fact_sources)""",
        (user_id,),
    )


def delete_document(document_id: int, user_id: int) -> int:
    """Delete a document and its provenance links. Facts still sourced by other
    documents keep those tags; facts left with no source become orphaned (kept,
    offered for removal). Returns the count of machine facts newly left unsourced."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT path FROM documents WHERE id = ? AND user_id = ?", (document_id, user_id)
        ).fetchone()
        if row is None:  # missing or not owned by this user
            return 0
        # Machine facts sourced ONLY by this document — they lose their last source.
        newly_unsourced = conn.execute(
            """SELECT COUNT(*) FROM profile_facts f
               WHERE f.user_id = ? AND f.user_edited = 0
                 AND EXISTS (SELECT 1 FROM fact_sources s
                             WHERE s.fact_id = f.id AND s.document_id = ?)
                 AND NOT EXISTS (SELECT 1 FROM fact_sources s
                                 WHERE s.fact_id = f.id AND s.document_id <> ?)""",
            (user_id, document_id, document_id),
        ).fetchone()[0]
        # Explicit for intent; ON DELETE CASCADE is the backstop.
        conn.execute("DELETE FROM fact_sources WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        recompute_orphaned(conn, user_id)
        conn.commit()
        get_storage().delete(row["path"])
    finally:
        conn.close()
    from app.services import insights

    insights.mark_stale(user_id)  # orphaned facts drop out of insights evidence
    return newly_unsourced
