import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from experiments.checkpoints import Checkpoints, BudgetError
import pytest


def test_budget_reservation_is_atomic_and_not_reset_on_reopen(tmp_path):
    path = tmp_path / "costs.sqlite"
    checkpoint = Checkpoints(path)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: checkpoint.reserve("same", "ocr", "5"), range(4)))
    assert sum(results) == 1
    Checkpoints(path).reserve("second", "ocr", "15")
    with pytest.raises(BudgetError):
        checkpoint.reserve("third", "ocr", ".0001")
    assert checkpoint.summary()["reserved_upper_yuan"] == 20
    assert checkpoint.get("same")["state"] == "started"


def test_result_replay_and_uncertainty_keep_reservation(tmp_path):
    checkpoint = Checkpoints(tmp_path / "costs.sqlite")
    checkpoint.reserve("a", "ocr", ".25")
    checkpoint.update("a", state="uncertain")
    assert not checkpoint.reserve("a", "ocr", ".25")
    checkpoint.reserve("b", "ocr", ".25")
    checkpoint.finish("b", {"text": "confirmed"}, 1)
    assert json.loads(checkpoint.get("b")["result"]) == {"text": "confirmed"}
    assert checkpoint.summary()["reserved_upper_yuan"] == .5
    with pytest.raises(ValueError):
        Checkpoints(checkpoint.path, "21")
    with pytest.raises(BudgetError):
        Checkpoints(checkpoint.path, "10")
