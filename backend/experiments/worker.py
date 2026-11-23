"""Offline conversion process. Parent bounds time; only parent can make paid calls."""

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import socket

from app.knowledge.ingestion.parsed_document import ParsedDocument
from app.knowledge.ingestion.parsers import parse_document, pdf_pages
from .selective_parser import parse_docx, parse_pdf, Policy, Result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--arm", choices=["baseline", "candidate"], required=True)
    parser.add_argument("--policy", required=True)
    args = parser.parse_args()
    source, output = Path(args.input), Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    requests = []
    calls = []
    def offline(*_args, **_kwargs):
        raise RuntimeError("Network disabled in conversion worker")
    socket.socket.connect = offline
    socket.create_connection = offline
    def ocr(data, pages, purpose):
        digest = hashlib.sha256(data).hexdigest()
        calls.append({"hash": digest, "pages": pages, "purpose": purpose})
        cached = output.parent / "ocr-results" / f"{digest}.json"
        if cached.exists():
            return ParsedDocument(**json.loads(cached.read_text(encoding="utf-8")))
        request = output.parent / "ocr-inputs" / f"{digest}.pdf"
        request.parent.mkdir(exist_ok=True)
        request.write_bytes(data)
        requests.append({"hash": digest, "pages": pages, "purpose": purpose})
        raise RuntimeError("OCR result pending")
    try:
        data = source.read_bytes()
        if args.arm == "baseline":
            if source.suffix.lower() == ".pdf":
                result = Result(ocr(data, len(pdf_pages(data)), "baseline-pdf"))
                result.decisions = [{"route": "ocr", "page_number": i + 1} for i in range(len(pdf_pages(data)))]
            else:
                text, name = parse_document(source.name, "", data)
                result = Result(ParsedDocument(text, name))
        else:
            result = (parse_docx(data, ocr) if source.suffix.lower() == ".docx" else
                      parse_pdf(data, ocr, Policy(**json.loads(Path(args.policy).read_text(encoding="utf-8")))))
        payload = asdict(result)
    except Exception as exc:
        payload = asdict(Result(ParsedDocument("", "failed"), complete=False, warnings=[type(exc).__name__]))
    payload["ocr_requests"] = requests
    payload["ocr_calls"] = calls
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
