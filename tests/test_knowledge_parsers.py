import pytest

import rusa_knowledge.parsers as parsers
from rusa_knowledge.parsers import parse_file


def test_parse_text_file(tmp_path):
    path = tmp_path / "guide.txt"
    path.write_text("RUSA knowledge source.", encoding="utf-8")
    parsed = parse_file(path)
    assert parsed.title == "guide"
    assert parsed.content == "RUSA knowledge source."
    assert parsed.metadata["media_type"] == "text/plain"
    assert parsed.source_uri.startswith("file://")


def test_parse_html_removes_scripts(tmp_path):
    path = tmp_path / "guide.html"
    path.write_text(
        "<html><body><h1>Knowledge</h1><script>steal()</script>"
        "<p>Useful text</p></body></html>",
        encoding="utf-8",
    )
    parsed = parse_file(path)
    assert "Knowledge" in parsed.content
    assert "Useful text" in parsed.content
    assert "steal" not in parsed.content


def test_parser_rejects_symlink(tmp_path):
    target = tmp_path / "source.txt"
    target.write_text("source", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    try:
        parse_file(link)
    except ValueError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("symlink was accepted")



def test_pdf_extraction_stops_at_character_limit(tmp_path, monkeypatch):
    path = tmp_path / "large.pdf"
    path.write_bytes(b"%PDF-test")

    class Page:
        def extract_text(self):
            return "0123456789"

    class Reader:
        def __init__(self, *args, **kwargs):
            self.is_encrypted = False
            self.pages = [Page()]

    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    monkeypatch.setattr(parsers, "MAX_EXTRACTED_CHARS", 5)
    with pytest.raises(ValueError, match="2 million character limit"):
        parse_file(path)


def test_docx_extraction_stops_at_character_limit(tmp_path, monkeypatch):
    from zipfile import ZipFile

    path = tmp_path / "large.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")

    class Paragraph:
        text = "0123456789"

    class Document:
        paragraphs = [Paragraph()]
        tables = []

    import docx

    monkeypatch.setattr(docx, "Document", lambda *args, **kwargs: Document())
    monkeypatch.setattr(parsers, "MAX_EXTRACTED_CHARS", 5)
    with pytest.raises(ValueError, match="2 million character limit"):
        parse_file(path)
