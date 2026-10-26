"""Alibaba Cloud DocMind adapter. Credentials and signed URLs never leave this module."""

import io
import hashlib
import json
import math
from time import monotonic, sleep
from typing import Callable

from alibabacloud_docmind_api20220711 import models
from alibabacloud_docmind_api20220711.client import Client
from alibabacloud_tea_openapi.models import Config
from alibabacloud_tea_util.models import RuntimeOptions

from .config import Settings, get_settings
from .parsed_document import ParsedDocument, assemble_pages
from pypdf import PdfWriter

PARSER_NAME = "aliyun-docmind-v1"
# Conservative submission budgets; these are not document acceptance limits.
MAX_SUBMISSION_BYTES = 100 * 1024 * 1024
MAX_SUBMISSION_PAGES = 500


def pdf_part(pages, start: int, end: int) -> bytes:
    writer = PdfWriter()
    try:
        for index in range(start, end):
            writer.add_page(pages[index])
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    finally:
        writer.close()


def submission_ranges(content: bytes, pages) -> list[tuple[int, int]]:
    """Preflight every batch before making any paid submission."""
    def split(start, end):
        part = pdf_part(pages, start, end)
        fits = len(part) <= MAX_SUBMISSION_BYTES
        del part
        if fits or end - start == 1:
            return [(start, end)]
        middle = (start + end) // 2
        return split(start, middle) + split(middle, end)

    if len(content) <= MAX_SUBMISSION_BYTES and len(pages) <= MAX_SUBMISSION_PAGES:
        return [(0, len(pages))]
    return [part for start in range(0, len(pages), MAX_SUBMISSION_PAGES)
            for part in split(start, min(start + MAX_SUBMISSION_PAGES, len(pages)))]


class DocMindError(ValueError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


class ParsingCancelled(Exception):
    pass


def _safe_error(code: str) -> DocMindError:
    # Provider messages can contain credentials, file URLs or document content.
    lowered = code.casefold()
    if any(word in lowered for word in ("access", "forbidden", "permission", "unauthorized")):
        return DocMindError("阿里云文档解析鉴权失败，请检查 AccessKey、RAM 权限及服务开通状态")
    if any(word in lowered for word in ("balance", "arrears", "payment", "quota")):
        return DocMindError("阿里云文档解析额度或账户状态异常，请检查云端控制台")
    if any(word in lowered for word in ("throttl", "limit")):
        return DocMindError("阿里云文档解析请求受限，请稍后重试")
    return DocMindError("阿里云文档解析请求失败，请检查云端服务状态后重试")


class AliyunDocMind:
    def __init__(self, settings: Settings | None = None, client=None):
        self.settings = settings or get_settings()
        if not self.settings.docmind_configured:
            raise DocMindError("未配置阿里云文档解析密钥")
        self.client = client or Client(Config(
            access_key_id=self.settings.aliyun_access_key_id.get_secret_value(),
            access_key_secret=self.settings.aliyun_access_key_secret.get_secret_value(),
            endpoint="docmind-api.cn-hangzhou.aliyuncs.com",
            region_id="cn-hangzhou",
            protocol="https",
        ))
        # A submission is not idempotent at the provider. Never auto-retry it.
        self.runtime = RuntimeOptions(
            connect_timeout=10000, read_timeout=30000, autoretry=False,
        )

    def _request(self, method, request):
        try:
            body = method(request, self.runtime).body
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            error = _safe_error(code)
            error.uncertain = not code or any(word in code.casefold() for word in ("timeout", "internal", "network"))
            raise error from None
        if str(getattr(body, "code", "") or "").casefold() not in ("", "200", "success", "ok"):
            raise _safe_error(str(body.code)) from None
        return body.data

    def submit(self, content: bytes, page_count: int) -> str:
        if page_count < 1:
            raise DocMindError("PDF 没有可解析页面")
        data = self._request(self.client.submit_doc_parser_job_advance,
                             models.SubmitDocParserJobAdvanceRequest(
                                 file_name="document.pdf", file_name_extension="pdf",
                                 file_url_object=io.BytesIO(content),
                                 page_index=f"1-{page_count}",
                                 output_format=["markdown", "visualLayoutInfo"],
                                 llm_enhancement=False, formula_enhancement=False,
                             ))
        if data is None or not data.id:
            raise DocMindError("阿里云未返回解析任务标识，未自动重复提交", uncertain=True)
        return data.id

    def collect(self, task_id: str, page_count: int, check_active: Callable[[], None]) -> ParsedDocument:
        deadline = monotonic() + self.settings.docmind_timeout_seconds

        def check():
            check_active()
            if monotonic() >= deadline:
                raise DocMindError("阿里云文档解析等待超时，重试将继续查询原任务")

        while True:
            check()
            data = self._request(self.client.query_doc_parser_status_with_options,
                                 models.QueryDocParserStatusRequest(id=task_id))
            status = str(getattr(data, "status", "")).casefold()
            if status == "success":
                break
            if status in ("fail", "failed"):
                raise DocMindError("阿里云解析任务失败；保留任务标识，未自动重复提交")
            sleep(self.settings.docmind_poll_seconds)
        layouts = []
        offset, step = 0, 100
        seen_batches = set()
        while True:
            check()
            data = self._request(self.client.get_doc_parser_result_with_options,
                                 models.GetDocParserResultRequest(
                                     id=task_id, layout_num=offset, layout_step_size=step,
                                 ))
            batch = data.get("layouts", []) if isinstance(data, dict) else None
            if not isinstance(batch, list):
                raise DocMindError("阿里云返回了无法识别的版面结构")
            if batch:
                fingerprint = hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).digest()
                if fingerprint in seen_batches:
                    raise DocMindError("阿里云重复返回同一批版面，请重试查询原任务")
                seen_batches.add(fingerprint)
            layouts.extend(batch)
            if len(batch) < step:
                break
            offset += len(batch)
        return normalize_layouts(layouts, page_count, allow_empty=True)


def normalize_layouts(layouts: list[dict], page_count: int, *, allow_empty: bool = False) -> ParsedDocument:
    pages: dict[int, list[dict]] = {page: [] for page in range(1, page_count + 1)}
    seen = set()
    for layout in sorted(layouts, key=lambda row: (row.get("pageNum", 0), row.get("index", 0))):
        page = layout.get("pageNum")
        if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < page_count:
            raise DocMindError("阿里云返回的 PDF 页码不合法，未建立引用")
        unique = layout.get("uniqueId")
        if unique and unique in seen:
            continue
        if unique:
            seen.add(unique)
        kind = str(layout.get("type") or "text")
        # Prefer Markdown for tables; image URLs alone are not searchable evidence.
        text = layout.get("markdownContent") or layout.get("text") or ""
        if kind in ("image", "figure"):
            text = layout.get("text") or ""
        bbox = None
        points = layout.get("pos")
        if isinstance(points, list) and points:
            try:
                xs = [float(point["x"]) for point in points]
                ys = [float(point["y"]) for point in points]
                if all(math.isfinite(value) and value >= 0 for value in xs + ys):
                    bbox = [min(xs), min(ys), max(xs), max(ys)]
            except (KeyError, TypeError, ValueError):
                pass
        pages[page + 1].append({"text": text, "block_type": kind, "bbox": bbox})
    parsed = assemble_pages(list(pages.items()), PARSER_NAME)
    if not parsed.text.strip() and not allow_empty:
        raise DocMindError("阿里云解析未返回可检索文字，请检查扫描清晰度或文档类型")
    return parsed
