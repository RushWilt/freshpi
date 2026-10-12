# FreshPi with OPNsense (Dnsmasq DHCP + Unbound DNS)

Placeholders: Pi network `192.0.2.0/24`, FreshPi server `192.0.2.10`, domain `example.net`.
Menu names below are from OPNsense 26.x and may differ in other versions.

## Network boot

**Services > Dnsmasq DNS & DHCP > DHCP boot**: add one entry for the Pi network.

- Interface: the Pi network
- Filename: `none` (required; use a name that doesn't exist on the FreshPi server)
- Servername: leave empty
- Server address: `192.0.2.10` (the FreshPi server)

That's all the Pis need.

What was tested (OPNsense 26.7, Dnsmasq DHCP; Pi 5 without `TFTP_IP` in its EEPROM): this entry,
first with a file name left over from an earlier PXE setup (`lpxelinux.0`), then with `none`.
With Servername empty, OPNsense writes `dhcp-boot=tag:<interface>,none,192.0.2.10`, which puts
the address in dnsmasq's server-name slot: the Pis receive it as DHCP option 66 (TFTP server)
and in the BOOTP server-name field, and next-server is the firewall's own address. A Pi 5
reinstalled with the leftover file name and booted the installer again with `none`; a Pi 3
fetched `bootcode.bin` with the leftover file name. Both ignored the file name. No vendor
option 43 was set. Filling in Servername replaces the address in option 66; that is untested.

Don't use `bootcode.bin` as the file name: FreshPi's TFTP root has that file, so a PC could
download it and try to run it. FreshPi serves no x86/UEFI loaders, so PCs that network-boot
from this network get "file not found" and fall back to their local disk. Leftover PC boot
entries (`pxelinux`, `bootx64.efi`, ...) and the DHCP options that tag UEFI clients for them
are not needed.

Check from another machine on the network: `tftp 192.0.2.10 -c get bootcode.bin` (needs
`freshpi refresh` to have run once on the server).

## Names for configuration management (recommended: static)

Configuration management needs each Pi at a stable name or address after every reinstall:

1. **Dnsmasq DHCP > Hosts**: add a reservation per Pi (its Ethernet MAC, a fixed address
   outside the dynamic range, hostname `pi<model>-<serial>`, e.g. `pi5-1234abcd`).
2. **Unbound DNS > Overrides**: add a host override for the same name and address, e.g.
   `pi5-1234abcd.example.net` -> `192.0.2.51`.

Don't rely on Unbound's "register DHCP leases" with Dnsmasq as the DHCP server. On at least one
OPNsense 26.x install it never saw Dnsmasq's leases, so leased Pis did not resolve by name.
Check with `dig @<opnsense> pi5-1234abcd.example.net`: it should be `status: NOERROR` with an
answer, not `NXDOMAIN`.
