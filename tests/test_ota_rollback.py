"""No-hardware, no-network tests for the managed OTA (ota_updater): the release archive path,
the offline way back, the busy wait, the git fallback, and the rollback state machine.

pip, the download, systemctl, the clock and the HTTP event report are fakes. The venv is a
real directory tree in tmp_path — what pip leaves for agal-one-agent (package, dist-info with a
RECORD, console script) — so the snapshot and the restore run for real against it.
"""
import hashlib
import io
import os
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.parse

import pytest

from agal_one_agent import ota_updater
from agal_one_agent.ota_updater import OtaError

PY = "python3.13"
ARCHIVE = "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/{tag}.tar.gz"

OTA_STUB = '''\
# the ota_updater of {version} (a stand-in)
def main_verify():
    import os
    with open(os.environ["OTA_MARKER"], "w") as f:
        f.write("{version}")
'''


class FakeVenv:
    """A venv as pip leaves it: site-packages with the agent and a dependency, bin/ with scripts."""

    def __init__(self, root):
        self.prefix = root / "venv"
        self.site = self.prefix / "lib" / PY / "site-packages"
        self.bin = self.prefix / "bin"
        self.site.mkdir(parents=True)
        self.bin.mkdir()
        (self.site / "paho").mkdir()
        (self.site / "paho" / "__init__.py").write_text("# a dependency: never touched\n")
        (self.bin / "pip").write_text("#!pip\n")

    def uninstall(self):
        for d in self.site.glob("agal_one_agent-*.dist-info"):
            for line in (d / "RECORD").read_text().splitlines():
                p = os.path.normpath(os.path.join(self.site, line.split(",")[0]))
                if os.path.isfile(p):
                    os.remove(p)
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self.site / "agal_one_agent", ignore_errors=True)

    def install(self, version, *, requires=(), half=False):
        """pip install of agal-one-agent <version>; half=True: pip died after removing the old one."""
        self.uninstall()
        dist = f"agal_one_agent-{version}.dist-info"
        files = {
            "agal_one_agent/__init__.py": f"VERSION = {version!r}\n",
            "agal_one_agent/main.py": f"# main of {version}\n",
            "agal_one_agent/ota_updater.py": OTA_STUB.format(version=version),
            "agal_one_agent/__pycache__/main.cpython-313.pyc": f"pyc of {version}",
            f"{dist}/METADATA": ("Metadata-Version: 2.1\nName: agal-one-agent\n"
                                 f"Version: {version}\n" + "".join(f"Requires-Dist: {r}\n" for r in requires)),
            f"{dist}/INSTALLER": "pip\n",
            "../../../bin/agal-one-agent": f"#!python\n# console script of {version}\n",
        }
        if half:
            files = {"agal_one_agent/__init__.py": "# half written"}
        for rel, text in files.items():
            p = os.path.normpath(os.path.join(self.site, rel))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        if not half:
            record = "".join(f"{rel},,\n" for rel in files) + f"{dist}/RECORD,,\n"
            (self.site / dist / "RECORD").write_text(record)

    def tree(self) -> dict:
        out = {}
        for dirpath, _, names in os.walk(self.prefix):
            for n in names:
                p = os.path.join(dirpath, n)
                with open(p) as f:
                    out[os.path.relpath(p, self.prefix)] = f.read()
        return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _release(tag: str) -> bytes:
    return f"agal-one-agent release {tag}".encode()


