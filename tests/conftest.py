"""Shared fixtures. The dsh client is injected, never patched into a module function."""
from __future__ import annotations

import pytest
from fakes import FakeDsh

from dsh_workers_mcp import server, state as st


@pytest.fixture
def dsh(monkeypatch) -> FakeDsh:
    """An in-memory dsh, installed as the client that the server and its commands use."""
    fake = FakeDsh()
    monkeypatch.setattr(server, "CLIENT", fake)
    return fake


@pytest.fixture
def state(tmp_path, monkeypatch, dsh) -> st.State:
    s = st.State(tmp_path / "orch", client=dsh)
    monkeypatch.setattr(server, "STATE", s)
    return s
