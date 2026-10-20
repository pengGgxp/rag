import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    text: str
    token_count: int
    char_start: int
    char_end: int
    heading_path: str | None
    content_hash: str


def _token_estimate(text: str) -> int:
    return max(1, round(len(text) / 4))


def chunk_text(text: str, max_tokens: int = 600, overlap_tokens: int = 80) -> list[ChunkDraft]:
    paragraphs = [match for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.S)]
    if not paragraphs and text.strip():
        paragraphs = [re.match(r"[\s\S]+", text)]
    drafts: list[ChunkDraft] = []
    current: list[tuple[str, int, int, str | None]] = []
    current_tokens = 0
    heading_path: str | None = None

    def flush() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        chunk_text_value = "\n\n".join(item[0] for item in current).strip()
        start = current[0][1]
        end = current[-1][2]
        drafts.append(
            ChunkDraft(
                ordinal=len(drafts),
                text=chunk_text_value,
                token_count=_token_estimate(chunk_text_value),
                char_start=start,
                char_end=end,
                heading_path=current[-1][3],
                content_hash=hashlib.sha256(chunk_text_value.encode("utf-8")).hexdigest(),
            )
        )
        if overlap_tokens and chunk_text_value:
            overlap_chars = overlap_tokens * 4
            overlap_start = max(start, end - overlap_chars)
            overlap_text = text[overlap_start:end].strip()
            current = [(overlap_text, overlap_start, end, heading_path)] if overlap_text else []
            current_tokens = _token_estimate(overlap_text) if overlap_text else 0
        else:
            current = []
            current_tokens = 0

    for match in paragraphs:
        if match is None:
            continue
        paragraph = match.group(0).strip()
        if not paragraph:
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", paragraph)
        if heading_match:
            heading_path = heading_match.group(2).strip()
        paragraph_tokens = _token_estimate(paragraph)
        if current and current_tokens + paragraph_tokens > max_tokens:
            flush()
        current.append((paragraph, match.start(), match.end(), heading_path))
        current_tokens += paragraph_tokens
    flush()
    return drafts
