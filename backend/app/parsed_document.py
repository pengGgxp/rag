"""Normalized parser output shared by local PDF parsing and DocMind."""

from dataclasses import dataclass, field
from typing import Any

from .chunking import ChunkDraft, chunk_text


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    parser_name: str
    pages: list[dict[str, Any]] = field(default_factory=list)
    source_locations: list[dict[str, Any]] = field(default_factory=list)


def assemble_pages(pages: list[tuple[int, list[dict]]], parser_name: str) -> ParsedDocument:
    parts: list[str] = []
    locations: list[dict] = []
    offset = 0
    for page_number, blocks in pages:
        page_start = offset
        page_blocks = []
        for block in blocks:
            value = str(block.get("text") or "").strip()
            if not value:
                continue
            if parts:
                parts.append("\n\n")
                offset += 2
            start = offset
            parts.append(value)
            offset += len(value)
            page_blocks.append({
                "page_number": page_number,
                "char_start": start,
                "char_end": offset,
                "block_type": block.get("block_type", "text"),
                "bbox": block.get("bbox"),
            })
        if page_blocks:
            page_start = page_blocks[0]["char_start"]
        locations.append({
            "page_number": page_number,
            "char_start": page_start,
            "char_end": offset,
            "blocks": page_blocks,
        })
    return ParsedDocument("".join(parts), parser_name, locations)


def parsed_chunks(parsed: ParsedDocument) -> list[tuple[ChunkDraft, list[dict]]]:
    if not parsed.pages:
        return [(draft, [location for location in parsed.source_locations
                         if location["char_start"] < draft.char_end and location["char_end"] > draft.char_start])
                for draft in chunk_text(parsed.text)]
    result = []
    for page in parsed.pages:
        base = page["char_start"]
        # Never carry overlap from one page into another: citations stay page-exact.
        for draft in chunk_text(parsed.text[base:page["char_end"]]):
            start, end = base + draft.char_start, base + draft.char_end
            result.append((
                ChunkDraft(
                    ordinal=len(result), text=draft.text, token_count=draft.token_count,
                    char_start=start, char_end=end, heading_path=draft.heading_path,
                    content_hash=draft.content_hash,
                ),
                [block for block in page["blocks"]
                 if block["char_start"] < end and block["char_end"] > start],
            ))
    return result
