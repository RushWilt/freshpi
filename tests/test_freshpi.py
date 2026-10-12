# SPDX-License-Identifier: Apache-2.0
"""Tests for the freshpi server/CLI and the installer menu, run as real processes.

The menu tests run installer/freshpi-menu against a local server with root-only and
reboot commands replaced by stubs. The registered target disk never exists, and every
command that could write to a disk is a stub that fails loudly, so blat cannot write.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FRESHPI = REPO / "server" / "freshpi.py"
MENU = REPO / "installer" / "freshpi-menu"
SERIAL = "1234abcd"
TARGET = "/dev/freshpi-test-nonexistent"
MENU_TIMEOUT = 5  # the menu loop spins on stdin EOF; never let it run unbounded
DISK_WRITERS = ("dd", "blkdiscard", "mkfs", "mkfs.vfat", "mkfs.ext4", "sfdisk", "wipefs",
                "blockdev", "mount")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.fixture
def env(tmp_path):
    (tmp_path / "tftp").mkdir()
    (tmp_path / "payload").mkdir()
    (tmp_path / "payload" / "config.txt").write_text("x\n")
    (tmp_path / "keys").write_text("ssh-ed25519 AAAA test\n")
    e = {k: v for k, v in os.environ.items() if not k.startswith("FRESHPI_")}
    e.update(FRESHPI_CONFIG=str(tmp_path / "no-config"), FRESHPI_STATE_DIR=str(tmp_path),
             FRESHPI_TFTP_ROOT=str(tmp_path / "tftp"), FRESHPI_KEYS_FILE=str(tmp_path / "keys"),
             FRESHPI_USER="ops", FRESHPI_BIND="127.0.0.1", FRESHPI_PORT=str(free_port()))
    return e


def cli(env, *args, stdin: str = "", **extra):
    # stdin defaults to empty (EOF), so a confirmation prompt answers no instead of hanging.
    return subprocess.run([sys.executable, str(FRESHPI), *args], env={**env, **extra},
                          input=stdin, capture_output=True, text=True, timeout=30)


def state(env) -> dict:
    return json.loads((Path(env["FRESHPI_STATE_DIR"]) / "state.json").read_text())["pis"][SERIAL]


def event_log(env) -> str:
    path = Path(env["FRESHPI_STATE_DIR"]) / "logs" / f"{SERIAL}.log"
    return path.read_text() if path.exists() else ""


@pytest.fixture
def pi(env):
    assert not os.path.exists(TARGET), f"{TARGET} must not exist"
    r = cli(env, "add", SERIAL, "--model", "pi5", "--target", TARGET)
    assert r.returncode == 0, r.stderr
    return env


class Server:
    def __init__(self, env, **extra):
        self.env = {**env, **extra}
        bind = self.env["FRESHPI_BIND"]
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(bind, bind)
        port = int(self.env["FRESHPI_PORT"])
        self.url = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
        self.proc = subprocess.Popen([sys.executable, str(FRESHPI), "serve"], env=self.env,
                                     stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"serve exited: {self.proc.stderr.read()}")
            try:
                socket.create_connection((host, port), 0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("serve did not start")

    def request(self, path: str, data: bytes | None = None) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.url + path, data=data, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stderr.close()


@pytest.fixture
def server(pi):
    servers = []

    def start(**extra):
        s = Server(pi, **extra)
        servers.append(s)
        return s
    yield start
    for s in servers:
        s.stop()


def run_menu(tmp_path, srv: Server, keys: str = "") -> str:
    """Run the real menu until it asks to reboot (stub kills it) or MENU_TIMEOUT expires."""
    assert not os.path.exists(TARGET)
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    marker = tmp_path / "DISK-WRITE-ATTEMPTED"
    scripts = {"reboot": 'echo STUB-REBOOT; kill -TERM "$PPID"', "sleep": "exit 0",
               "ip": "exit 0", "udhcpc": "exit 0", "touch": "exit 0", "clear": "exit 0"}
    for name in DISK_WRITERS:
        scripts[name] = f'echo "$0 $*" >> {marker}; echo "STUB {name} must not run" >&2; exit 99'
    for name, body in scripts.items():
        p = stubs / name
        p.write_text(f"#!/bin/sh\n{body}\n")
        p.chmod(0o755)
    menu_env = {k: v for k, v in os.environ.items() if not k.startswith("FRESHPI_")}
    menu_env.update(PATH=f"{stubs}:{os.environ['PATH']}", TERM="dumb", FRESHPI_SERVER=srv.url,
                    FRESHPI_SERIAL=SERIAL, FRESHPI_DEVMODEL="Raspberry Pi 5 Model B")
    try:
        r = subprocess.run(["bash", str(MENU)], env=menu_env, input=keys, capture_output=True,
                           text=True, timeout=MENU_TIMEOUT)
        out = r.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    assert not marker.exists(), f"disk writer invoked: {marker.read_text()}"
    return out


# --------------------------------------------------------------------------- arm defaults


def test_arm_uses_builtin_defaults(pi):
    r = cli(pi, "arm", SERIAL)
    assert r.returncode == 0, r.stderr
    assert (state(pi)["action"], state(pi)["countdown"]) == ("menu", 30)
    assert (Path(pi["FRESHPI_TFTP_ROOT"]) / SERIAL).is_dir()


def test_arm_uses_configured_defaults(pi):
    cli(pi, "arm", SERIAL, FRESHPI_DEFAULT_ACTION="local", FRESHPI_COUNTDOWN="5")
    assert (state(pi)["action"], state(pi)["countdown"]) == ("local", 5)


def test_arm_config_file_is_read(pi, tmp_path):
    conf = tmp_path / "freshpi.conf"
    conf.write_text("FRESHPI_DEFAULT_ACTION=rescue\nFRESHPI_COUNTDOWN=\"7\"\n")
    cli(pi, "arm", SERIAL, FRESHPI_CONFIG=str(conf))
    assert (state(pi)["action"], state(pi)["countdown"]) == ("rescue", 7)


def test_arm_flags_override_config(pi):
    cli(pi, "arm", SERIAL, "--action", "rescue", "--countdown", "0",
        FRESHPI_DEFAULT_ACTION="local", FRESHPI_COUNTDOWN="5")
    assert (state(pi)["action"], state(pi)["countdown"]) == ("rescue", 0)


@pytest.mark.parametrize("extra, message", [
    ({"FRESHPI_DEFAULT_ACTION": "boot"}, "FRESHPI_DEFAULT_ACTION must be one of"),
    ({"FRESHPI_COUNTDOWN": "-1"}, "FRESHPI_COUNTDOWN must be whole seconds"),
    ({"FRESHPI_COUNTDOWN": "abc"}, "FRESHPI_COUNTDOWN must be whole seconds"),
])
def test_arm_rejects_bad_config_and_leaves_pi_disarmed(pi, extra, message):
    r = cli(pi, "arm", SERIAL, **extra)
    assert r.returncode != 0 and message in r.stderr
    assert state(pi)["armed"] is False
    assert not (Path(pi["FRESHPI_TFTP_ROOT"]) / SERIAL).exists()


def test_arm_rejects_negative_countdown_flag(pi):
    r = cli(pi, "arm", SERIAL, "--countdown", "-1")
    assert r.returncode == 2 and "whole seconds" in r.stderr


@pytest.mark.parametrize("args, extra", [(("--action", "blat"), {}),
                                         ((), {"FRESHPI_DEFAULT_ACTION": "blat"})])
def test_arm_blat_needs_fetched_image(pi, args, extra):
    r = cli(pi, "arm", SERIAL, *args, **extra)
    assert r.returncode != 0 and "no image fetched" in r.stderr
    assert state(pi)["armed"] is False


OTHER_PI5, PI3 = "aaaa0001", "deadbeef"


@pytest.fixture
def fleet(pi):
    cli(pi, "add", OTHER_PI5, "--model", "pi5", "--target", TARGET)
    cli(pi, "add", PI3, "--model", "pi3", "--target", TARGET)
    return pi


def armed_serials(env) -> list[str]:
    pis = json.loads((Path(env["FRESHPI_STATE_DIR"]) / "state.json").read_text())["pis"]
    tftp = Path(env["FRESHPI_TFTP_ROOT"])
    assert all(p["armed"] == (tftp / s).is_dir() for s, p in pis.items())
    return sorted(s for s, p in pis.items() if p["armed"])


def test_arm_group_asks_first(fleet):
    r = cli(fleet, "arm", "pi5")
    assert r.returncode != 0 and "aborted" in r.stderr
    assert f"{SERIAL} (pi5-{SERIAL})" in r.stdout and PI3 not in r.stdout
    assert armed_serials(fleet) == []
    r = cli(fleet, "arm", "pi5", stdin="y\n")
    assert r.returncode == 0, r.stderr
    assert armed_serials(fleet) == [SERIAL, OTHER_PI5]


def test_arm_all_with_yes_and_dry_run(fleet):
    r = cli(fleet, "arm", "all", "--dry-run")
    assert r.returncode == 0 and r.stdout.count("would arm") == 3
    assert armed_serials(fleet) == []
    assert cli(fleet, "arm", "ALL", "--yes").returncode == 0
    assert armed_serials(fleet) == [SERIAL, OTHER_PI5, PI3]


def test_targets_are_a_union_without_duplicates(fleet):
    r = cli(fleet, "arm", "pi3", PI3, f"pi3-{PI3}")
    assert r.returncode == 0, r.stderr  # one Pi, so no question
    assert armed_serials(fleet) == [PI3]


def test_arm_by_hostname_checks_model(fleet):
    assert cli(fleet, "arm", f"PI5-{SERIAL.upper()}").returncode == 0
    assert armed_serials(fleet) == [SERIAL]
    r = cli(fleet, "arm", f"pi3-{OTHER_PI5}")
    assert r.returncode != 0 and f"pi5-{OTHER_PI5}" in r.stderr
    assert armed_serials(fleet) == [SERIAL]


@pytest.mark.parametrize("targets, message", [
    (["all", "ffffffff"], "unknown serial ffffffff"),
    (["all", "pi5-ffffffff"], "unknown serial ffffffff"),
    (["all", "web"], "unknown target 'web'"),
])
def test_one_bad_target_changes_nothing(fleet, targets, message):
    r = cli(fleet, "arm", *targets, "--yes")
    assert r.returncode != 0 and message in r.stderr
    assert armed_serials(fleet) == []


def test_empty_group_is_an_error(pi):
    r = cli(pi, "arm", "pi3")
    assert r.returncode != 0 and "no registered Pis in 'pi3'" in r.stderr


def test_arm_skips_pis_already_armed_the_same_way(fleet):
    cli(fleet, "arm", SERIAL, "--action", "rescue", "--countdown", "5")
    # Only OTHER_PI5 changes, so no question is asked.
    r = cli(fleet, "arm", "pi5", "--action", "rescue", "--countdown", "5")
    assert r.returncode == 0, r.stderr
    assert f"skipped {SERIAL}" in r.stdout and f"armed {OTHER_PI5}" in r.stdout
    assert armed_serials(fleet) == [SERIAL, OTHER_PI5]
    # Different settings re-arm it.
    cli(fleet, "arm", SERIAL, "--action", "local")
    assert state(fleet)["action"] == "local"


def test_disarm_all_skips_disarmed_pis(fleet):
    cli(fleet, "arm", PI3)
    r = cli(fleet, "disarm", "all")
    assert r.returncode == 0, r.stderr
    assert f"disarmed {PI3} (pi3-{PI3})" in r.stdout
    assert f"skipped {SERIAL} (pi5-{SERIAL}): already disarmed" in r.stdout
    assert armed_serials(fleet) == []


def test_disarm_clears_stray_tftp_folder(pi):
    (Path(pi["FRESHPI_TFTP_ROOT"]) / SERIAL).mkdir()
    assert f"disarmed {SERIAL}" in cli(pi, "disarm", SERIAL).stdout
    assert not (Path(pi["FRESHPI_TFTP_ROOT"]) / SERIAL).exists()


def test_list_filters_by_target(fleet):
    out = cli(fleet, "list", "pi3", f"pi5-{SERIAL}").stdout
    assert PI3 in out and SERIAL in out and OTHER_PI5 not in out
    assert OTHER_PI5 in cli(fleet, "list").stdout


def test_remove_and_log_take_one_pi(fleet):
    cli(fleet, "arm", SERIAL)
    assert "armed action=" in cli(fleet, "log", f"pi5-{SERIAL}").stdout
    for command in ("remove", "log"):
        r = cli(fleet, command, "pi5")
        assert r.returncode != 0 and "takes one Pi" in r.stderr
    assert cli(fleet, "remove", f"pi5-{SERIAL}").returncode == 0
    assert not (Path(fleet["FRESHPI_TFTP_ROOT"]) / SERIAL).exists()
    # A removed Pi's log is still readable by serial.
    assert "armed action=" in cli(fleet, "log", SERIAL).stdout



# --------------------------------------------------------------------------- serve


@pytest.mark.parametrize("bind, reachable, refused", [
    ("127.0.0.1", ["127.0.0.1"], ["::1"]),
    pytest.param("::1", ["::1"], ["127.0.0.1"], marks=pytest.mark.skipif(
        not has_ipv6_loopback(), reason="no IPv6 loopback on this host")),
    ("0.0.0.0", ["127.0.0.1"], ["::1"]),
])
def test_serve_binds_only_requested_address(server, bind, reachable, refused):
    srv = server(FRESHPI_BIND=bind)
    port = int(srv.env["FRESHPI_PORT"])
    for host in reachable:
        socket.create_connection((host, port), 1).close()
    for host in refused:
        with pytest.raises(OSError):
            socket.create_connection((host, port), 1).close()


@pytest.mark.parametrize("extra, message", [
    ({"FRESHPI_BIND": "eth0"}, "FRESHPI_BIND must be an IP address"),
    ({"FRESHPI_CONFIRM_BLAT": "maybe"}, "FRESHPI_CONFIRM_BLAT must be yes or no"),
    ({"FRESHPI_USER": ""}, "FRESHPI_USER must be set"),
])
def test_serve_refuses_bad_config(pi, extra, message):
    r = cli(pi, "serve", **extra)
    assert r.returncode != 0 and message in r.stderr


@pytest.mark.parametrize("setting, expected", [(None, True), ("no", False), ("OFF", False),
                                               ("1", True)])
def test_state_reports_confirm_blat(server, setting, expected):
    srv = server(**({} if setting is None else {"FRESHPI_CONFIRM_BLAT": setting}))
    status, body = srv.request(f"/pi/{SERIAL}/state")
    assert status == 200 and json.loads(body)["confirm_blat"] is expected


def test_state_hides_action_while_disarmed(server):
    srv = server()
    cli(srv.env, "arm", SERIAL, "--action", "rescue")
    assert json.loads(srv.request(f"/pi/{SERIAL}/state")[1])["action"] == "rescue"
    cli(srv.env, "disarm", SERIAL)
    assert json.loads(srv.request(f"/pi/{SERIAL}/state")[1])["action"] == "none"


def test_unknown_pi_and_paths_are_404(server):
    srv = server()
    assert srv.request("/pi/ffffffff/state")[0] == 404
    assert srv.request("/nope")[0] == 404
    assert srv.request("/images/../state.json")[0] == 404
    assert srv.request("/images/x.txt")[0] == 404


def test_posts_need_armed_pi(server):
    srv = server()
    assert srv.request(f"/pi/{SERIAL}/disarm", b"")[0] == 409


def test_fail_switches_default_to_rescue_and_stays_armed(server):
    srv = server()
    cli(srv.env, "arm", SERIAL, "--action", "local")
    assert srv.request(f"/pi/{SERIAL}/fail", b"boom")[0] == 200
    pi = state(srv.env)
    assert pi["armed"] is True and pi["action"] == "rescue"


def test_done_disarms_and_removes_tftp_folder(server):
    srv = server()
    cli(srv.env, "arm", SERIAL)
    assert srv.request(f"/pi/{SERIAL}/done", b"image=x")[0] == 200
    assert state(srv.env)["armed"] is False and "last_blat" in state(srv.env)
    assert not (Path(srv.env["FRESHPI_TFTP_ROOT"]) / SERIAL).exists()


def test_seed_user_data(server):
    srv = server()
    status, body = srv.request(f"/pi/{SERIAL}/seed/user-data")
    assert status == 200
    assert "hostname: pi5-1234abcd" in body and "name: ops" in body
    assert '"ssh-ed25519 AAAA test"' in body


def get_with_host(srv: Server, path: str, host: str) -> tuple[int, str]:
    req = urllib.request.Request(srv.url + path, headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_seed_phone_home_uses_address_the_pi_used(server):
    srv = server()
    status, body = get_with_host(srv, f"/pi/{SERIAL}/seed/user-data", "192.0.2.10:8090")
    assert status == 200
    assert f"url: http://192.0.2.10:8090/pi/{SERIAL}/ready" in body


@pytest.mark.parametrize("host", ["a b", "x/y", ""])
def test_seed_rejects_bad_host_header(server, host):
    assert get_with_host(server(), f"/pi/{SERIAL}/seed/user-data", host)[0] == 400


def test_seed_user_defaults_to_cm(env, tmp_path):
    del env["FRESHPI_USER"]
    cli(env, "add", SERIAL, "--model", "pi5", "--target", TARGET)
    srv = Server(env)
    try:
        assert "name: cm\n" in srv.request(f"/pi/{SERIAL}/seed/user-data")[1]
    finally:
        srv.stop()


PHONE_HOME = (b"pub_key_ed25519=ssh-ed25519+AAAAhost+root%40pi5&pub_key_rsa=&"
              b"hostname=pi5-1234abcd&instance_id=freshpi-1234abcd-1")


def test_ready_needs_a_blat(server):
    srv = server()
    assert srv.request(f"/pi/{SERIAL}/ready", PHONE_HOME)[0] == 409
    assert "ready" not in state(srv.env)


def test_ready_recorded_once_per_blat(server):
    srv = server()
    cli(srv.env, "arm", SERIAL)
    srv.request(f"/pi/{SERIAL}/done", b"image=x")
    assert srv.request(f"/pi/{SERIAL}/ready", PHONE_HOME)[0] == 200
    pi = state(srv.env)
    assert pi["address"] == "127.0.0.1"
    assert pi["host_keys"] == {"ed25519": "ssh-ed25519 AAAAhost root@pi5"}
    assert "ready" in pi and pi["armed"] is False
    assert srv.request(f"/pi/{SERIAL}/ready", PHONE_HOME)[0] == 409
    # The next blat forgets the old first boot.
    cli(srv.env, "arm", SERIAL)
    srv.request(f"/pi/{SERIAL}/done", b"image=y")
    assert not {"ready", "address", "host_keys"} & state(srv.env).keys()
    assert srv.request(f"/pi/{SERIAL}/ready", PHONE_HOME)[0] == 200


def test_ready_refused_for_blat_that_expects_no_report(server):
    # A blat finished before the seed had phone_home never reports in. A ready posted for it
    # can only come from something else on the network, so it must not be recorded.
    srv = server()
    path = Path(srv.env["FRESHPI_STATE_DIR"]) / "state.json"
    data = json.loads(path.read_text())
    data["pis"][SERIAL]["last_blat"] = "2026-01-01T00:00:00Z"
    path.write_text(json.dumps(data))
    assert srv.request(f"/pi/{SERIAL}/ready", PHONE_HOME)[0] == 409
    assert "ready" not in state(srv.env)


# --------------------------------------------------------------------------- menu


@pytest.mark.parametrize("countdown", ["2", "0"])
def test_menu_local_default_disarms_after_countdown(server, tmp_path, countdown):
    srv = server()
    cli(srv.env, "arm", SERIAL, FRESHPI_DEFAULT_ACTION="local", FRESHPI_COUNTDOWN=countdown)
    out = run_menu(tmp_path, srv)
    assert "STUB-REBOOT" in out
    assert state(srv.env)["armed"] is False
    assert state(srv.env)["last_event"] == "menu: boot local disk"


def test_menu_keypress_stops_countdown(server, tmp_path):
    srv = server()
    cli(srv.env, "arm", SERIAL, "--action", "local", "--countdown", "5")
    out = run_menu(tmp_path, srv, keys="x")
    assert "STUB-REBOOT" not in out
    assert state(srv.env)["armed"] is True
    assert "Countdown stopped by keypress" in event_log(srv.env)


def test_menu_disarmed_pi_boots_local_disk(server, tmp_path):
    srv = server()
    out = run_menu(tmp_path, srv)
    assert "not armed on the server" in out and "STUB-REBOOT" in out


@pytest.mark.parametrize("setting, keys, prompted, blat_started", [
    (None, "3no\n", True, False),
    (None, "3yes\n", True, True),
    ("no", "3", False, True),
    ("yes", "3\n", True, False),
])
def test_menu_manual_blat_confirmation(server, tmp_path, setting, keys, prompted, blat_started):
    srv = server(**({} if setting is None else {"FRESHPI_CONFIRM_BLAT": setting}))
    cli(srv.env, "arm", SERIAL, "--action", "menu")
    out = run_menu(tmp_path, srv, keys=keys)
    assert ("Type yes to continue" in out) is prompted
    # Blat stops at its target check: the registered disk never exists.
    assert (f"target {TARGET} not present" in event_log(srv.env)) is blat_started


# --------------------------------------------------------------------------- CM handoff


DONE_WITH_KEYS = (b"image=freshpi.img.xz&host=pi5-1234abcd"
                  b"&pub_key_ed25519=ssh-ed25519%20AAAABBBB"
                  b"&pub_key_rsa=ssh-rsa%20AAAACCCC")


def test_done_records_blat_host_keys(server):
    srv = server()
    cli(srv.env, "arm", SERIAL)
    assert srv.request(f"/pi/{SERIAL}/done", DONE_WITH_KEYS)[0] == 200
    pi = state(srv.env)
    assert pi["blat_host_keys"] == {"ed25519": "ssh-ed25519 AAAABBBB",
                                    "rsa": "ssh-rsa AAAACCCC"}
    assert pi["last_event"] == "blat verified image=freshpi.img.xz host=pi5-1234abcd"
    assert "host keys generated during blat" in event_log(srv.env)
    # A later blat replaces the keys.
    cli(srv.env, "arm", SERIAL)
    srv.request(f"/pi/{SERIAL}/done", DONE_WITH_KEYS)
    assert state(srv.env)["blat_host_keys"] == {"ed25519": "ssh-ed25519 AAAABBBB",
                                                "rsa": "ssh-rsa AAAACCCC"}


def test_done_without_keys_keeps_legacy_behaviour(server):
    srv = server()
    cli(srv.env, "arm", SERIAL)
    srv.request(f"/pi/{SERIAL}/done", b"image=x host=y")
    pi = state(srv.env)
    assert "blat_host_keys" not in pi
    assert pi["last_event"] == "blat verified image=x host=y"


def test_inventory_lifecycle(server):
    srv = server()
    cli(srv.env, "add", "deadbeef", "--model", "pi3", "--target", "/dev/mmcblk0")
    data = json.loads(cli(srv.env, "inventory").stdout)
    assert data["pi3"]["hosts"] == ["pi3-deadbeef"]
    assert data["pi5"]["hosts"] == ["pi5-1234abcd"]
    assert data["armed"]["hosts"] == [] and data["ready"]["hosts"] == []
    assert data["provisioning"]["hosts"] == []
    var = json.loads(cli(srv.env, "inventory", "--host", "pi5-1234abcd").stdout)
    assert var["freshpi_serial"] == SERIAL and var["freshpi_model"] == "pi5"
    assert var["ansible_user"] == srv.env["FRESHPI_USER"]
    assert var["freshpi_target"] == TARGET and var["freshpi_armed"] is False
    assert var["ansible_host"] == "pi5-1234abcd"  # hostname by default
    # Arming joins the armed group.
    cli(srv.env, "arm", SERIAL)
    data = json.loads(cli(srv.env, "inventory").stdout)
    assert data["armed"]["hosts"] == ["pi5-1234abcd"]
    assert data["_meta"]["hostvars"]["pi5-1234abcd"]["freshpi_armed"] is True
    # Blat done moves it to provisioning and publishes the generated keys.
    srv.request(f"/pi/{SERIAL}/done", DONE_WITH_KEYS)
    data = json.loads(cli(srv.env, "inventory").stdout)
    assert data["provisioning"]["hosts"] == ["pi5-1234abcd"]
    assert data["ready"]["hosts"] == []
    hv = data["_meta"]["hostvars"]["pi5-1234abcd"]
    assert hv["freshpi_blat_host_keys"] == {"ed25519": "ssh-ed25519 AAAABBBB",
                                            "rsa": "ssh-rsa AAAACCCC"}
    assert hv["freshpi_last_blat"] is not None
    # First boot moves it to ready; --use-address publishes the reported address.
    assert srv.request(f"/pi/{SERIAL}/ready", PHONE_HOME)[0] == 200
    data = json.loads(cli(srv.env, "inventory").stdout)
    assert data["ready"]["hosts"] == ["pi5-1234abcd"]
    assert data["provisioning"]["hosts"] == []
    assert data["_meta"]["hostvars"]["pi5-1234abcd"]["freshpi_ready"] is not None
    data = json.loads(cli(srv.env, "inventory", "--use-address").stdout)
    assert data["_meta"]["hostvars"]["pi5-1234abcd"]["ansible_host"] == "127.0.0.1"
    assert data["_meta"]["hostvars"]["pi3-deadbeef"]["ansible_host"] == "pi3-deadbeef"


def test_inventory_unknown_host_is_empty(server):
    srv = server()
    assert json.loads(cli(srv.env, "inventory", "--host", "pi5-ffffffff").stdout) == {}


def test_freshpi_hostkeys_generates_and_masks(tmp_path):
    root = tmp_path / "rootfs"
    (root / "etc/ssh").mkdir(parents=True)
    (root / "etc/systemd/system").mkdir(parents=True)
    r = subprocess.run(["bash", str(REPO / "installer" / "freshpi-hostkeys"), str(root)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    lines = [line.split() for line in r.stdout.splitlines() if line.strip()]
    assert {line[0] for line in lines} == {"ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"}
    assert all(len(line) == 2 for line in lines)
    for name in ("ssh_host_ed25519_key", "ssh_host_ecdsa_key", "ssh_host_rsa_key"):
        priv = root / "etc/ssh" / name
        pub = root / "etc/ssh" / f"{name}.pub"
        assert priv.exists() and pub.exists()
        assert (priv.stat().st_mode & 0o077) == 0
        assert (pub.stat().st_mode & 0o022) == 0  # public keys are world-readable by design
    unit = root / "etc/systemd/system/regenerate_ssh_host_keys.service"
    assert unit.is_symlink() and os.readlink(unit) == "/dev/null"
    # A second run replaces the keys rather than failing on existing ones.
    r = subprocess.run(["bash", str(REPO / "installer" / "freshpi-hostkeys"), str(root)],
                       capture_output=True, timeout=120)
    assert r.returncode == 0
