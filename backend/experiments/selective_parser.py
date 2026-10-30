"""Local MarkItDown conversion with explicit, injected OCR and source positions."""

import copy
import hashlib
import html
import io
import re
import time
import warnings
import zipfile
from dataclasses import dataclass, field
from typing import Callable

from defusedxml import ElementTree as SafeET
from lxml import etree
from markitdown import StreamInfo
from markitdown.converters import DocxConverter, PdfConverter
from PIL import Image
import pdfplumber

from app.docmind import pdf_part
from app.parsed_document import ParsedDocument, assemble_pages
from app.parsers import pdf_pages

VERSION = "selective-v1"
MAX_BYTES = 200 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_PIXELS = 24_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


@dataclass(frozen=True)
class Policy:
    version: str = "calibration-v1"
    min_chars: int = 24
    max_bad_ratio: float = .02
    image_coverage: float = .18
    text_layer_overlap: float = .60


@dataclass
class Result:
    parsed: ParsedDocument
    decisions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True


def image_pdf(data: bytes) -> bytes:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("Image exceeds pixel limit")
            output = io.BytesIO()
            image.convert("RGB").save(output, "PDF", resolution=150)
            return output.getvalue()


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _intersection(a, b):
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def page_decision(page, text: str, policy: Policy) -> dict:
    chars = [c for c in text if not c.isspace()]
    bad = sum(c == "\ufffd" or ord(c) < 32 for c in chars)
    words = page.extract_words()
    images = [(i["x0"], i["top"], i["x1"], i["bottom"]) for i in page.images]
    coverage = min(1., sum(_area(box) for box in images) / max(1, page.width * page.height))
    boxes = [(w["x0"], w["top"], w["x1"], w["bottom"]) for w in words]
    word_area = sum(_area(box) for box in boxes)
    overlap = min(1., sum(max((_intersection(box, image) for image in images), default=0)
                         for box in boxes) / max(1, word_area))
    if not chars and not images:
        reason = "blank"
    elif len(chars) < policy.min_chars:
        reason = "insufficient_text"
    elif bad / max(1, len(chars)) > policy.max_bad_ratio:
        reason = "unreadable_text"
    elif coverage >= policy.image_coverage and overlap < policy.text_layer_overlap:
        reason = "image_without_reliable_text_layer"
    else:
        reason = "readable_text"
    return {"route": "local" if reason in ("blank", "readable_text") else "ocr",
            "reason": reason, "characters": len(chars), "bad_ratio": bad / max(1, len(chars)),
            "image_coverage": coverage, "text_layer_overlap": overlap}


def parse_pdf(data: bytes, ocr: Callable, policy=Policy()) -> Result:
    if len(data) > MAX_BYTES:
        raise ValueError("Document exceeds size limit")
    pages = pdf_pages(data)
    result = Result(ParsedDocument("", VERSION))
    assembled = []
    started = time.monotonic()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for index, page in enumerate(pdf.pages):
            if time.monotonic() - started > 180:
                raise TimeoutError("PDF local processing time exceeded")
            part = pdf_part(pages, index, index + 1)
            text = PdfConverter().convert(io.BytesIO(part), StreamInfo(extension=".pdf")).markdown
            decision = {"page_number": index + 1, **page_decision(page, text, policy)}
            result.decisions.append(decision)
            if decision["route"] == "ocr":
                try:
                    parsed = ocr(part, 1, f"pdf-page-{index + 1}")
                    blocks = [copy.deepcopy(block) | {"text": parsed.text[block["char_start"]:block["char_end"]]}
                              for p in parsed.pages for block in p["blocks"]]
                    if not blocks and parsed.text.strip():
                        blocks = [{"text": parsed.text, "block_type": "text"}]
                    if not blocks:
                        raise ValueError("OCR returned no readable text")
                except Exception as exc:
                    result.complete = False
                    decision["error_type"] = type(exc).__name__
                    result.warnings.append(f"Page {index + 1}: OCR incomplete ({type(exc).__name__})")
                    blocks = [{"text": text, "block_type": "partial_local"}]
            else:
                blocks = [{"text": text, "block_type": "text"}]
            assembled.append((index + 1, blocks))
            page.close()
    result.parsed = assemble_pages(assembled, VERSION)
    return result


