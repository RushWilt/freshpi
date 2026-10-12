#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""freshpi: Raspberry Pi network provisioning server and CLI.

One file, standard library only. Two entry points:

    freshpi.py serve            HTTP service polled by the installer on the Pi
    freshpi.py <command> ...    operator CLI (add, arm, disarm, list, fetch-image, ...)

A Pi is "armed" when its serial folder exists in the TFTP root. The Pi
bootloader then loads the installer from that folder; without it the
bootloader misses and falls through to local disk.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import lzma
import os
import re
import shutil
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("FRESHPI_CONFIG", "/etc/freshpi/freshpi.conf"))


def load_config(path: Path) -> None:
    """Read KEY=VALUE lines into the environment; variables already set take precedence."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_config(CONFIG_FILE)

STATE_DIR = Path(os.environ.get("FRESHPI_STATE_DIR", "/var/lib/freshpi"))
TFTP_ROOT = Path(os.environ.get("FRESHPI_TFTP_ROOT", "/srv/tftp"))
KEYS_FILE = Path(os.environ.get("FRESHPI_KEYS_FILE", "/etc/freshpi/authorized_keys"))
LISTEN_PORT = int(os.environ.get("FRESHPI_PORT", "8090"))
# Address the HTTP service binds to: an IPv4/IPv6 address, e.g. 127.0.0.1, 192.0.2.10, 0.0.0.0, ::.
LISTEN_ADDR = os.environ.get("FRESHPI_BIND", "0.0.0.0")
# Account created on every provisioned Pi, for configuration management to log in as.
USERNAME = os.environ.get("FRESHPI_USER", "cm")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOST_HEADER_RE = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$|^\[[0-9A-Fa-f:.]+\](:[0-9]{1,5})?$")
IMAGE_LATEST_URL = "https://downloads.raspberrypi.com/raspios_lite_arm64_latest"

STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.lock"
PAYLOAD_DIR = STATE_DIR / "payload"
IMAGES_DIR = STATE_DIR / "images"
LOGS_DIR = STATE_DIR / "logs"
CURRENT_IMAGE = IMAGES_DIR / "current.json"

SERIAL_RE = re.compile(r"^[0-9a-f]{8}$")
MODELS = {"pi3": "3", "pi5": "5"}
HOSTNAME_RE = re.compile(r"^pi[0-9]+-([0-9a-f]{8})$")
ALL_GROUP = "all"
TARGET_FORMS = (f"a serial (8 hex digits), a hostname such as pi5-1234abcd, "
                f"{', '.join(sorted(MODELS))} or {ALL_GROUP}")
# Menu default after the countdown: local = item 1 (boot local disk, disarms), rescue = item 2,
# blat = item 3; menu = no default, wait for a choice.
ACTIONS = ("local", "rescue", "blat", "menu")
# Defaults for 'arm'; its --action/--countdown override them per Pi.
DEFAULT_ACTION = os.environ.get("FRESHPI_DEFAULT_ACTION", "menu")
DEFAULT_COUNTDOWN = os.environ.get("FRESHPI_COUNTDOWN", "30")
# Whether choosing blat by hand in the installer menu asks for a typed "yes".
CONFIRM_BLAT = os.environ.get("FRESHPI_CONFIRM_BLAT", "yes").strip().lower()
BOOLEANS = {"yes": True, "true": True, "on": True, "1": True,
            "no": False, "false": False, "off": False, "0": False}
# Files in the TFTP root that would make a disarmed Pi load something
# instead of falling through to local disk.
FORBIDDEN_ROOT_FILES = ("config.txt", "start.elf", "kernel8.img", "kernel_2712.img")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_serial(serial: str) -> str:
    serial = serial.lower()
    if not SERIAL_RE.match(serial):
        raise ValueError(f"invalid serial {serial!r}: expected 8 hex digits")
    return serial


def hostname_for(serial: str, pi: dict) -> str:
    return f"pi{MODELS[pi['model']]}-{serial}"


# --------------------------------------------------------------------------- state


@contextlib.contextmanager
def locked_state(write: bool = True):
    """Yield the state dict under an exclusive lock; persist it atomically on exit."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"pis": {}}
        yield state
        if write:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, STATE_FILE)


