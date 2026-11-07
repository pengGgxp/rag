"""Reviewed image parameter catalog; contains no commercial prices or credentials."""
from copy import deepcopy

REVISION = 1
VERIFIED_AT = "2026-11-07"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/guides/image-generation"
GEMINI_SOURCE = "https://ai.google.dev/gemini-api/docs/image-generation"
RATIOS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
RATIO_LABELS = {"1:1": "方形", "2:3": "竖图", "3:2": "横图", "3:4": "竖幅", "4:3": "横幅",
    "4:5": "竖版", "5:4": "横版", "9:16": "竖屏", "16:9": "宽屏", "21:9": "超宽屏",
    "1:4": "长竖图", "4:1": "长横图", "1:8": "超长竖图", "8:1": "超长横图"}
GPT_SIZES = [
    ("1024x1024", "方形", "1:1"), ("1536x1024", "横图", "3:2"),
    ("1024x1536", "竖图", "2:3"),
]
QUALITY_LABELS = {"auto": "自动", "low": "草稿", "medium": "标准", "high": "高质量",
                  "xhigh": "超高质量", "max": "最高质量", "native": "原生分辨率"}


def preset(identifier, name, sizes, qualities, *, gemini=False, references=8, omit_resolution=False):
    return {"id": identifier, "name": name, "revision": REVISION, "verified_at": VERIFIED_AT,
        "source": GEMINI_SOURCE if gemini else OPENAI_SOURCE,
        "protocols": ["gemini"] if gemini else ["openai_images", "responses"],
        "operations": ["generate", "edit"], "max_count": 1 if gemini else 4,
        "max_references": references, "size_label": "宽高比" if gemini else "尺寸",
        "quality_label": "分辨率" if gemini else "质量", "omit_resolution": omit_resolution,
        "sizes": [{"value": value, "label": label, "ratio": ratio} for value, label, ratio in sizes],
        "qualities": [{"value": q, "label": QUALITY_LABELS.get(q, q)} for q in qualities]}


CATALOG = [
    preset("gpt-image-1", "GPT Image 1 / 1.5 / Mini", GPT_SIZES, ["low", "medium", "high", "auto"]),
    preset("gpt-image-2", "GPT Image 2", GPT_SIZES + [
        ("1536x864", "宽屏", "16:9"), ("864x1536", "竖屏", "9:16"),
        ("2560x1440", "高清宽屏", "16:9"), ("1440x2560", "高清竖屏", "9:16")],
        ["low", "medium", "high", "auto"]),
    preset("gpt-image-2.5", "GPT Image 2.5 Sunburst / Flare", GPT_SIZES + [
        ("1536x864", "宽屏", "16:9"), ("864x1536", "竖屏", "9:16"),
        ("2560x1440", "高清宽屏", "16:9"), ("1440x2560", "高清竖屏", "9:16")],
        ["low", "medium", "high", "xhigh", "max", "auto"]),
    preset("gemini-2.5-flash-image", "Gemini 2.5 Flash Image", [(r, RATIO_LABELS[r], r) for r in RATIOS],
        ["native"], gemini=True, references=3, omit_resolution=True),
    preset("gemini-3-pro-image", "Gemini 3 Pro Image", [(r, RATIO_LABELS[r], r) for r in RATIOS],
        ["1K", "2K", "4K"], gemini=True),
    preset("gemini-3.1-flash-image", "Gemini 3.1 Flash Image", [(r, RATIO_LABELS[r], r) for r in RATIOS + ["1:4", "4:1", "1:8", "8:1"]],
        ["512", "1K", "2K", "4K"], gemini=True),
]


def catalog():
    return deepcopy(CATALOG)


def find(identifier):
    return next((p for p in CATALOG if p["id"] == identifier), None)


def validate(config):
    if config.preset_id is None:
        if config.preset_revision is not None:
            raise ValueError("自定义配置不能指定预设修订号")
        return
    p = find(config.preset_id)
    if not p or config.preset_revision != p["revision"]:
        raise ValueError("图片能力预设已更新，请重新选择预设")
    if config.protocol not in p["protocols"]:
        raise ValueError("图片协议与能力预设不匹配")
    if config.max_count > p["max_count"] or config.max_references > p["max_references"]:
        raise ValueError("图片张数或参考图数量超过预设限制")
    sizes, qualities = {s["value"] for s in p["sizes"]}, {q["value"] for q in p["qualities"]}
    for spec in config.specs:
        if spec.operation not in p["operations"] or spec.size not in sizes or spec.quality not in qualities:
            raise ValueError("图片规格不属于所选能力预设")


def resolution_options(snapshot, quality):
    """Gemini 2.5 has a fixed native resolution; never send a fake imageSize."""
    return {} if snapshot.get("omit_resolution") else {"imageSize": quality}
