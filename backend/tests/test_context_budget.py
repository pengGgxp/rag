from types import SimpleNamespace

import pytest

from app.agent.conversation_context import input_budget


@pytest.mark.parametrize('capacity,output,expected', [
    (100000, 10000, 90000),
    (64000, 4096, 59904),
    (1000, 1000, 0),
    (1000, 2000, 0),
    (100000, 1000, 95000),
    (None, 1000, None),
    (1000, None, None),
])
def test_context_budget_reserves_output_and_codex_effective_capacity(capacity, output, expected):
    config = SimpleNamespace(context_capacity=capacity, max_output_tokens=output)
    assert input_budget(config) == expected