@pytest.fixture
def ota(tmp_path, monkeypatch):
    """ota_updater against a fake venv on agent 0.3.3, with every outside effect faked.

    h.archives: url → bytes the fake download returns (anything else is "not reached").
    h.pip_fail: versions whose install fails (half-way: the old version is gone).
    h.git: whether the board has git. h.now: the fake monotonic clock (sleep advances it)."""
    venv = FakeVenv(tmp_path)
    venv.install("0.3.3")
    state = tmp_path / "state"
    monkeypatch.setattr(ota_updater, "STATE_DIR", str(state))
    monkeypatch.setattr(ota_updater, "STATE_FILE", str(state / "ota_state.json"))
    monkeypatch.setattr(ota_updater, "SITE_PACKAGES", str(venv.site))
    monkeypatch.setattr(ota_updater, "VENV_PREFIX", str(venv.prefix))
    monkeypatch.setattr(ota_updater, "SYSTEMD_DIR", str(tmp_path / "systemd"))
    monkeypatch.setattr(ota_updater, "BUSY_WAIT_S", 600)

    h = types.SimpleNamespace(
        venv=venv, archives={}, archive_versions={}, pip_fail=set(), pip=[], downloads=[], git=True,
        git_asked=0, systemctl=[], systemctl_rc=0, restarts=0, emits=[], now=0.0, sleeps=[],
        check_out=[], deps_installed=[],
    )

    def add_release(tag, data=None):
        data = data or _release(tag)
        h.archives[ARCHIVE.format(tag=tag)] = data
        h.archive_versions[data] = tag[1:]
        return ARCHIVE.format(tag=tag), _sha(data)

    h.add_release = add_release

    def fake_download(url, dest):
        h.downloads.append(url)
        if url not in h.archives:
            raise OtaError("the archive could not be downloaded: not reached")
        with open(dest, "wb") as f:
            f.write(h.archives[url])

    def pip_result(version):
        if version in h.pip_fail:
            venv.install(version, half=True)
            return subprocess.CompletedProcess([], 1, "", "ERROR: boom")
        venv.install(version, requires=getattr(h, "requires", ()))
        return subprocess.CompletedProcess([], 0, "Successfully installed", "")

    def fake_run_pip(args):
        h.pip.append(list(args))
        if args[0] == "check":
            out = h.check_out.pop(0) if h.check_out else ""
            return subprocess.CompletedProcess(args, 1 if out else 0, out, "")
        if "-r" in args:
            line = open(args[args.index("-r") + 1]).read().strip()
            uri = line.split(" @ ", 1)[1].split(" --hash=", 1)[0]
            data = open(urllib.parse.unquote(urllib.parse.urlsplit(uri).path), "rb").read()
            assert line.endswith(f"--hash=sha256:{_sha(data)}")
            return pip_result(h.archive_versions[data])
        if args[-1].startswith("git+"):
            return pip_result(args[-1].rsplit("@", 1)[1][1:])
        h.deps_installed.append(list(args[1:]))
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_have_git():
        h.git_asked += 1
        return h.git

    def fake_systemctl(*args):
        h.systemctl.append(args)
        return subprocess.CompletedProcess(args, h.systemctl_rc, "", "" if h.systemctl_rc == 0 else "Failed")

    def fake_restart():
        h.restarts += 1

    def fake_emit(phase, st, *, error=None):
        h.emits.append((phase, error, dict(st)))

    def fake_sleep(s):
        h.sleeps.append(s)
        h.now += s

    monkeypatch.setattr(ota_updater, "_download", fake_download)
    monkeypatch.setattr(ota_updater, "_run_pip", fake_run_pip)
    monkeypatch.setattr(ota_updater, "_have_git", fake_have_git)
    monkeypatch.setattr(ota_updater, "_systemctl", fake_systemctl)
    monkeypatch.setattr(ota_updater, "_restart_service", fake_restart)
    monkeypatch.setattr(ota_updater, "_emit", fake_emit)
    monkeypatch.setattr(ota_updater, "_sleep", fake_sleep)
    monkeypatch.setattr(ota_updater, "_monotonic", lambda: h.now)
    return h


def _phases(h):
    return [p for p, _, _ in h.emits]


def _installs(h):
    return [a for a in h.pip if a[0] == "install"]


def _update(h, tag="v0.3.4", **kw):
    url, sha = h.add_release(tag)
    kw.setdefault("source", url)
    kw.setdefault("sha256", sha)
    return ota_updater.perform_update(tag, command_id="cmd1", rollout_id="r1", node_uid="node-x", **kw)


# ---- the release archive -------------------------------------------------------

def test_good_archive_is_downloaded_checked_installed_and_restarted(ota):
    before = time.time()
    out = _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify"
    assert (st["previous"], st["target"], st["via"]) == ("v0.3.3", "v0.3.4", "archive")
    assert (st["commandId"], st["rolloutId"], st["nodeUid"]) == ("cmd1", "r1", "node-x")
    assert before + ota_updater.VERIFY_DEADLINE_S - 2 <= st["deadlineEpoch"] <= time.time() + ota_updater.VERIFY_DEADLINE_S + 2
    assert ota.downloads == [ARCHIVE.format(tag="v0.3.4")]
    # the join script's flags: one hashed requirement line, --no-deps --require-hashes
    assert _installs(ota)[0][:4] == ["install", "--no-deps", "--require-hashes", "-r"]
    assert ota_updater.installed_version() == "v0.3.4"
    assert ota.restarts == 1
    assert _phases(ota) == ["started"]
    assert "restarting into probation" in out
    # the archive does not stay on the SD card; the saved 0.3.3 does
    assert os.listdir(os.path.join(ota_updater.STATE_DIR, "ota", "download")) == []
    assert st["snapshot"]["version"] == "v0.3.3"


