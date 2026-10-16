#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the installer userland from Debian arm64 packages.

Usage:
    debian-userland.py <rootfs-dir>

Environment, defaults shown:
    DEBIAN_SUITE=trixie
    DEBIAN_MIRROR=https://deb.debian.org/debian                  <suite>, <suite>-updates
    DEBIAN_SECURITY_MIRROR=https://security.debian.org/debian-security    <suite>-security
    DEBIAN_KEYRING=/usr/share/keyrings/debian-archive-keyring.gpg

apt downloads PACKAGES and everything they depend on, checked against the keyring, into a
temporary directory: nothing is installed on this machine and no package script runs, so the
build machine can be any architecture with apt and dpkg. The programs taken from PACKAGES, and
the shared libraries they load (each ELF file's NEEDED entries and interpreter, via readelf),
are copied into <rootfs-dir>, links kept as links, laid out like a merged-/usr Debian system
(/bin, /sbin and /lib link into /usr). Busybox applets are not linked here: the installer's
init runs `busybox --install -s` at boot, which links every applet that is not already a real
program.

Prints "<package> <version>" on stdout for each package that contributed a file.
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile

ARCH = "arm64"
TRIPLET = "aarch64-linux-gnu"

# Debian package -> the programs taken from it; None takes every program it ships.
PACKAGES: dict[str, list[str] | None] = {
    # Installer and blat: busybox (shell, udhcpc, dd, mount, ...), curl, jq, xz, ssh-keygen.
    # Not busybox-static: its shell runs its own applets ahead of real programs (xz, ...).
    "busybox": None,
    "curl": None,
    "jq": None,
    "xz-utils": None,
    "openssh-client": ["ssh-keygen"],
    # Rescue shell.
    "dropbear-bin": None,
    "e2fsprogs": None,
    "dosfstools": None,
    "fdisk": ["sfdisk"],
    "nvme-cli": None,
    "smartmontools": ["smartctl"],
}

PROGRAM_RE = re.compile(r"^/usr/s?bin/[^/]+$")
LIB_DIRS = (f"/usr/lib/{TRIPLET}", "/usr/lib")


def run(*argv: str, cwd: str | None = None) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])} ... failed:\n{proc.stderr.strip()}")
    return proc.stdout


def usr_merged(path: str) -> str:
    """/bin, /sbin and /lib are links into /usr on the target."""
    return re.sub(r"^/(bin|sbin|lib)(/|$)", r"/usr/\1\2", os.path.normpath(path))


