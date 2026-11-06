from rusa_knowledge.chunking import chunk_text, normalize_text


def test_normalize_text_removes_nulls_and_excess_whitespace():
    assert normalize_text("  alpha\r\n\r\n\r\nbeta\x00 ") == "alpha\n\nbeta"


def test_chunk_text_returns_single_short_chunk():
    assert chunk_text("A short document.") == ["A short document."]


def test_chunk_text_bounds_chunks_and_overlaps():
    text = "\n\n".join(f"Paragraph {index}. " + ("x" * 170) for index in range(8))
    chunks = chunk_text(text, chunk_size=300, overlap=40)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 300 for chunk in chunks)
    assert all(chunk.strip() for chunk in chunks)


def test_chunk_text_rejects_invalid_limits():
    try:
        chunk_text("text", chunk_size=100, overlap=10)
    except ValueError as exc:
        assert "chunk_size" in str(exc)
    else:
        raise AssertionError("invalid chunk size was accepted")