def get_pi(state: dict, serial: str) -> dict:
    pi = state["pis"].get(serial)
    if pi is None:
        raise KeyError(f"unknown serial {serial}; register it with 'add' first")
    return pi


def label(serial: str, pi: dict) -> str:
    return f"{serial} ({hostname_for(serial, pi)})"


def resolve_targets(state: dict, names: list[str], groups: bool = True) -> list[str]:
    """Serials for CLI targets: a serial, a hostname (pi5-1234abcd), a model group (pi5) or
    'all'. Every name must match a registered Pi and every group at least one."""
    pis = state["pis"]
    serials = set()
    for raw in names:
        name = raw.lower()
        if name == ALL_GROUP or name in MODELS:
            if not groups:
                raise ValueError(f"{raw!r} is a group; this command takes one Pi")
            found = {s for s, pi in pis.items() if name in (ALL_GROUP, pi["model"])}
            if not found:
                raise ValueError(f"no registered Pis in {name!r}")
            serials |= found
        elif SERIAL_RE.match(name):
            get_pi(state, name)
            serials.add(name)
        elif match := HOSTNAME_RE.match(name):
            serial = match.group(1)
            pi = get_pi(state, serial)
            if hostname_for(serial, pi) != name:
                raise ValueError(f"{raw!r} does not match {serial}: it is a {pi['model']}, "
                                 f"{hostname_for(serial, pi)}")
            serials.add(serial)
        else:
            raise ValueError(f"unknown target {raw!r}: expected {TARGET_FORMS}")
    return sorted(serials)


def log_event(serial: str, message: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOGS_DIR / f"{serial}.log", "a") as f:
        f.write(f"{now()} {message}\n")


# --------------------------------------------------------------------------- arming


def tftp_folder(serial: str) -> Path:
    return TFTP_ROOT / valid_serial(serial)


def link_tree(src: Path, dst: Path) -> None:
    """Populate dst from src with hard links (same filesystem), falling back to copies."""
    for path in src.rglob("*"):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)


def require_payload() -> None:
    if not (PAYLOAD_DIR / "config.txt").exists():
        raise FileNotFoundError(f"no installer payload in {PAYLOAD_DIR}; run deploy first")


def arm_folder(serial: str) -> None:
    require_payload()
    folder = tftp_folder(serial)
    staging = folder.with_name(f".{serial}.staging")
    shutil.rmtree(staging, ignore_errors=True)
    link_tree(PAYLOAD_DIR, staging)
    shutil.rmtree(folder, ignore_errors=True)
    os.replace(staging, folder)


def disarm_folder(serial: str) -> None:
    shutil.rmtree(tftp_folder(serial), ignore_errors=True)


def set_disarmed(state: dict, serial: str, reason: str) -> None:
    pi = get_pi(state, serial)
    disarm_folder(serial)
    pi.update(armed=False, updated=now(), last_event=reason)
    log_event(serial, f"disarmed: {reason}")


# --------------------------------------------------------------------------- seed


def config_errors() -> list[str]:
    errors = []
    if not USERNAME_RE.match(USERNAME):
        errors.append("FRESHPI_USER must be set to a valid user name "
                      f"in {CONFIG_FILE} (got {USERNAME!r})")
    if not KEYS_FILE.exists():
        errors.append(f"no SSH keys file at {KEYS_FILE}")
    if CONFIRM_BLAT not in BOOLEANS:
        errors.append(f"FRESHPI_CONFIRM_BLAT must be yes or no (got {CONFIRM_BLAT!r})")
    return errors


def render_user_data(serial: str, pi: dict, server_url: str) -> str:
    if errors := config_errors():
        raise ValueError("; ".join(errors))
    keys = [k.strip() for k in KEYS_FILE.read_text().splitlines()
            if k.strip() and not k.startswith("#")]
    if not keys:
        raise ValueError(f"no SSH keys in {KEYS_FILE}")
    key_lines = "\n".join(f"      - {json.dumps(k)}" for k in keys)
    return f"""#cloud-config
hostname: {hostname_for(serial, pi)}
preserve_hostname: false
manage_etc_hosts: true
enable_ssh: true
ssh_pwauth: false
users:
  - name: {USERNAME}
    groups: [adm, dialout, cdrom, audio, users, sudo, video, plugdev, input,
             gpio, spi, i2c, netdev, render]
    shell: /bin/bash
    lock_passwd: true
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    ssh_authorized_keys:
{key_lines}
# Tells the server first boot finished, from the address the Pi OS itself got.
phone_home:
  url: {server_url}/pi/{serial}/ready
  post: [pub_key_ed25519, pub_key_ecdsa, pub_key_rsa, hostname, instance_id]
  tries: 10
"""


