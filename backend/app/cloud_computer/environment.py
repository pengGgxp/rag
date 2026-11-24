"""Server-owned descriptions of verified default execution templates."""

VERIFIED_TEMPLATE = "i9li6mxfc13yumsdji0y"


def environment_description(settings):
    if not settings or not settings.enabled:
        return ""
    if settings.config.get("template") != VERIFIED_TEMPLATE:
        return "当前默认云电脑模板的预装软件未核验；执行时按需检查命令和依赖，不假定软件已安装。"
    return (
        "当前默认云电脑模板已验证：Python 3.13、Node.js、bash、Pillow、python-docx、"
        "pypdf、LibreOffice Writer、Poppler 和中文字体；可处理图片、编辑 Word、导出 PDF、"
        "运行网页及通过 pip 安装依赖。另预装 openpyxl、reportlab，可按需检查后使用。"
        "这是新建环境的能力说明；已有环境可能仍使用旧模板，遇到缺失命令或版本差异时先检查实际环境。"
        "不承诺特定 Python 小版本、任意网络地址或额外软件始终可用。"
    )