def test_hash_mismatch_is_refused_before_anything_is_installed(ota):
    before = ota.venv.tree()
    url, _ = ota.add_release("v0.3.4")
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=url, sha256="0" * 64)
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert "sha256" in st["error"] and "not installed" in st["error"]
    assert _installs(ota) == [] and ota.restarts == 0
    assert _phases(ota) == ["started", "failed"]
    assert ota.venv.tree() == before
    assert os.listdir(os.path.join(ota_updater.STATE_DIR, "ota", "download")) == []


@pytest.mark.parametrize("source", [
    "http://github.com/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz",
    "https://github.com/someone-else/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz",
    "https://evil.example/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz",
    "https://github.com.evil.example/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz",
    "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.zip",
    "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/v0.3.4.tar.gz?x=1",
    "https://github.com/logapradeep/agal-one-agent/archive/refs/heads/main.tar.gz",
    "https://github.com/logapradeep/agal-one-agent/archive/refs/tags/v0.3.2.tar.gz",  # not the target
    "file:///tmp/agal-one-agent-v0.3.4.tar.gz",
], ids=["http", "other-owner", "other-host", "lookalike-host", "zip", "query", "branch", "other-tag", "file"])
def test_source_not_on_the_allow_list_is_refused(ota, source):
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=source, sha256="a" * 64)
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert ota.downloads == [] and ota.pip == [] and ota.restarts == 0
    assert _phases(ota) == ["failed"]
    assert ota_updater.installed_version() == "v0.3.3"


@pytest.mark.parametrize("sha", [None, "", "abc", "g" * 64])
def test_archive_without_a_usable_sha256_is_refused(ota, sha):
    url, _ = ota.add_release("v0.3.4")
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=url, sha256=sha)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "sha256" in st["error"]
    assert ota.downloads == [] and ota.pip == []


def test_a_version_that_is_not_a_release_is_refused(ota):
    ota_updater.perform_update("main; rm -rf /", node_uid="node-x")
    assert ota_updater.read_state()["phase"] == "failed"
    assert ota.pip == [] and ota.git_asked == 0


def test_download_failure_is_failed_and_changes_nothing(ota):
    before = ota.venv.tree()
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=ARCHIVE.format(tag="v0.3.4"), sha256="a" * 64)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "not reached" in st["error"]
    assert _phases(ota) == ["started", "failed"]
    assert ota.venv.tree() == before and ota.restarts == 0


# ---- the way back, offline -----------------------------------------------------

def test_pip_failure_puts_the_previous_version_back_offline(ota):
    before = ota.venv.tree()
    ota.pip_fail = {"0.3.4"}
    out = _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert "boom" in st["error"]
    assert ota.venv.tree() == before, "0.3.3 is back, file for file — the half install is gone"
    assert ota_updater.installed_version() == "v0.3.3"
    assert len(_installs(ota)) == 1 and len(ota.downloads) == 1, "the way back needed no pip and no download"
    assert ota.restarts == 0          # never restart into a half-broken state mid-run
    assert _phases(ota) == ["started", "failed"]
    assert "put v0.3.3 back" in out


def test_verify_timer_rollback_puts_the_previous_version_back_offline(ota):
    before = ota.venv.tree()
    _update(ota)
    assert ota_updater.installed_version() == "v0.3.4"
    # the new version never confirmed; the board is offline now and has no git
    ota.archives.clear()
    ota.git = False
    st = ota_updater.read_state()
    st["deadlineEpoch"] = int(time.time()) - 1
    ota_updater.write_state(st)
    pip_before, downloads_before = len(ota.pip), len(ota.downloads)

    out = ota_updater.run_verify()

    assert ota_updater.read_state()["phase"] == "rolled_back"
    assert ota.venv.tree() == before
    assert len(ota.pip) == pip_before and len(ota.downloads) == downloads_before
    assert ota.restarts == 2           # into probation, then back
    assert _phases(ota)[-1] == "rolled_back"
    assert "rolled back v0.3.4 -> v0.3.3" in out