def render_meta_data(serial: str, pi: dict) -> str:
    return f"dsmode: local\ninstance_id: freshpi-{serial}-{int(time.time())}\n"


# --------------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "freshpi/1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def send_json(self, body: dict, status: int = HTTPStatus.OK) -> None:
        data = (json.dumps(body) + "\n").encode()
        self.send_text(data, status, "application/json")

    def send_text(self, data: bytes, status: int = HTTPStatus.OK,
                  ctype: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(self) -> tuple[str, str] | None:
        m = re.fullmatch(r"/pi/([0-9a-f]{8})/([a-z/-]+)", self.path.split("?", 1)[0])
        return (m.group(1), m.group(2)) if m else None

    def do_GET(self):
        if self.path.startswith("/images/"):
            return self.serve_image()
        r = self.route()
        if r is None:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        serial, what = r
        try:
            with locked_state(write=False) as state:
                pi = get_pi(state, serial)
                if what == "state":
                    return self.send_json(self.state_body(serial, pi))
                if what == "seed/user-data":
                    host = self.headers.get("Host", "")
                    if not HOST_HEADER_RE.match(host):
                        return self.send_json({"error": "bad or missing Host header"},
                                              HTTPStatus.BAD_REQUEST)
                    data = render_user_data(serial, pi, f"http://{host}")
                    return self.send_text(data.encode())
                if what == "seed/meta-data":
                    return self.send_text(render_meta_data(serial, pi).encode())
        except KeyError as e:
            return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
        except ValueError as e:
            return self.send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def state_body(self, serial: str, pi: dict) -> dict:
        image = json.loads(CURRENT_IMAGE.read_text()) if CURRENT_IMAGE.exists() else None
        return {
            "serial": serial,
            "armed": pi["armed"],
            "action": pi["action"] if pi["armed"] else "none",
            "countdown": pi["countdown"],
            "confirm_blat": BOOLEANS[CONFIRM_BLAT],
            "model": pi["model"],
            "target": pi["target"],
            "hostname": hostname_for(serial, pi),
            "image": None if image is None else {
                "path": f"/images/{image['file']}",
                "img_sha256": image["img_sha256"],
                "img_size": image["img_size"],
                "name": image["name"],
            },
        }

    def serve_image(self):
        name = self.path[len("/images/"):]
        path = IMAGES_DIR / name
        if "/" in name or not name.endswith(".img.xz") or not path.is_file():
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-xz")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def do_POST(self):
        r = self.route()
        if r is None:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        serial, what = r
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 1 << 20)).decode(errors="replace")
        # Reply only after locked_state has written state.json, so a client that reads state
        # right after a 200 sees the change.
        try:
            with locked_state() as state:
                reply = self.apply_post(state, serial, what, body)
        except KeyError as e:
            reply = ({"error": str(e)}, HTTPStatus.NOT_FOUND)
        self.send_json(*reply)

    def apply_post(self, state: dict, serial: str, what: str, body: str) -> tuple[dict, int]:
        pi = get_pi(state, serial)
        if what == "log":
            for line in body.splitlines():
                log_event(serial, f"pi: {line}")
            return {"ok": True}, HTTPStatus.OK
        if what == "ready":
            return self.record_ready(serial, pi, body)
        if not pi["armed"]:
            return {"error": "not armed"}, HTTPStatus.CONFLICT
        if what == "disarm":
            set_disarmed(state, serial, "menu: boot local disk")
        elif what == "done":
            form = urllib.parse.parse_qs(body)
            detail = body.strip()
            if "image" in form and "host" in form:
                detail = f"image={form['image'][0]} host={form['host'][0]}"
            set_disarmed(state, serial, f"blat verified {detail}")
            # The seed this server serves has phone_home, so this blat's first boot reports in.
            pi.update(last_blat=now(), awaiting_ready=True)
            for key in ("ready", "address", "host_keys"):
                pi.pop(key, None)
            # Verified first contact: the installer generated these in the new rootfs just now.
            keys = {k.removeprefix("pub_key_"): v[0].strip()
                    for k, v in form.items() if k.startswith("pub_key_") and v[0].strip()}
            if keys:
                pi["blat_host_keys"] = keys
                log_event(serial, "host keys generated during blat: "
                                  f"{', '.join(sorted(keys))}")
            else:
                pi.pop("blat_host_keys", None)
        elif what == "fail":
            pi.update(action="rescue", updated=now(),
                      last_event=f"blat failed: {body.strip()}")
            log_event(serial, f"blat failed, default now rescue: {body.strip()}")
        else:
            return {"error": "not found"}, HTTPStatus.NOT_FOUND
        return {"ok": True}, HTTPStatus.OK

    def record_ready(self, serial: str, pi: dict, body: str) -> tuple[dict, int]:
        """cloud-init phone_home after the first boot of a blat; accepted once, and only for a
        blat this server recorded as done (an older seed has no phone_home, so a report for its
        blat can only come from something else on the network)."""
        if not pi.get("awaiting_ready"):
            return {"error": "no blat awaiting first boot"}, HTTPStatus.CONFLICT
        form = urllib.parse.parse_qs(body)
        host_keys = {k.removeprefix("pub_key_"): v[0].strip()
                     for k, v in form.items() if k.startswith("pub_key_") and v[0].strip()}
        address = self.client_address[0]
        del pi["awaiting_ready"]
        pi.update(ready=now(), address=address, host_keys=host_keys, updated=now(),
                  last_event=f"first boot done at {address}")
        log_event(serial, f"first boot done: {form.get('hostname', ['?'])[0]} at {address}, "
                          f"host keys {', '.join(sorted(host_keys)) or 'none'}")
        return {"ok": True}, HTTPStatus.OK


