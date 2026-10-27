"""Storing uploaded documents safely and reading text out of resumes."""
from __future__ import annotations

import io
import re
import secrets
import zipfile
from pathlib import Path

from fastapi import HTTPException

from .config import settings

# extension -> (content type, magic bytes check)
ALLOWED = {
    "pdf": ("application/pdf", lambda b: b.startswith(b"%PDF")),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             lambda b: b.startswith(b"PK\x03\x04") and _is_docx(b)),
    "png": ("image/png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    "jpg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "jpeg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
}
KINDS = ("resume", "certificate", "portfolio", "other")


def _is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "word/document.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def validate(filename: str, data: bytes) -> tuple[str, str]:
    """Returns (extension, content type) or raises a 422 the person can act on."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED:
        raise HTTPException(422, "Upload a PDF, Word (.docx), PNG or JPG file.")
    if len(data) == 0:
        raise HTTPException(422, "That file is empty.")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"Files can be up to {settings.max_upload_mb} MB. Try saving a smaller PDF.")
    ctype, check = ALLOWED[ext]
    if not check(data):
        raise HTTPException(422, f"This file doesn't look like a real .{ext} file. Export it again and retry.")
    return ext, ctype


def save(user_id: int, ext: str, data: bytes) -> str:
    folder = settings.upload_dir / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.{ext}"
    (folder / name).write_bytes(data)
    return f"{user_id}/{name}"


def path_for(stored_name: str) -> Path:
    p = (settings.upload_dir / stored_name).resolve()
    if settings.upload_dir.resolve() not in p.parents:
        raise HTTPException(404, "File not found.")
    return p


def remove(stored_name: str) -> None:
    try:
        path_for(stored_name).unlink(missing_ok=True)
    except HTTPException:
        pass


def safe_download_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name)[:120] or "document"


def extract_text(ext: str, data: bytes) -> str | None:
    """Plain text from a PDF or Word file, for matching and scoring. Images return None."""
    try:
        if ext == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:15])
        elif ext == "docx":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            text = re.sub(r"<[^>]+>", "", xml)
        else:
            return None
    except Exception:
        return None
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:50_000] or None
