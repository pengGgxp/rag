import pytest

from app.chunking import chunk_text
from app.parsed_document import ParsedDocument, parsed_chunks


def test_chunking_is_deterministic_and_preserves_offsets():
    text = "# Title\n\nFirst paragraph.\n\nSecond paragraph with enough content to be searchable."

    first = chunk_text(text, max_tokens=12, overlap_tokens=0)
    second = chunk_text(text, max_tokens=12, overlap_tokens=0)

    assert first == second
    assert first[0].ordinal == 0
    assert first[0].char_start == 0
    assert first[-1].char_end <= len(text)
    assert first[0].content_hash


@pytest.mark.parametrize("ending", ["\n", "\r\n", " \t\n", "\n\n"])
def test_final_paragraph_with_trailing_whitespace_is_indexed(ending):
    body = "Support window is Monday to Friday.\nThe support contact is Willow-8301."
    text = "# Support\n\nIndependent acceptance fixture.\n\n" + body + ending

    chunks = parsed_chunks(ParsedDocument(text, "text"))

    assert chunks
    assert body in "\n".join(draft.text for draft, _ in chunks)
    assert chunks[-1][0].char_end == len(text.rstrip())
    for draft, _ in chunks:
        assert text[draft.char_start:draft.char_end] == draft.text
