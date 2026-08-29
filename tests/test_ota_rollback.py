"""No-hardware tests for the managed-OTA rollback state machine (ota_updater).

Mocks pip + systemctl + the HTTP event report. Validates the decision logic that
keeps an unattended field device from bricking on a bad update — the whole point
of the rollback safety net.
"""
import time
import types

import pytest

from agal_one_agent import ota_updater


@pytest.fixture
def ota(tmp_path, monkeypatch):
    """ota_updater wired to a temp state dir, with pip/systemctl/report mocked.

    Exposes recorded side effects (pip_calls, restarts, emits) + a knob
    (pip_fail_tags) to make a given pip install fail.
    """
    monkeypatch.setattr(ota_updater, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ota_updater, "STATE_FILE", str(tmp_path / "ota_state.json"))
    monkeypatch.setattr(ota_updater, "installed_version", lambda: "v0.1.5")

    h = types.SimpleNamespace(pip_calls=[], restarts=0, emits=[], pip_fail_tags=set())

    def fake_pip(tag):
        h.pip_calls.append(tag)
        rc = 1 if tag in h.pip_fail_tags else 0
        return types.SimpleNamespace(returncode=rc, stdout="ok" if rc == 0 else "",
                                     stderr="" if rc == 0 else "boom")

    def fake_restart():
        h.restarts += 1

    def fake_emit(phase, state, *, error=None):
        h.emits.append((phase, error))

    monkeypatch.setattr(ota_updater, "_pip_install", fake_pip)
    monkeypatch.setattr(ota_updater, "_restart_service", fake_restart)
    monkeypatch.setattr(ota_updater, "_emit", fake_emit)
    return h


def _phases(h):
    return [p for p, _ in h.emits]


# ---- perform_update --------------------------------------------------------

def test_perform_update_installs_and_enters_probation(ota):
    ota_updater.perform_update("0.1.6", command_id="cmd1", rollout_id="r1", node_uid="node-x")
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify"
    assert st["target"] == "v0.1.6"
    assert st["previous"] == "v0.1.5"
    assert st["deadlineEpoch"] > int(time.time())
    assert (st["commandId"], st["rolloutId"], st["nodeUid"]) == ("cmd1", "r1", "node-x")
    assert ota.pip_calls == ["v0.1.6"]
    assert ota.restarts == 1
    assert "started" in _phases(ota)


def test_perform_update_noop_when_same_version(ota):
    ota_updater.perform_update("0.1.5", node_uid="node-x")
    st = ota_updater.read_state()
    assert st["phase"] == "idle"
    assert ota.restarts == 0
    assert ota.pip_calls == []
    assert "succeeded" in _phases(ota)


def test_perform_update_pip_failure_restores_previous(ota):
    ota.pip_fail_tags = {"v0.1.6"}
    ota_updater.perform_update("0.1.6", node_uid="node-x")
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert "boom" in st.get("error", "")
    # tried target (failed), then reinstalled the known-good previous
    assert ota.pip_calls == ["v0.1.6", "v0.1.5"]
    assert ota.restarts == 0          # never restart into a half-broken state mid-run
    assert "failed" in _phases(ota)


# ---- confirm_update --------------------------------------------------------

def test_confirm_update_settles_probation(ota):
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.1.5", "target": "v0.1.6",
        "deadlineEpoch": int(time.time()) + 100, "nodeUid": "node-x",
    })
    assert ota_updater.confirm_update(node_uid="node-x") is True
    st = ota_updater.read_state()
    assert st["phase"] == "idle"
    assert st["previous"] == "v0.1.6"   # the target is now the known-good baseline
    assert "succeeded" in _phases(ota)


def test_confirm_update_noop_when_not_probation(ota):
    ota_updater.write_state({"phase": "idle", "nodeUid": "node-x"})
    assert ota_updater.confirm_update() is False
    assert ota.emits == []


# ---- run_verify (the safety net) -------------------------------------------

def test_run_verify_rolls_back_past_deadline(ota):
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.1.5", "target": "v0.1.6",
        "deadlineEpoch": int(time.time()) - 1, "nodeUid": "node-x",
    })
    out = ota_updater.run_verify()
    st = ota_updater.read_state()
    assert st["phase"] == "rolled_back"
    assert ota.pip_calls == ["v0.1.5"]   # reinstall the previous known-good
    assert ota.restarts == 1
    assert "rolled_back" in _phases(ota)
    assert "rolled back" in out


def test_run_verify_noop_in_probation(ota):
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.1.5", "target": "v0.1.6",
        "deadlineEpoch": int(time.time()) + 100, "nodeUid": "node-x",
    })
    out = ota_updater.run_verify()
    assert ota_updater.read_state()["phase"] == "pending_verify"  # unchanged
    assert ota.pip_calls == [] and ota.restarts == 0
    assert "probation" in out


def test_run_verify_noop_when_idle(ota):
    ota_updater.write_state({"phase": "idle", "nodeUid": "node-x"})
    out = ota_updater.run_verify()
    assert ota.pip_calls == [] and ota.restarts == 0
    assert "noop" in out


def test_run_verify_rollback_pip_failure_marks_failed(ota):
    ota.pip_fail_tags = {"v0.1.5"}
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.1.5", "target": "v0.1.6",
        "deadlineEpoch": int(time.time()) - 1, "nodeUid": "node-x",
    })
    ota_updater.run_verify()
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert "boom" in st.get("error", "")
    assert ota.restarts == 1          # still restart to leave the device in *a* known state
    assert "failed" in _phases(ota)