def test_a_damaged_saved_copy_is_not_put_back(ota):
    _update(ota)
    now = ota.venv.tree()
    snap = os.path.join(ota_updater.STATE_DIR, "ota", "previous", "tree", "lib", PY, "site-packages",
                        "agal_one_agent", "main.py")
    with open(snap, "w") as f:
        f.write("# damaged on the SD card")
    st = ota_updater.read_state()
    st["deadlineEpoch"] = 0
    ota_updater.write_state(st)
    out = ota_updater.run_verify()
    assert "FAILED" in out and "damaged" in out
    assert ota.venv.tree() == now, "nothing was taken out when the way back was not whole"
    assert ota_updater.read_state()["phase"] == "failed"
    assert ota.restarts == 2


def test_an_install_without_a_record_is_not_updated(ota):
    for rec in ota.venv.site.glob("agal_one_agent-*.dist-info/RECORD"):
        rec.unlink()
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "no way back" in st["error"]
    assert _installs(ota) == [] and ota.restarts == 0


def test_a_release_that_installs_another_version_is_undone(ota):
    url, sha = ota.add_release("v0.3.4", data=b"mislabelled")
    ota.archive_versions[b"mislabelled"] = "0.3.5"
    ota_updater.perform_update("v0.3.4", node_uid="node-x", source=url, sha256=sha)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "installed v0.3.5, not v0.3.4" in st["error"]
    assert ota_updater.installed_version() == "v0.3.3" and ota.restarts == 0


def test_a_library_the_new_version_added_is_installed(ota):
    ota.requires = ("paho-mqtt>=2.0.0", "newlib>=1.2", 'RPi.GPIO>=0.7.1; extra == "rpi"')
    ota.check_out = ["agal-one-agent 0.3.4 requires newlib, which is not installed.", ""]
    _update(ota)
    assert ota.deps_installed == [["paho-mqtt>=2.0.0", "newlib>=1.2"]], "extras are not installed"
    assert ota_updater.read_state()["phase"] == "pending_verify" and ota.restarts == 1


def test_a_library_that_cannot_be_met_undoes_the_update(ota):
    ota.requires = ("newlib>=1.2",)
    ota.check_out = ["agal-one-agent 0.3.4 requires newlib, which is not installed."] * 2
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "newlib" in st["error"]
    assert ota_updater.installed_version() == "v0.3.3" and ota.restarts == 0


