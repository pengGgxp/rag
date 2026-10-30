"""Small reproducible documents with independently specified answers and positions."""

import io
import json
from pathlib import Path

from docx import Document
from docx.shared import Inches
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader


def font_path():
    for path in ("C:/Windows/Fonts/simhei.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(path).exists():
            return path
    raise RuntimeError("Set up a TrueType font before generating evaluation samples")


def screenshot(lines):
    image = Image.new("RGB", (1400, 700), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(font_path(), 40)
    for index, text in enumerate(lines):
        draw.text((55, 50 + index * 90), text, font=font, fill="black")
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def pdf_document(path, pages):
    pdfmetrics.registerFont(TTFont("Experiment", font_path()))
    c = canvas.Canvas(str(path), pagesize=(700, 700), invariant=1)
    for page in pages:
        if page.get("image"):
            c.drawImage(ImageReader(io.BytesIO(page["image"])), 20, 230, width=660, height=330)
        c.setFont("Experiment", 15)
        for index, text in enumerate(page.get("lines", [])):
            c.drawString(35, 640 - index * 35, text)
        if page.get("columns"):
            for x, lines in zip((35, 365), page["columns"]):
                for index, text in enumerate(lines):
                    c.drawString(x, 600 - index * 35, text)
        if page.get("text_layer"):
            text = c.beginText(46, 500)
            text.setFont("Experiment", 15)
            text.setTextRenderMode(3)
            for line in page["text_layer"]:
                text.textLine(line)
            c.drawText(text)
        c.showPage()
    c.save()


def generate(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, kind in enumerate(("word-text", "word-table", "word-image", "word-image-repeat",
                                 "word-nested-table", "word-order", "pdf-text", "pdf-scan", "pdf-mixed",
                                 "pdf-table", "pdf-text-layer", "pdf-columns"), start=1):
        code, amount = f"XK{index:03}", str(100 + index * 7)
        fact1, fact2 = f"项目编号：{code}", f"审批金额：{amount}元"
        heading = f"样本{index} 项目审批记录"
        image = screenshot([heading, fact1, fact2])
        suffix = ".docx" if kind.startswith("word") else ".pdf"
        path = root / f"{index:02}-{kind}{suffix}"
        location1, location2 = {}, {}
        if suffix == ".docx":
            doc = Document()
            doc.add_heading(heading, level=1)
            if kind in ("word-text", "word-order"):
                doc.add_paragraph(fact1)
                if kind == "word-order":
                    doc.add_heading("审批结果", level=2)
                doc.add_paragraph(fact2)
                location1 = location2 = {"block_type": "paragraph"}
            elif kind in ("word-table", "word-nested-table"):
                doc.add_paragraph(fact1)
                table = doc.add_table(rows=2, cols=2)
                table.cell(0, 0).text, table.cell(0, 1).text = "字段", "内容"
                table.cell(1, 0).text, table.cell(1, 1).text = "审批金额", fact2
                if kind == "word-nested-table":
                    nested = table.cell(1, 1).add_table(rows=1, cols=2)
                    nested.cell(0, 0).text, nested.cell(0, 1).text = "复核结果", "同意"
                location1, location2 = {"block_type": "paragraph"}, {"block_type": "table"}
            else:
                doc.add_paragraph("下方截图包含项目编号和审批金额。")
                doc.add_picture(io.BytesIO(image), width=Inches(5))
                if kind == "word-image-repeat":
                    doc.add_paragraph("复核截图与上图相同。")
                    doc.add_picture(io.BytesIO(image), width=Inches(5))
                location1 = location2 = {"block_type": "image"}
            doc.add_paragraph("记录结束，请仅根据本记录回答问题。")
            doc.save(path)
        else:
            lines = [heading, fact1, fact2, "负责人为李明，状态为已批准。请保留原始证据。"]
            if kind == "pdf-scan":
                pages = [{"image": image}]
            elif kind == "pdf-mixed":
                pages = [{"lines": [heading, fact1, "项目基本信息已核验，无其他变更。"]}, {"image": image}]
            elif kind == "pdf-columns":
                pages = [{"lines": [heading], "columns": [[fact1, "第一栏：基本信息"], [fact2, "第二栏：审批结果"]]}]
            elif kind == "pdf-table":
                pages = [{"lines": [heading, "字段                 内容", fact1, fact2, "流程：申请 -> 审批 -> 归档"]}]
            elif kind == "pdf-text-layer":
                pages = [{"image": image, "text_layer": lines}]
            else:
                pages = [{"lines": lines}]
            pdf_document(path, pages)
            location1 = {"page_number": 1}
            location2 = {"page_number": 2 if kind == "pdf-mixed" else 1}
        manifest.append({"id": path.stem, "path": path.name, "kind": kind, "origin": "generated",
                         "facts": [code, amount], "order": [code, amount], "expected_locations": [location1, location2],
                         "questions": [{"query": "项目编号是什么？", "answer": code},
                                       {"query": "审批金额是多少元？", "answer": amount}],
                         "original_excerpt": f"{heading}\n{fact1}\n{fact2}"})
    # Calibration is disjoint from scored samples and has explicit routing labels.
    calibration = []
    for name, page, expected in (
        ("readable", {"lines": ["Calibration sample: project C0123", "The approved amount is 560 yuan."]}, "local"),
        ("scan", {"image": screenshot(["CALIBRATION ONLY", "Project C000", "Amount 920 yuan"])}, "ocr"),
        ("layer", {"image": screenshot(["Calibration original"]),
                   "text_layer": ["Calibration sample: project C0123", "The approved amount is 560 yuan."]}, "local"),
    ):
        path = root / f"calibration-{name}.pdf"
        pdf_document(path, [page])
        calibration.append({"path": path.name, "expected": expected})
    (root / "manifest.json").write_text(json.dumps({"samples": manifest, "calibration": calibration}, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
