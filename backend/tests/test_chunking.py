from app.chunking import chunk_text


def test_chunking_is_deterministic_and_preserves_offsets():
    text = "# Title\n\nFirst paragraph.\n\nSecond paragraph with enough content to be searchable."

    first = chunk_text(text, max_tokens=12, overlap_tokens=0)
    second = chunk_text(text, max_tokens=12, overlap_tokens=0)

    assert first == second
    assert first[0].ordinal == 0
    assert first[0].char_start == 0
    assert first[-1].char_end <= len(text)
    assert first[0].content_hash