def _marked_docx(data: bytes):
    if len(data) > MAX_BYTES:
        raise ValueError("Document exceeds size limit")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ValueError("DOCX expanded size exceeds limit")
        if len({i.filename for i in infos}) != len(infos):
            raise ValueError("DOCX duplicate entries")
        xml = archive.read("word/document.xml")
        SafeET.fromstring(xml)
        root = etree.fromstring(xml, etree.XMLParser(resolve_entities=False, no_network=True))
        body = root.find("w:body", NS)
        locations = []
        section = None
        prefix = "ZK" + hashlib.sha256(data).hexdigest()[:20]
        for index, block in enumerate(list(body)):
            kind = etree.QName(block).localname
            if kind not in ("p", "tbl"):
                continue
            value = "".join(block.itertext())
            style = block.find("w:pPr/w:pStyle", NS)
            if style is not None and "heading" in (style.get(f"{{{W}}}val") or "").lower():
                section = "".join(block.xpath(".//w:t/text()", namespaces=NS))
            marker = f"{prefix}BLOCK{len(locations)}END"
            p = etree.Element(f"{{{W}}}p")
            etree.SubElement(etree.SubElement(p, f"{{{W}}}r"), f"{{{W}}}t").text = marker
            body.insert(body.index(block), p)
            locations.append({"marker": marker, "block_type": "table" if kind == "tbl" else "paragraph",
                              "document_block": index, "section": section,
                              "table_rows": len(block.findall("w:tr", NS)) if kind == "tbl" else None})
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for info in infos:
                target.writestr(info.filename, etree.tostring(root) if info.filename == "word/document.xml"
                                else archive.read(info))
    return output.getvalue(), locations


def parse_docx(data: bytes, ocr: Callable) -> Result:
    marked, locations = _marked_docx(data)
    result = Result(ParsedDocument("", VERSION))
    image_cache = {}

    class Converter(DocxConverter):
        def _image_to_html(self, image_stream, stream_info, **kwargs):
            raw = image_stream.read(MAX_BYTES + 1)
            digest = hashlib.sha256(raw).hexdigest()
            ordinal = len(result.decisions)
            decision = {"image_hash": digest, "occurrence": ordinal, "route": "ocr", "reused": digest in image_cache}
            result.decisions.append(decision)
            if digest not in image_cache:
                try:
                    parsed = ocr(image_pdf(raw), 1, "docx-image-" + digest)
                    if not parsed.text.strip():
                        raise ValueError("OCR returned no readable text")
                    image_cache[digest] = parsed.text
                except Exception as exc:
                    result.complete = False
                    result.warnings.append(f"Image {ordinal + 1}: OCR incomplete ({type(exc).__name__})")
                    image_cache[digest] = ""
            text = html.escape(image_cache[digest])
            return f"<p>ZKIMAGE{ordinal}START</p><p>{text}</p><p>ZKIMAGE{ordinal}STOP</p>"

    markdown = Converter().convert(io.BytesIO(marked), StreamInfo(extension=".docx")).markdown
    parts, source_locations, offset = [], [], 0
    for index, location in enumerate(locations):
        start = markdown.find(location["marker"])
        if start < 0:
            raise ValueError("Conversion lost source boundary")
        end = markdown.find(locations[index + 1]["marker"]) if index + 1 < len(locations) else len(markdown)
        value = markdown[start + len(location["marker"]):end].strip()
        images = []
        def replace_image(match):
            images.append(int(match[1]))
            return match[2].strip()
        value = re.sub(r"ZKIMAGE(\d+)START\s*(.*?)\s*ZKIMAGE\1STOP", replace_image, value, flags=re.S).strip()
        if not value:
            continue
        if parts:
            offset += 2
        parts.append(value)
        source = {key: val for key, val in location.items() if key != "marker"}
        source.update(char_start=offset, char_end=offset + len(value))
        source_locations.append(source)
        for ordinal in images:
            source_locations.append({**source, "block_type": "image", "image_hash": result.decisions[ordinal]["image_hash"],
                                     "image_occurrence": ordinal})
        offset += len(value)
    result.parsed = ParsedDocument("\n\n".join(parts), VERSION, source_locations=source_locations)
    return result
