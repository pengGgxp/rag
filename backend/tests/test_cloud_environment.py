from types import SimpleNamespace

import pytest

from app.cloud_environment import VERIFIED_TEMPLATE, environment_description
from app.conversation_context import build_messages


@pytest.mark.parametrize("settings", [None, SimpleNamespace(enabled=False)])
def test_disabled_environment_does_not_advertise_packages(settings):
    assert environment_description(settings) == ""


def test_unknown_template_does_not_inherit_verified_capabilities():
    result = environment_description(SimpleNamespace(enabled=True, config={"template": "another"}))
    assert "未核验" in result
    assert "LibreOffice" not in result


def test_rebuilt_context_retains_server_owned_environment():
    description = environment_description(SimpleNamespace(enabled=True, config={"template": VERIFIED_TEMPLATE}))
    extensions = SimpleNamespace(skills=[], tools=[], environment_description=description)
    messages, _ = build_messages(None, SimpleNamespace(libraries=[]), [], None, extensions, query="制作简历")
    assert description in messages[0]["content"]
    assert "python-docx" in messages[0]["content"]
    assert "已有环境可能仍使用旧模板" in messages[0]["content"]
    assert "工具执行结束后必须用自然语言给出简短交付总结" in messages[0]["content"]
    assert "[打开资源库](/resources)" in messages[0]["content"]
