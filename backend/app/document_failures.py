import logging
import re
from uuid import uuid4

from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


def failure(stage, code, message, retryable=False, request_id=None):
    return dict(code=code, stage=stage, message=message, retryable=retryable,
                request_id=request_id or str(uuid4()))


def uncertain_submission(document):
    metadata = document.parsing_metadata or {}
    if metadata.get("status") == "submitting" and not metadata.get("task_id"):
        return True
    return any(
        part.get("status") == "submitting" and not part.get("task_id")
        for part in metadata.get("parts", [])
        if isinstance(part, dict)
    )


def safe_job_failure(job, document=None):
    stage = job.phase or ("parsing" if not document or not document.parsed_text else "indexing")
    request_id = str(job.metering_id or job.id)
    state = getattr(job.state, "value", job.state)
    if state in ("failed", "cancelled") and (job.embedding_submitting or document and uncertain_submission(document)):
        return failure(
            stage, "needs_reconciliation", "付费处理请求结果需要核对，暂不能重复提交",
            request_id=request_id,
        )
    detail = job.failure
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return detail
    if not job.error_detail and not (document and document.error_detail):
        return None
    cancelled = state == "cancelled"
    return failure(
        stage,
        "processing_failed",
        "任务已取消" if cancelled else "文档处理失败，请查看任务状态或重试",
        retryable=state in ("failed", "cancelled"),
        request_id=request_id,
    )


def safe_job_message(job, document=None):
    detail = safe_job_failure(job, document)
    return detail["message"] if detail else None


def safe_document_message(document, detail=None):
    if detail:
        return detail["message"]
    if document.error_detail:
        return "文档处理失败，请查看文档任务状态"
    return None


def processing_failure(stage, error, request_id):
    if getattr(error, "uncertain", False):
        return failure(stage, "needs_reconciliation",
                       "付费处理请求结果需要核对，暂不能重复提交",
                       request_id=request_id)
    status = getattr(error, "status_code", None)
    if status is not None:
        return classify(stage, error, request_id)
    error_type = type(error).__name__
    if stage == "parsing":
        if error_type == "PdfParsingError":
            code = getattr(error, "code", "invalid_format")
            return failure(stage, code, str(error), request_id=request_id)
        if error_type == "DocMindError":
            message = str(error)
            return failure(stage, getattr(error, "code", "parsing_failed"), message,
                           retryable=bool(getattr(error, "retryable", False)) or "请稍后重试" in message,
                           request_id=request_id)
        if isinstance(error, ValueError):
            return failure(stage, "parsing_failed", "文档内容无法解析，请检查文件格式或完整性",
                           request_id=request_id)
        return failure(stage, "service_unavailable", "文档处理服务暂时不可用，请稍后重试",
                       retryable=True, request_id=request_id)
    if error_type == "EmbeddingConfigurationError":
        return failure(stage, "indexing_unavailable", "索引服务暂不可用，请联系管理员",
                       retryable=True, request_id=request_id)
    return failure(stage, "indexing_failed", "文档索引失败，请稍后重试",
                   retryable=True, request_id=request_id)


def classify(stage, error, request_id=None):
    status = error.status_code if isinstance(error, StarletteHTTPException) else 500
    if isinstance(error, StarletteHTTPException) and isinstance(error.detail, dict) and "code" in error.detail:
        return {**error.detail, "request_id": request_id or error.detail.get("request_id") or str(uuid4())}
    code, message = {
        401: ("sign_in_required", "请重新登录后继续"),
        402: ("quota_exceeded", "额度不足，请联系管理员"),
        403: ("permission_denied", "没有操作权限，请联系管理员"),
        404: ("not_found", "任务或文档已不存在，请刷新列表"),
        409: ("conflict", "任务状态已变化，请刷新后继续"),
        410: ("upload_expired", "上传已过期，请重新选择文件"),
        413: ("quota_exceeded", "文件大小超限或存储额度不足"),
        415: ("invalid_format", "不支持此文件格式，或文件内容与格式不符"),
        429: ("rate_limited", "请求过于频繁，请稍后重试"),
    }.get(status, ("invalid_request", "请求不符合要求，请检查后重试"))
    if status >= 500:
        code, message = "service_unavailable", "服务暂时不可用，请稍后重试"
    return failure(stage, code, message, status == 429 or status >= 500, request_id)


def rejection(status, stage, code, message):
    return StarletteHTTPException(status, failure(stage, code, message))


def route_stage(path, method):
    if re.fullmatch(r"/knowledge-bases/[^/]+/(uploads|documents)/?", path) and method == "POST":
        return "upload"
    if re.fullmatch(r"/documents/[^/]+/reindex/?", path):
        return "processing"
    if re.fullmatch(r"/uploads/[^/]+(?:/complete|/parts/[^/]+)?/?", path):
        return "verification" if path.rstrip("/").endswith("/complete") else "query" if method == "GET" else "upload"
    if re.fullmatch(r"/jobs/[^/]+(?:/retry|/cancel)?/?", path):
        return "processing"
    return None


def error_response(request, error):
    stage = request.state.document_stage
    status = error.status_code if isinstance(error, StarletteHTTPException) else 500
    headers = error.headers if isinstance(error, StarletteHTTPException) else None
    return JSONResponse({"detail": classify(stage, error, request.state.document_request_id)}, status, headers)


def log_worker_failure(*, stage, route, request_id, upload_id=None, job_id=None,
                       document_id=None, status=None, content_type=None,
                       code=None, retryable=False, exception_type=None):
    log.warning(
        "document_worker_failed request_id=%s stage=%s route=%s status=%s content_type=%s upload_id=%s job_id=%s document_id=%s code=%s retryable=%s exception_type=%s",
        request_id, stage, route, status, content_type, upload_id, job_id,
        document_id, code, retryable, exception_type,
    )