class Stage:
    """Every downloaded package unpacked into one tree, recording who ships each path."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.owner: dict[str, str] = {}  # usr-merged path -> package
        self.programs: dict[str, list[str]] = {}  # package -> usr-merged program paths
        self.version: dict[str, str] = {}

    def unpack(self, deb: str) -> None:
        fields = dict(line.split(": ", 1) for line in
                      run("dpkg-deb", "-f", deb, "Package", "Version").splitlines())
        package = fields["Package"]
        self.version[package] = fields["Version"]
        listing = run("dpkg-deb", "-X", deb, self.root).splitlines()
        paths = [usr_merged(line[1:]) for line in listing
                 if line.startswith("./") and not line.endswith("/")]
        for path in paths:
            self.owner.setdefault(path, package)
        self.programs[package] = [p for p in paths if PROGRAM_RE.match(p)]

    def find(self, path: str) -> str | None:
        """Where a usr-merged path sits in the stage: packages may still ship /bin, /lib."""
        for p in (path, re.sub(r"^/usr/", "/", path)):
            if os.path.lexists(self.root + p):
                return self.root + p
        return None


class Rootfs:
    def __init__(self, root: str, stage: Stage) -> None:
        self.root, self.stage = root, stage
        self.copied: dict[tuple[int, int], str] = {}  # stage inode -> first copy (hard links)
        self.used: set[str] = set()  # packages that contributed a file
        for d in ("usr/bin", "usr/sbin", f"usr/lib/{TRIPLET}"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        for d in ("bin", "sbin", "lib"):
            os.symlink(f"usr/{d}", os.path.join(root, d))

    def add(self, path: str) -> None:
        """Copy a path as its package ships it: a link stays a link and its target comes too;
        an ELF file brings the libraries it loads."""
        path = usr_merged(path)
        out = self.root + path
        if os.path.lexists(out):
            return
        src = self.stage.find(path)
        if src is None:
            raise FileNotFoundError(f"{path}: no downloaded package ships it")
        self.used.add(self.stage.owner[path])
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.islink(src):
            target = os.readlink(src)
            os.symlink(target, out)
            # join() keeps an absolute target as it is.
            self.add(os.path.join(os.path.dirname(path), target))
            return
        st = os.lstat(src)
        if (st.st_dev, st.st_ino) in self.copied:
            os.link(self.root + self.copied[(st.st_dev, st.st_ino)], out)
            return
        shutil.copy2(src, out)
        self.copied[(st.st_dev, st.st_ino)] = path
        with open(src, "rb") as f:
            elf = f.read(4) == b"\x7fELF"
        if elf:
            self.add_libraries(path, src)

    def add_libraries(self, path: str, src: str) -> None:
        dyn = run("readelf", "-dlW", src)
        for interp in re.findall(r"\[Requesting program interpreter: (\S+)\]", dyn):
            self.add(interp)
        for name in re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", dyn):
            lib = next((f"{d}/{name}" for d in LIB_DIRS if self.stage.find(f"{d}/{name}")), None)
            if lib is None:
                raise RuntimeError(f"{path} needs {name}, which no downloaded package ships")
            self.add(lib)


def apt_options(work: str) -> list[str]:
    suite = os.environ.get("DEBIAN_SUITE", "trixie")
    mirror = os.environ.get("DEBIAN_MIRROR", "https://deb.debian.org/debian")
    security = os.environ.get("DEBIAN_SECURITY_MIRROR",
                              "https://security.debian.org/debian-security")
    keyring = os.environ.get("DEBIAN_KEYRING", "/usr/share/keyrings/debian-archive-keyring.gpg")
    if not os.path.isfile(keyring):
        raise RuntimeError(f"no keyring at {keyring} (apt install debian-archive-keyring)")
    opts = f"arch={ARCH} signed-by={keyring}"
    with open(f"{work}/sources.list", "w") as f:
        f.write(f"deb [{opts}] {mirror} {suite} main\n"
                f"deb [{opts}] {mirror} {suite}-updates main\n"
                f"deb [{opts}] {security} {suite}-security main\n")
    owned = ("lists", "lists/partial", "cache", "cache/archives", "cache/archives/partial", "debs")
    for d in ("etc.d",) + owned:
        os.makedirs(f"{work}/{d}", exist_ok=True)
    open(f"{work}/status", "w").close()
    # Started as root, downloads run as _apt, as apt does itself.
    sandbox = pwd.getpwuid(os.geteuid()).pw_name
    if os.geteuid() == 0:
        try:
            apt_user = pwd.getpwnam("_apt")
        except KeyError:
            pass
        else:
            sandbox = apt_user.pw_name
            for d in owned:
                os.chown(f"{work}/{d}", apt_user.pw_uid, apt_user.pw_gid)
    # These settings only: none of this machine's apt configuration, sources, pins or
    # installed packages take part.
    settings = {
        "Dir::Etc::main": "/dev/null",
        "Dir::Etc::Parts": f"{work}/etc.d",
        "Dir::Etc::SourceList": f"{work}/sources.list",
        "Dir::Etc::SourceParts": f"{work}/etc.d",
        "Dir::Etc::Preferences": "/dev/null",
        "Dir::Etc::PreferencesParts": f"{work}/etc.d",
        "Dir::State::Lists": f"{work}/lists",
        "Dir::State::status": f"{work}/status",
        "Dir::Cache": f"{work}/cache",
        "APT::Architecture": ARCH,
        "APT::Architectures": ARCH,
        "APT::Sandbox::User": sandbox,
        "Acquire::Languages": "none",
        "Debug::NoLocking": "true",
    }
    return [arg for k, v in settings.items() for arg in ("-o", f"{k}={v}")]


def download(work: str) -> list[str]:
    """PACKAGES and their dependencies (no recommends), as .deb files."""
    opts = apt_options(work)
    run("apt-get", "-q", *opts, "update")
    tree = run("apt-cache", *opts, "depends", "--recurse", "--no-recommends", "--no-suggests",
               "--no-conflicts", "--no-breaks", "--no-replaces", "--no-enhances", *PACKAGES)
    # Top-level lines are packages; indented lines are their relations; <name> is virtual.
    names = sorted({line for line in tree.splitlines() if re.match(r"^[a-z0-9]", line)})
    run("apt-get", "-q", *opts, "download", *names, cwd=f"{work}/debs")
    return sorted(os.path.join(work, "debs", f) for f in os.listdir(f"{work}/debs"))


def build(rootfs: str) -> list[str]:
    if os.path.exists(rootfs) and os.listdir(rootfs):
        raise RuntimeError(f"{rootfs} is not empty")
    work = tempfile.mkdtemp(prefix="freshpi-userland-")
    try:
        stage = Stage(f"{work}/stage")
        for deb in download(work):
            stage.unpack(deb)
        tree = Rootfs(rootfs, stage)
        for package, wanted in PACKAGES.items():
            programs = stage.programs[package]
            if wanted is not None:
                missing = set(wanted) - {os.path.basename(p) for p in programs}
                if missing:
                    raise RuntimeError(f"{package} ships no {', '.join(sorted(missing))}")
                programs = [p for p in programs if os.path.basename(p) in wanted]
            for program in programs:
                tree.add(program)
        return [f"{p} {stage.version[p]}" for p in sorted(tree.used)]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <rootfs-dir>", file=sys.stderr)
        return 2
    try:
        for line in build(sys.argv[1]):
            print(line)
    except Exception as e:
        sys.exit(f"error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
