#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Build the network-boot payload (kernels, firmware, dtbs, installer initramfs)
# on an x86_64 workstation. Needs docker and sudo (to loop-mount the Pi OS image).
#
#   build/build.sh <raspios-lite-arm64.img.xz | latest> <authorized_keys> <server-url>
#
#   Use "latest" to download the current Pi OS Lite arm64 image (cached in build/cache/,
#   checksum-verified and idempotent), or give a local .img.xz path to build from a specific image.
#
# Output: build/out/payload/ - copied to the server by deploy.sh.
set -eu

[ $# -eq 3 ] || { echo "usage: $0 <raspios-lite-arm64.img.xz | latest> <authorized_keys> <server-url>" >&2; exit 2; }
IMG_XZ=$1
KEYS=$2
SERVER_URL=$3
ALPINE=${ALPINE:-3.24}
PACKAGES="busybox busybox-binsh musl curl xz jq e2fsprogs dosfstools sfdisk nvme-cli smartmontools dropbear openssh-keygen"

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
OUT=$HERE/out
PAYLOAD=$OUT/payload
ROOTFS=$OUT/rootfs
rm -rf "$OUT"
mkdir -p "$PAYLOAD/overlays" "$ROOTFS"

if [ "$IMG_XZ" = "latest" ]; then
    CACHE=$HERE/cache
    IMG_XZ=$(python3 "$HERE/fetch-pi-os.py" \
        https://downloads.raspberrypi.com/raspios_lite_arm64_latest "$CACHE")
    IMG_XZ="$CACHE/$IMG_XZ"
fi
[ -f "$IMG_XZ" ] || { echo "image not found: $IMG_XZ" >&2; exit 1; }
echo "== building from $IMG_XZ"

echo "== installer userland (Alpine $ALPINE aarch64, no emulation needed)"
docker run --rm -v "$ROOTFS:/out" "alpine:$ALPINE" sh -ec "
    trap 'chown -R $(id -u):$(id -g) /out' EXIT
    apk add --quiet --no-cache alpine-keys
    mkdir -p /out/etc/apk/keys && cp /usr/share/apk/keys/aarch64/* /out/etc/apk/keys/
    apk add --quiet --arch aarch64 --root /out --initdb --no-scripts --no-cache \
        -X https://dl-cdn.alpinelinux.org/alpine/v$ALPINE/main \
        -X https://dl-cdn.alpinelinux.org/alpine/v$ALPINE/community \
        $PACKAGES
    rm -rf /out/etc/apk /out/lib/apk /out/var/cache"

install -m 0755 "$REPO/installer/init" "$ROOTFS/init"
install -D -m 0755 "$REPO/installer/freshpi-menu" "$ROOTFS/sbin/freshpi-menu"
install -D -m 0755 "$REPO/installer/freshpi-mount" "$ROOTFS/usr/bin/freshpi-mount"
install -D -m 0755 "$REPO/installer/freshpi-hostkeys" "$ROOTFS/usr/bin/freshpi-hostkeys"
install -D -m 0755 "$REPO/installer/freshpi.script" "$ROOTFS/usr/share/udhcpc/freshpi.script"
install -D -m 0600 "$KEYS" "$ROOTFS/root/.ssh/authorized_keys"
chmod 0700 "$ROOTFS/root" "$ROOTFS/root/.ssh"
printf 'export PS1="freshpi-rescue:\\w# "\nexport PATH=/usr/sbin:/usr/bin:/sbin:/bin\n' > "$ROOTFS/root/.profile"
# Minimal passwd/group: ssh-keygen fails with "No user exists for uid 0" without a passwd
# entry, and dropbear uses it to find root's home (and thus authorized_keys).
printf 'root:x:0:0:root:/root:/bin/sh\n' > "$ROOTFS/etc/passwd"
printf 'root:x:0:\n' > "$ROOTFS/etc/group"
chmod 0644 "$ROOTFS/etc/passwd" "$ROOTFS/etc/group"

echo "== initramfs"
(cd "$ROOTFS" && find . | cpio -o -H newc --owner=0:0 --quiet | gzip -9) > "$PAYLOAD/freshpi-initramfs.gz"

echo "== kernels, firmware, dtbs from $(basename "$IMG_XZ")"
IMG=$OUT/raspios.img
xz -dc "$IMG_XZ" > "$IMG"
LOOP=$(sudo losetup -Pf --show "$IMG")
MNT=$(mktemp -d)
trap 'sudo umount "$MNT" 2>/dev/null; sudo losetup -d "$LOOP"; rmdir "$MNT"; rm -f "$IMG"' EXIT
sudo mount -o ro "${LOOP}p1" "$MNT"
cp "$MNT"/bootcode.bin "$MNT"/kernel8.img "$MNT"/kernel_2712.img "$MNT"/start.elf "$MNT"/fixup.dat \
   "$MNT"/bcm2710-rpi-3-b.dtb "$MNT"/bcm2710-rpi-3-b-plus.dtb \
   "$MNT"/bcm2712-rpi-5-b.dtb "$MNT"/bcm2712d0-rpi-5-b.dtb "$MNT"/bcm2712-d-rpi-5-b.dtb "$PAYLOAD/"
cp "$MNT"/overlays/* "$PAYLOAD/overlays/"
basename "$IMG_XZ" > "$PAYLOAD/SOURCE_IMAGE"

cat > "$PAYLOAD/config.txt" <<'EOF'
# freshpi installer - served only while this Pi is armed
os_check=0
arm_64bit=1
auto_initramfs=0
disable_overscan=1
initramfs freshpi-initramfs.gz followkernel

[pi3]
kernel=kernel8.img

[pi5]
kernel=kernel_2712.img

[all]
EOF
echo "console=tty1 loglevel=3 rdinit=/init freshpi.server=$SERVER_URL" > "$PAYLOAD/cmdline.txt"

du -sh "$PAYLOAD" "$PAYLOAD/freshpi-initramfs.gz"