def check_tftp_root() -> list[str]:
    return [n for n in FORBIDDEN_ROOT_FILES if (TFTP_ROOT / n).exists()]


def cmd_serve(args) -> None:
    if errors := config_errors():
        sys.exit("freshpi: " + "; ".join(errors))
    bad = check_tftp_root()
    if bad:
        print("warning: TFTP root contains "
              f"{bad}; disarmed Pis may not fall through", file=sys.stderr)
    try:
        version = ipaddress.ip_address(LISTEN_ADDR).version
    except ValueError:
        sys.exit(f"freshpi: FRESHPI_BIND must be an IP address, got {LISTEN_ADDR!r}")
    family = socket.AF_INET6 if version == 6 else socket.AF_INET
    server_class = type("Server", (ThreadingHTTPServer,), {"address_family": family})
    httpd = server_class((LISTEN_ADDR, LISTEN_PORT), Handler)
    host = f"[{LISTEN_ADDR}]" if family == socket.AF_INET6 else LISTEN_ADDR
    print(f"freshpi serving on {host}:{LISTEN_PORT}", file=sys.stderr)
    httpd.serve_forever()


# --------------------------------------------------------------------------- CLI


def cmd_add(args) -> None:
    serial = valid_serial(args.serial)
    with locked_state() as state:
        state["pis"][serial] = {
            "model": args.model, "target": args.target, "armed": False,
            "action": "menu", "countdown": 0, "updated": now(), "last_event": "added",
        }
    print(f"added {serial} ({args.model}, target {args.target}) as "
          f"{hostname_for(serial, state['pis'][serial])}")


def cmd_remove(args) -> None:
    with locked_state() as state:
        [serial] = resolve_targets(state, [args.target], groups=False)
        name = label(serial, state["pis"][serial])
        disarm_folder(serial)
        del state["pis"][serial]
    print(f"removed {name}")


def countdown_seconds(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value.strip()):
        raise argparse.ArgumentTypeError(f"must be whole seconds (0 or more), got {value!r}")
    return int(value)


