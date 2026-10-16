# Changelog

All notable changes to FreshPi are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- The installer userland is built from Debian arm64 packages (`build/debian-userland.py`)
  instead of Alpine in a docker container: `build/build.sh` needs no docker and runs on any
  Debian-family machine, any architecture, and `DEBIAN_MIRROR`/`DEBIAN_SECURITY_MIRROR` can
  point it at a local mirror for offline builds. The payload lists the packages it was built
  from in `SOURCE_PACKAGES`. The initramfs grows from about 10 to 16 MB.

## [0.0.1] - 2026-10-11

Initial release. Tested on hardware with a Raspberry Pi 5 booting from NVMe; the Pi 3 is untested
and the Pi 4 is not supported yet.