def test_the_verify_launcher_runs_the_saved_versions_code(ota, tmp_path):
    _update(ota)
    launcher = os.path.join(ota_updater.STATE_DIR, "ota", "verify.py")
    service = open(os.path.join(ota_updater.SYSTEMD_DIR, "agal-one-agent-ota-verify.service")).read()
    assert f"ExecStart={ota.venv.prefix}/bin/python -I {launcher}" in service
    assert f"Environment=AGAL_ONE_AGENT_STATE_DIR={ota_updater.STATE_DIR}" in service
    marker = tmp_path / "marker"
    r = subprocess.run([sys.executable, "-I", launcher], env={**os.environ, "OTA_MARKER": str(marker)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "0.3.3", "the rollback check is the saved 0.3.3's, not the new install's"


# ---- the verify-timer is in place before an update ------------------------------

def test_the_verify_timer_is_installed_when_the_board_lacks_it(ota):
    _update(ota)
    units = sorted(os.listdir(ota_updater.SYSTEMD_DIR))
    assert units == ["agal-one-agent-ota-verify.service", "agal-one-agent-ota-verify.timer"]
    assert ("daemon-reload",) in ota.systemctl
    assert ("enable", "--now", "agal-one-agent-ota-verify.timer") in ota.systemctl
    # the next update finds them in place: no reload, the timer only (re)enabled
    ota.systemctl.clear()
    ota_updater.write_state({"phase": "idle"})
    _update(ota, tag="v0.3.5")
    assert ota.systemctl == [("enable", "--now", "agal-one-agent-ota-verify.timer")]


def test_no_update_when_the_verify_timer_cannot_be_started(ota):
    ota.systemctl_rc = 1
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "rollback check" in st["error"]
    assert _installs(ota) == [] and ota.restarts == 0
    assert ota_updater.installed_version() == "v0.3.3"


# ---- not while a motor runs ------------------------------------------------------

def test_a_busy_node_waits_for_its_outputs_then_updates(ota):
    calls = {"n": 0}

    def outputs_on():
        calls["n"] += 1
        return ["Pump (device.power)"] if calls["n"] <= 3 else []

    def download_after_wait(url, dest, _real=ota_updater._download):
        assert calls["n"] >= 4, "nothing is downloaded while the pump runs"
        return _real(url, dest)

    ota_updater._download = download_after_wait  # restored by monkeypatch (set in the fixture)
    _update(ota, outputs_on=outputs_on)
    assert ota.sleeps == [ota_updater.BUSY_POLL_S] * 3
    assert ota.restarts == 1 and _phases(ota) == ["started"]


def test_a_node_that_stays_busy_is_failed_busy_and_left_alone(ota):
    before = ota.venv.tree()
    _update(ota, outputs_on=lambda: ["Pump (device.power)", "the run of plot plot-1"])
    st = ota_updater.read_state()
    assert st["phase"] == "failed"
    assert st["error"].startswith("busy: Pump (device.power), the run of plot plot-1")
    assert sum(ota.sleeps) == pytest.approx(ota_updater.BUSY_WAIT_S)
    assert ota.downloads == [] and ota.pip == [] and ota.restarts == 0
    assert _phases(ota) == ["failed"]
    assert ota.venv.tree() == before


def test_outputs_that_cannot_be_read_count_as_busy(ota):
    def broken():
        raise RuntimeError("runtime lock")
    _update(ota, outputs_on=broken)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "could not be read" in st["error"]
    assert ota.downloads == []


def test_an_output_that_came_on_during_the_install_delays_the_restart(ota):
    seq = iter([[], ["Valve 1 (device.power)"], ["Valve 1 (device.power)"], []])
    _update(ota, outputs_on=lambda: next(seq))
    assert ota.sleeps == [ota_updater.BUSY_POLL_S] * 2
    assert ota.restarts == 1
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify"
    assert st["deadlineEpoch"] <= time.time() + ota_updater.VERIFY_DEADLINE_S + 2


def test_an_output_that_stays_on_after_the_install_undoes_it_without_a_restart(ota):
    before = ota.venv.tree()
    seq = iter([[]])
    _update(ota, outputs_on=lambda: next(seq, ["Pump (device.power)"]))
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and st["error"].startswith("busy: Pump")
    assert ota.venv.tree() == before and ota.restarts == 0
    assert _phases(ota) == ["started", "failed"]


# ---- git, only for a command without an archive -----------------------------------

def test_with_an_archive_git_is_never_used(ota):
    ota.git = False
    _update(ota)
    assert ota.git_asked == 0
    assert not any(a[-1].startswith("git+") for a in ota.pip)
    assert ota.restarts == 1


def test_without_an_archive_git_is_used_when_the_board_has_it(ota, caplog):
    ota_updater.perform_update("0.3.4", node_uid="node-x")
    assert _installs(ota)[0][-1] == "git+https://github.com/logapradeep/agal-one-agent.git@v0.3.4"
    assert "needs git" in caplog.text
    st = ota_updater.read_state()
    assert st["phase"] == "pending_verify" and st["via"] == "git"
    assert st["snapshot"]["version"] == "v0.3.3", "the git path gets the offline way back too"
    assert ota.restarts == 1


def test_without_an_archive_and_without_git_it_says_so(ota):
    ota.git = False
    ota_updater.perform_update("0.3.4", node_uid="node-x")
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "no git" in st["error"]
    assert ota.pip == [] and ota.restarts == 0
    assert _phases(ota) == ["failed"]


# ---- the state machine (as before) ---------------------------------------------------

def test_perform_update_noop_when_same_version(ota):
    ota_updater.perform_update("0.3.3", node_uid="node-x")
    st = ota_updater.read_state()
    assert st["phase"] == "idle"
    assert ota.restarts == 0
    assert ota.pip == []
    assert "succeeded" in _phases(ota)


def test_a_second_update_while_one_runs_is_ignored(ota):
    gate, entered = threading.Event(), threading.Event()

    def slow():
        entered.set()
        gate.wait(5)
        return []

    t = threading.Thread(target=lambda: _update(ota, outputs_on=slow))
    t.start()
    assert entered.wait(5)
    assert "already in progress" in ota_updater.perform_update("v0.3.5", node_uid="node-x")
    gate.set()
    t.join(5)
    assert ota.restarts == 1 and ota.downloads == [ARCHIVE.format(tag="v0.3.4")]


def test_an_update_interrupted_before_the_install_is_settled_failed_at_boot(ota):
    ota_updater.write_state({"phase": "waiting", "previous": "v0.3.3", "target": "v0.3.4", "nodeUid": "node-x"})
    assert ota_updater.settle_interrupted() is True
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and st["error"].startswith("interrupted")
    assert _phases(ota) == ["failed"]
    assert ota_updater.settle_interrupted() is False


def test_confirm_update_settles_probation(ota):
    ota.venv.install("0.3.4")
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.3.3", "target": "v0.3.4",
        "deadlineEpoch": int(time.time()) + 100, "nodeUid": "node-x",
    })
    assert ota_updater.confirm_update(node_uid="node-x") is True
    st = ota_updater.read_state()
    assert st["phase"] == "idle"
    assert st["previous"] == "v0.3.4"   # the target is now the known-good baseline
    assert "succeeded" in _phases(ota)