def arm_defaults() -> tuple[str, int]:
    """The configured default action and countdown, validated."""
    if DEFAULT_ACTION not in ACTIONS:
        raise ValueError(f"FRESHPI_DEFAULT_ACTION must be one of {', '.join(ACTIONS)}, "
                         f"got {DEFAULT_ACTION!r}")
    try:
        return DEFAULT_ACTION, countdown_seconds(DEFAULT_COUNTDOWN)
    except argparse.ArgumentTypeError as e:
        raise ValueError(f"FRESHPI_COUNTDOWN {e}") from None


def plan_changes(state: dict, names: list[str], needed) -> tuple[list[str], list[str]]:
    """Split the resolved targets into Pis to change and Pis already as asked."""
    serials = resolve_targets(state, names)
    todo = [s for s in serials if needed(s, state["pis"][s])]
    return todo, [s for s in serials if s not in todo]


def confirm(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        print()
        return False


def change_targets(args, verb: str, detail: str, needed, change) -> None:
    """Apply change(state, serial, pi) to every target where needed(serial, pi) is true.

    More than one change is confirmed first (unless --yes). The state lock is not held while
    asking, as that would stall the Pis' requests, so the plan is made again under the write
    lock and must still match: either every planned Pi changes or none does."""
    with locked_state(write=False) as state:
        plan = plan_changes(state, args.targets, needed)
        names = {s: label(s, state["pis"][s]) for s in plan[0] + plan[1]}
    todo, skip = plan
    for serial in skip:
        print(f"skipped {names[serial]}: already {verb}ed{detail}")
    if args.dry_run:
        for serial in todo:
            print(f"would {verb} {names[serial]}{detail}")
        return
    if len(todo) > 1 and not args.yes:
        print("\n".join(f"  {names[s]}" for s in todo))
        if not confirm(f"{verb} these {len(todo)} Pis{detail}? [y/N] "):
            sys.exit("aborted; nothing changed")
    if not todo:
        return
    with locked_state() as state:
        if plan_changes(state, args.targets, needed) != plan:
            raise ValueError("the targets changed while asking; nothing changed, run it again")
        for serial in todo:
            change(state, serial, state["pis"][serial])
    for serial in todo:
        print(f"{verb}ed {names[serial]}{detail}")



def cmd_arm(args) -> None:
    default_action, default_countdown = arm_defaults()
    action = args.action or default_action
    countdown = default_countdown if args.countdown is None else args.countdown
    if action == "blat" and not CURRENT_IMAGE.exists():
        sys.exit("no image fetched yet; run 'fetch-image' first")
    require_payload()

    def needed(serial: str, pi: dict) -> bool:
        return not (pi["armed"] and tftp_folder(serial).is_dir()
                    and (pi["action"], pi["countdown"]) == (action, countdown))

    def change(state: dict, serial: str, pi: dict) -> None:
        arm_folder(serial)
        pi.update(armed=True, action=action, countdown=countdown,
                  updated=now(), last_event=f"armed {action}")
        log_event(serial, f"armed action={action} countdown={countdown}")

    change_targets(args, "arm", f": {action}, countdown {countdown}s", needed, change)


def cmd_disarm(args) -> None:
    def needed(serial: str, pi: dict) -> bool:
        return pi["armed"] or tftp_folder(serial).is_dir()

    def change(state: dict, serial: str, pi: dict) -> None:
        set_disarmed(state, serial, "operator")

    change_targets(args, "disarm", "", needed, change)


def cmd_refresh(args) -> None:
    """After a payload deploy: put bootcode.bin in the TFTP root and re-link armed folders."""
    # A Pi 3 ROM fetches bootcode.bin from the TFTP root before it looks in its serial folder.
    TFTP_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PAYLOAD_DIR / "bootcode.bin", TFTP_ROOT / "bootcode.bin")
    with locked_state() as state:
        armed = [s for s, pi in state["pis"].items() if pi["armed"]]
        for serial in armed:
            arm_folder(serial)
    print(f"refreshed {len(armed)} armed folder(s)")


