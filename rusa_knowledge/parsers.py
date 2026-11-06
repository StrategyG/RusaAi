"""Bounded local-file parsers. Remote URLs are never fetched during ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 2_000_000

_MEDIA_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    source_uri: str
    content: str
    metadata: dict[str, str]


def parse_file(path: str | Path, *, source_uri: str | None = None) -> ParsedDocument:
    """Parse a local TXT, Markdown, HTML, PDF, or DOCX file within strict size limits."""
    file_path = Path(path)
    if file_path.is_symlink() or not file_path.is_file():
        raise ValueError("input must be a regular local file, not a symlink")
    suffix = file_path.suffix.lower()
    if suffix not in _MEDIA_TYPES:
        raise ValueError("supported file types are TXT, MD, HTML, PDF, and DOCX")

    size = file_path.stat().st_size
    if size < 1 or size > MAX_DOCUMENT_BYTES:
        raise ValueError("document must contain between 1 byte and 10 MiB")

    raw = file_path.read_bytes()
    if suffix in {".txt", ".md"}:
        content = raw.decode("utf-8-sig", errors="strict")
        parser = "utf-8"
    elif suffix in {".html", ".htm"}:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
        for element in soup(["script", "style", "noscript", "svg"]):
            element.decompose()
        content = soup.get_text("\n", strip=True)
        parser = "beautifulsoup4"
    elif suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise ValueError("encrypted PDF files are not supported")
        if len(reader.pages) > 5000:
            raise ValueError("PDF exceeds the 5000-page parsing limit")
        content = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        parser = "pypdf"
    else:
        from docx import Document

        try:
            with ZipFile(BytesIO(raw)) as archive:
                entries = archive.infolist()
                expanded_size = sum(entry.file_size for entry in entries)
                if len(entries) > 10_000 or expanded_size > 50 * 1024 * 1024:
                    raise ValueError("DOCX archive exceeds safe expansion limits")
                if any(
                    entry.file_size > 25 * 1024 * 1024
                    or (entry.compress_size == 0 and entry.file_size > 0)
                    or (
                        entry.compress_size > 0
                        and entry.file_size / entry.compress_size > 1000
                    )
                    for entry in entries
                ):
                    raise ValueError("DOCX archive contains an unsafe compression ratio")
        except BadZipFile as exc:
            raise ValueError("DOCX file is not a valid ZIP archive") from exc

        document = Document(BytesIO(raw))
        blocks = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    blocks.append(" | ".join(cells))
        content = "\n".join(blocks)
        parser = "python-docx"

    content = content.replace("\x00", " ").strip()
    if not content:
        raise ValueError("no readable text was extracted from the document")
    if len(content) > MAX_EXTRACTED_CHARS:
        raise ValueError("extracted document text exceeds the 2 million character limit")

    resolved = file_path.resolve()
    return ParsedDocument(
        title=file_path.stem[:256],
        source_uri=(source_uri or resolved.as_uri())[:2048],
        content=content,
        metadata={
            "filename": file_path.name[:255],
            "media_type": _MEDIA_TYPES[suffix],
            "parser": parser,
            "file_size_bytes": str(size),
        },
    )
