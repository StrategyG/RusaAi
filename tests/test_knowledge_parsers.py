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