def test_confirm_update_with_the_old_version_running_reports_failed(ota):
    # the agent restarted while pip ran and 0.3.3 came back up: healthy, but not the update
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.3.3", "target": "v0.3.4",
        "deadlineEpoch": int(time.time()) + 100, "nodeUid": "node-x",
    })
    assert ota_updater.confirm_update(node_uid="node-x") is False
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and st["previous"] == "v0.3.3"
    assert _phases(ota) == ["failed"]


def test_confirm_update_noop_when_not_probation(ota):
    ota_updater.write_state({"phase": "idle", "nodeUid": "node-x"})
    assert ota_updater.confirm_update() is False
    assert ota.emits == []


def test_run_verify_rolls_back_a_state_without_a_snapshot_through_git(ota):
    # ota_state.json written by an agent older than 0.3.4 (no snapshot): git, as before
    ota.venv.install("0.1.6")
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.1.5", "target": "v0.1.6",
        "deadlineEpoch": int(time.time()) - 1, "nodeUid": "node-x",
    })
    out = ota_updater.run_verify()
    st = ota_updater.read_state()
    assert st["phase"] == "rolled_back"
    assert _installs(ota) == [["install", "--upgrade", "--force-reinstall",
                               "git+https://github.com/logapradeep/agal-one-agent.git@v0.1.5"]]
    assert ota.restarts == 1
    assert "rolled_back" in _phases(ota)
    assert "rolled back" in out


def test_run_verify_noop_in_probation(ota):
    ota_updater.write_state({
        "phase": "pending_verify", "previous": "v0.3.3", "target": "v0.3.4",
        "deadlineEpoch": int(time.time()) + 100, "nodeUid": "node-x",
    })
    out = ota_updater.run_verify()
    assert ota_updater.read_state()["phase"] == "pending_verify"  # unchanged
    assert ota.pip == [] and ota.restarts == 0
    assert "probation" in out


def test_run_verify_noop_when_idle(ota):
    ota_updater.write_state({"phase": "idle", "nodeUid": "node-x"})
    out = ota_updater.run_verify()
    assert ota.pip == [] and ota.restarts == 0
    assert "noop" in out


def test_run_verify_rollback_failure_marks_failed(ota):
    ota.pip_fail = {"0.1.5"}
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


# ---- the real download (no network: a fake opener) --------------------------------

class _Resp(io.BytesIO):
    def __init__(self, data, status=200, url="https://codeload.github.com/x.tar.gz", length=None):
        super().__init__(data)
        self.status = status
        self._url = url
        self.headers = {"Content-Length": str(length if length is not None else len(data))}

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener_for(resp):
    return lambda: types.SimpleNamespace(open=lambda req, timeout: resp)


def test_download_streams_into_the_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ota_updater, "_opener", _opener_for(_Resp(b"x" * 1000)))
    dest = tmp_path / "a.tar.gz"
    ota_updater._download(ARCHIVE.format(tag="v0.3.4"), str(dest))
    assert dest.read_bytes() == b"x" * 1000


