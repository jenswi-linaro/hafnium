# Hafnium RAM disk

Hafnium expects to find the following files in the root directory of its RAM
disk:

*   `manifest.dtb` -- configuration file in DeviceTree format (required)
*   kernels for the VMs, whose names are described in the manifest (optional)
*   initrd of the primary VM, whose name is described in the manifest (optional)

Follow the [preparing Linux](PreparingLinux.md) instructions to produce
`vmlinuz` and `initrd.img` for a basic Linux primary VM.

## Create a RAM disk for Hafnium

Assuming that a subdirectory called `initrd` contains the files listed in the
previous section, we can build `initrd.img` with the following command:

```shell
cd initrd; find . | cpio -o > ../initrd.img; cd -
```
