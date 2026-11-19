# Changelog

All notable changes to FreshPi are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `freshpi add --root-size SIZE` (e.g. `16G`): the blat keeps the root partition at that size
  and makes the rest of the target a third partition, unformatted, for a data volume beside the
  system on a one-disk Pi. Pi OS's first-boot resize is replaced (new disk id, `resize` taken
  off `cmdline.txt`). The size is in the installer's state, `freshpi list` (`root` column) and
  the inventory (`freshpi_root_mib`). Before splitting, the installer sets its clock from the
  server's HTTP `Date` header: at 1970 (no RTC, no NTP) `resize2fs` refuses the image's root.
  Needs a rebuilt and deployed payload (`installer/freshpi-split`).

## [0.0.2] - 2026-11-04

### Added

- `freshpi list` has an `address` column: the address each Pi reported at its first boot after
  its last blat, `-` until then.

### Changed

- The installer userland is built from Debian arm64 packages (`build/debian-userland.py`)
  instead of Alpine in a docker container: `build/build.sh` needs no docker and runs on any
  Debian-family machine, any architecture, and `DEBIAN_MIRROR`/`DEBIAN_SECURITY_MIRROR` can
  point it at a local mirror for offline builds. The payload lists the packages it was built
  from in `SOURCE_PACKAGES`. The initramfs grows from about 10 to 16 MB.

## [0.0.1] - 2026-10-11

Initial release. Tested on hardware with a Raspberry Pi 5 booting from NVMe; the Pi 3 is untested
and the Pi 4 is not supported yet.
