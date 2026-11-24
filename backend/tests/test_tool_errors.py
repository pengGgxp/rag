import pytest

from app.agent.tool_errors import tool_outcome


@pytest.mark.parametrize("result,expected", [
    ({}, "success"),
    ({"error": "invalid", "error_code": "file_arguments"}, "feedback"),
    ({"error": "invalid", "error_code": "image_arguments"}, "feedback"),
    ({"error": "invalid", "error_code": "plan_arguments"}, "feedback"),
    ({"error": "invalid", "status_code": 422, "error_code": "preview_unavailable"}, "feedback"),
    ({"error": "balance", "status_code": 402}, "blocked"),
    ({"error": "forbidden", "status_code": 403, "error_code": "file_arguments"}, "blocked"),
    ({"error": "capacity", "error_code": "file_capacity"}, "blocked"),
    ({"error": "unavailable", "error_code": "knowledge_access"}, "blocked"),
    ({"error": "invalid arguments without a stable code"}, "failed"),
    ({"isError": True}, "failed"),
    ({"error": "failed", "error_code": "script_failed"}, "failed"),
    ({"error": "unknown", "uncertain": True, "error_code": "file_arguments"}, "uncertain"),
    ({"uncertain": True, "status_code": 402}, "uncertain"),
])
def test_tool_observations_have_independent_presentation_outcomes(result, expected):
    original = dict(result)
    assert tool_outcome(result) == expected
    assert result == original
