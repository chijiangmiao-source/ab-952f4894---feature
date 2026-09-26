"""Pytest fixtures: fresh engine on temporary databases."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.device import DeviceBank  # noqa: E402
from app.models import SwitchRequest, ValveSpec  # noqa: E402
from app.saga import SagaEngine  # noqa: E402
from app.store import SwitchStore  # noqa: E402


@pytest.fixture()
def engine(tmp_path):
    store = SwitchStore(str(tmp_path / "switches.db"))
    devices = DeviceBank(str(tmp_path / "devices.db"))
    return SagaEngine(store, devices), store, devices, tmp_path


def make_request(op: str = "op-1", n: int = 4,
                 initial: int = 0, target: int = 60) -> SwitchRequest:
    return SwitchRequest(
        operation_id=op,
        valves=[
            ValveSpec(
                valve_id=f"V{i:02d}",
                initial_opening=initial + i,
                target_opening=target + i,
            )
            for i in range(1, n + 1)
        ],
    )