def cmd_list(args) -> None:
    with locked_state(write=False) as state:
        pis = state["pis"]
        serials = resolve_targets(state, args.targets) if args.targets else sorted(pis)
    print(f"{'serial':8}  {'hostname':14}  {'target':14}  {'armed':5}  "
          f"{'action':6}  {'cd':>3}  last event")
    for serial in serials:
        pi = pis[serial]
        folder = tftp_folder(serial).is_dir()
        armed = "yes" if pi["armed"] else "no"
        if folder != pi["armed"]:
            armed += "!"  # state and TFTP folder disagree
        print(f"{serial:8}  {hostname_for(serial, pi):14}  {pi['target']:14}  "
              f"{armed:5}  {pi['action']:6}  {pi['countdown']:>3}  "
              f"{pi['updated']} {pi['last_event']}")
    bad = check_tftp_root()
    if bad:
        print(f"WARNING: TFTP root contains {bad}")
    installer_image = ""
    if (PAYLOAD_DIR / "SOURCE_IMAGE").exists():
        raw = (PAYLOAD_DIR / "SOURCE_IMAGE").read_text().strip()
        installer_image = raw.removesuffix(".img.xz")
    serve_image = CURRENT_IMAGE.read_text() if CURRENT_IMAGE.exists() else ""
    try:
        serve_name = json.loads(serve_image).get("name", "")
    except Exception:
        serve_name = ""
    if installer_image and serve_name and installer_image != serve_name:
        print(f"WARNING: installer (kernel/firmware) is from {installer_image}, "
              f"but the image the Pis get is {serve_name}. Rebuild + deploy if they should match.")


def cmd_inventory(args) -> None:
    """Print the registered Pis as an Ansible dynamic inventory (JSON)."""
    with locked_state(write=False) as state:
        pis = dict(state["pis"])
    try:
        image = json.loads(CURRENT_IMAGE.read_text()).get("name")
    except (OSError, ValueError):
        image = None
    hostvars: dict[str, dict] = {}
    # Ansible's script inventory takes {"group": {"hosts": [...]}}; the plain list form is
    # deprecated by the script plugin and is removed in ansible-core 2.23.
    groups = {model: {"hosts": []} for model in MODELS} | {
        "armed": {"hosts": []}, "ready": {"hosts": []}, "provisioning": {"hosts": []}}
    for serial, pi in sorted(pis.items()):
        host = hostname_for(serial, pi)
        address = pi.get("address")
        var = {
            "freshpi_serial": serial,
            "freshpi_model": pi["model"],
            "freshpi_target": pi["target"],
            "freshpi_armed": bool(pi["armed"]),
            "freshpi_last_blat": pi.get("last_blat"),
            "freshpi_ready": pi.get("ready"),
            "freshpi_address": address,
            "freshpi_blat_host_keys": pi.get("blat_host_keys"),
            "ansible_host": address if args.use_address and address else host,
            "ansible_user": USERNAME,
        }
        if image:
            var["freshpi_image"] = image
        groups[pi["model"]]["hosts"].append(host)
        if pi["armed"]:
            groups["armed"]["hosts"].append(host)
        if pi.get("ready"):
            groups["ready"]["hosts"].append(host)
        elif pi.get("last_blat"):
            groups["provisioning"]["hosts"].append(host)
        hostvars[host] = var
    if args.host:
        print(json.dumps(hostvars.get(args.host, {}), indent=2, sort_keys=True))
        return
    print(json.dumps({"_meta": {"hostvars": hostvars}, **groups}, indent=2, sort_keys=True))


def cmd_log(args) -> None:
    serial = args.target.lower()
    if not SERIAL_RE.match(serial):  # a removed Pi's log is still readable by serial
        with locked_state(write=False) as state:
            [serial] = resolve_targets(state, [args.target], groups=False)
    path = LOGS_DIR / f"{serial}.log"
    lines = path.read_text().splitlines() if path.exists() else []
    print("\n".join(lines[-args.lines:]))


