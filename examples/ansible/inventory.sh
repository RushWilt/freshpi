#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Ansible dynamic inventory for FreshPi (protocol: --list / --host <name>).
# Read-only: it runs `freshpi inventory` over SSH on the FreshPi server.
#
#   ansible -i examples/ansible/inventory.sh pi5 -m ping
#
# Point it at your server with FRESHPI_ADMIN:
#   FRESHPI_ADMIN=admin@freshpi.example.net ansible -i examples/ansible/inventory.sh ...
# The variable must be in the script's environment (exported or set inline) — a plain
# shell assignment is not seen by this script.
SERVER=${FRESHPI_ADMIN:?set FRESHPI_ADMIN to the FreshPi server, e.g. admin@freshpi.example.net}
exec ssh "$SERVER" sudo freshpi inventory "$@"