@pytest.mark.parametrize("resp, match", [
    (_Resp(b"x", length=10 ** 9), "more than"),
    (_Resp(b"x" * 300, length=0), "larger than"),
    (_Resp(b"", status=404), "HTTP 404"),
    (_Resp(b"x", url="http://codeload.github.com/x.tar.gz"), "not allowed"),
    (_Resp(b"x", url="https://evil.example/x.tar.gz"), "not allowed"),
], ids=["declared-too-big", "streamed-too-big", "404", "ended-on-http", "ended-elsewhere"])
def test_download_refusals(tmp_path, monkeypatch, resp, match):
    monkeypatch.setattr(ota_updater, "MAX_ARCHIVE_BYTES", 200)
    monkeypatch.setattr(ota_updater, "_opener", _opener_for(resp))
    dest = tmp_path / "a.tar.gz"
    with pytest.raises(OtaError, match=match):
        ota_updater._download(ARCHIVE.format(tag="v0.3.4"), str(dest))
    assert not dest.exists() and not (tmp_path / "a.tar.gz.part").exists()


@pytest.mark.parametrize("newurl, ok", [
    ("https://codeload.github.com/logapradeep/agal-one-agent/tar.gz/refs/tags/v0.3.4", True),
    ("http://codeload.github.com/logapradeep/agal-one-agent/tar.gz/refs/tags/v0.3.4", False),
    ("https://evil.example/agal-one-agent.tar.gz", False),
])
def test_redirects_only_to_github_over_https(newurl, ok):
    import urllib.request
    handler = ota_updater._AllowListRedirect()
    req = urllib.request.Request(ARCHIVE.format(tag="v0.3.4"))
    if ok:
        assert handler.redirect_request(req, None, 302, "Found", {}, newurl).full_url == newurl
    else:
        with pytest.raises(urllib.error.HTTPError, match="refused"):
            handler.redirect_request(req, None, 302, "Found", {}, newurl)


# ---- main: off the MQTT loop, with the archive and what the node has ON ------------

def test_start_firmware_update_runs_off_the_mqtt_loop_with_the_archive(monkeypatch):
    from unittest.mock import MagicMock
    from agal_one_agent import main as agent_main

    seen, gate = {}, threading.Event()

    def perform_update(version, command_id="", rollout_id="", node_uid="", **kw):
        seen.update(version=version, command_id=command_id, node_uid=node_uid, **kw)
        gate.wait(5)
        return "ok"

    monkeypatch.setattr(ota_updater, "perform_update", perform_update)
    mqtt = MagicMock()
    outputs = lambda: []  # noqa: E731
    started = time.monotonic()
    t = agent_main.start_firmware_update(
        {"commandId": "c1", "type": "firmwareUpdate", "version": "v0.3.4",
         "source": ARCHIVE.format(tag="v0.3.4"), "sha256": "a" * 64}, mqtt, "node-1", outputs_on=outputs)
    assert time.monotonic() - started < 0.5, "the MQTT loop does not wait for the update"
    gate.set()
    t.join(5)
    assert seen == {"version": "v0.3.4", "command_id": "c1", "node_uid": "node-1",
                    "source": ARCHIVE.format(tag="v0.3.4"), "sha256": "a" * 64, "outputs_on": outputs}
    mqtt.publish_command_ack.assert_called_once_with("c1", "executing", ingress=False)


def test_a_command_from_an_older_backend_has_no_archive(monkeypatch):
    from unittest.mock import MagicMock
    from agal_one_agent import main as agent_main

    seen = {}
    monkeypatch.setattr(ota_updater, "perform_update", lambda version, **kw: seen.update(kw) or "ok")
    agent_main.handle_firmware_update({"commandId": "c1", "type": "firmwareUpdate", "version": "v0.3.4"},
                                      MagicMock(), "node-1")
    assert seen["source"] is None and seen["sha256"] is None