def cmd_fetch_image(args) -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(args.url, method="HEAD")) as r:
        url = r.geturl()
    file = url.rsplit("/", 1)[1]
    dest = IMAGES_DIR / file
    expected = urllib.request.urlopen(url + ".sha256").read().decode().split()[0]
    if not dest.exists():
        print(f"downloading {url}")
        tmp = dest.with_suffix(".part")
        with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(tmp, dest)
    xz_hash, img_hash, img_size = hashlib.sha256(), hashlib.sha256(), 0
    with open(dest, "rb") as raw:
        while chunk := raw.read(1 << 20):
            xz_hash.update(chunk)
    if xz_hash.hexdigest() != expected:
        dest.unlink()
        sys.exit(f"sha256 mismatch for {file}; deleted")
    with lzma.open(dest) as img:
        while chunk := img.read(1 << 20):
            img_hash.update(chunk)
            img_size += len(chunk)
    meta = {"name": file.removesuffix(".img.xz"), "file": file, "xz_sha256": expected,
            "img_sha256": img_hash.hexdigest(), "img_size": img_size, "fetched": now()}
    CURRENT_IMAGE.write_text(json.dumps(meta, indent=2) + "\n")
    for old in IMAGES_DIR.glob("*.img.xz"):
        if old != dest:
            old.unlink()
    print(f"current image: {meta['name']} ({img_size} bytes, "
          f"img sha256 {meta['img_sha256'][:16]}…)")


def add_change_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("targets", nargs="+", metavar="TARGET", help=TARGET_FORMS)
    parser.add_argument("-y", "--yes", action="store_true",
                        help="do not ask before changing more than one Pi")
    parser.add_argument("--dry-run", action="store_true",
                        help="show which Pis would change; change nothing")


def main() -> None:
    p = argparse.ArgumentParser(prog="freshpi", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    one_pi = "a serial or hostname (pi5-1234abcd)"
    sub.add_parser("serve", help="run the HTTP service").set_defaults(fn=cmd_serve)
    a = sub.add_parser("add", help="register a Pi")
    a.add_argument("serial")
    a.add_argument("--model", choices=sorted(MODELS), required=True)
    a.add_argument("--target", required=True, help="disk blat writes to, e.g. /dev/nvme0n1")
    a.set_defaults(fn=cmd_add)
    r = sub.add_parser("remove", help="unregister a Pi")
    r.add_argument("target", help=one_pi)
    r.set_defaults(fn=cmd_remove)
    a = sub.add_parser("arm", help="arm Pis: their next boot loads the installer")
    add_change_options(a)
    a.add_argument("--action", choices=ACTIONS,
                   help="menu default after the countdown: local (boot local disk), rescue, "
                        "blat, or menu (wait for a choice); "
                        f"default FRESHPI_DEFAULT_ACTION ({DEFAULT_ACTION})")
    a.add_argument("--countdown", type=countdown_seconds,
                   help="seconds before the default runs; "
                        f"default FRESHPI_COUNTDOWN ({DEFAULT_COUNTDOWN})")
    a.set_defaults(fn=cmd_arm)
    d = sub.add_parser("disarm", help="disarm Pis: their next boot goes to local disk")
    add_change_options(d)
    d.set_defaults(fn=cmd_disarm)
    ls = sub.add_parser("list", help="show registered Pis")
    ls.add_argument("targets", nargs="*", metavar="TARGET",
                    help=f"only these Pis: {TARGET_FORMS}")
    ls.set_defaults(fn=cmd_list)
    inv = sub.add_parser("inventory", help="print registered Pis as Ansible inventory JSON")
    inv.add_argument("--list", action="store_true",
                     help="print the group inventory (the default; Ansible passes this)")
    inv.add_argument("--host", help="print the host variables for one inventory host")
    inv.add_argument("--use-address", action="store_true",
                     help="use each Pi's reported address as ansible_host, not its hostname")
    inv.set_defaults(fn=cmd_inventory)
    sub.add_parser("refresh", help="re-link armed folders to the current payload")\
        .set_defaults(fn=cmd_refresh)
    lg = sub.add_parser("log", help="show a Pi's event log")
    lg.add_argument("target", help=one_pi)
    lg.add_argument("-n", "--lines", type=int, default=40)
    lg.set_defaults(fn=cmd_log)
    f = sub.add_parser("fetch-image", help="download and verify the latest Pi OS Lite arm64")
    f.add_argument("--url", default=IMAGE_LATEST_URL)
    f.set_defaults(fn=cmd_fetch_image)
    args = p.parse_args()
    try:
        args.fn(args)
    except (KeyError, ValueError, FileNotFoundError) as e:
        sys.exit(f"error: {e.args[0] if e.args else e}")


if __name__ == "__main__":
    main()