def test_main_routes_firmware_update_off_the_loop_with_the_outputs(tmp_path, monkeypatch):
    import yaml
    from agal_one_agent import main as agent_main
    from agal_one_agent.http_reporter import HttpReporter
    from agal_one_agent.mqtt_client import AgalOneMqttClient

    class Captured(Exception):
        pass

    handler = {}

    def capture(self, fn):
        handler["fn"] = fn
        raise Captured

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({
        "node": {"uid": "node-1", "name": "Bench", "auth_token": "tok"}, "mqtt": {"broker": "broker.example"},
        "telemetry": {"ingress_url": "https://ingress.example/x", "buffer_db_path": str(tmp_path / "buf.db")},
        "blocks": {"state_dir": str(tmp_path / "blocks")}}))
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(AgalOneMqttClient, "set_command_handler", capture)
    monkeypatch.setattr(HttpReporter, "report_hardware", lambda self, report: None)
    monkeypatch.setattr("sys.argv", ["agal-one-agent", "--config", str(cfg), "--offline"])
    with pytest.raises(Captured):
        agent_main.main()

    calls = []
    monkeypatch.setattr(agent_main, "start_firmware_update",
                        lambda command, mqtt, node_uid, outputs_on=None: calls.append((command, node_uid, outputs_on)))
    handler["fn"]({"commandId": "c1", "type": "firmwareUpdate", "version": "v0.3.4"})
    assert len(calls) == 1 and calls[0][1] == "node-1"
    assert calls[0][2]() == [], "no program, nothing on"


def test_outputs_on_names_what_the_program_has_on():
    import _contracts
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from agal_one_agent import main as agent_main
    from agal_one_agent.blocks.clock import SimClock
    from agal_one_agent.blocks.io import SimulatedIO
    from agal_one_agent.blocks.runtime import BlockRuntime, RecordingSink
    from agal_one_agent.blocks.simulate import build_bundle_from_defaults

    if not os.path.isdir(_contracts.DEFAULTS):
        pytest.skip(f"defaults not found at {_contracts.DEFAULTS}")
    clock = SimClock(datetime(2026, 9, 7, 5, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    rt = BlockRuntime(SimulatedIO(), RecordingSink(), clock=clock)
    rt.compile(build_bundle_from_defaults(_contracts.DEFAULTS, "farm"))

    def run(seconds):
        for _ in range(seconds):
            rt.step()
            clock.advance(1)

    run(1)
    assert agent_main.outputs_on_for(rt) == []
    rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    run(6)
    on = agent_main.outputs_on_for(rt)
    assert "Pump (device.power)" in on and "Valve 1 (device.power)" in on
    assert "the run of plot plot-1" in on
    rt.apply_command({"type": "stopPlot", "plotId": "plot-1"})
    run(2)
    assert agent_main.outputs_on_for(rt) == []


def test_outputs_on_names_the_legacy_outputs_that_are_on(monkeypatch):
    from agal_one_agent import main as agent_main
    from agal_one_agent.handlers import gpio_handler, pwm_handler

    monkeypatch.setattr(gpio_handler, "_levels", {})
    monkeypatch.setattr(pwm_handler, "_duties", {})
    pin = types.SimpleNamespace(gpio_number=17, physical_pin=11, label="Pump relay", assigned_to=None,
                                pwm_frequency_hz=None, pwm_polarity=None)
    gpio_handler.GpioHandler().write(pin, 1)
    pwm_handler.PwmHandler().write(types.SimpleNamespace(**{**vars(pin), "gpio_number": 18, "label": "Fan"}), 40)
    assert agent_main.outputs_on_for(None) == ["Pump relay (GPIO17)", "Fan (PWM GPIO18)"]
    gpio_handler.GpioHandler().write(pin, 0)
    pwm_handler.PwmHandler().write(types.SimpleNamespace(**{**vars(pin), "gpio_number": 18, "label": "Fan"}), 0)
    assert agent_main.outputs_on_for(None) == []


def test_an_unexpected_error_before_the_install_is_reported_and_changes_nothing(ota, monkeypatch):
    before = ota.venv.tree()

    def boom():
        raise PermissionError("read-only file system")

    monkeypatch.setattr(ota_updater, "_check_free_space", boom)
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "read-only" in st["error"]
    assert _phases(ota) == ["started", "failed"]
    assert ota.venv.tree() == before and ota.restarts == 0


def test_an_unexpected_error_during_the_install_puts_the_previous_version_back(ota, monkeypatch):
    before = ota.venv.tree()

    def broken(archive, sha256):
        ota.venv.install("0.3.4", half=True)
        raise OSError("No space left on device")

    monkeypatch.setattr(ota_updater, "_pip_install_archive", broken)
    _update(ota)
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and "broke off" in st["error"]
    assert ota.venv.tree() == before and ota.restarts == 0
