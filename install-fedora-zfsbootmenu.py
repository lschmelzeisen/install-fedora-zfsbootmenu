#!/usr/bin/env python3

import logging
import os
import shutil
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from itertools import chain
from pathlib import Path
from random import choices
from string import ascii_lowercase, digits
from types import MappingProxyType
from typing import Iterator, Literal, Mapping, Optional, Sequence, Union

# The following class definitions here are not the installation configuration!
# Scroll down to section "Configuration".


@dataclass
class ZfsPool:
    name: str
    disks: Sequence[Path]
    mountpoint: Union[Path, Literal["none", "legacy"], None] = None
    kind: str = "single"
    password: str = ""
    pool_properties: Mapping[str, str] = field(default_factory=dict)
    file_system_properties: Mapping[str, str] = field(default_factory=dict)


@dataclass
class User:
    name: str
    comment: str = ""
    password: str = ""
    wheel: bool = True


@dataclass
class Config:
    root_pool: ZfsPool
    data_pools: Sequence[ZfsPool] = ()
    swap_size: str = "1G"
    root_password: str = ""
    users: Sequence[User] = ()
    packages: Sequence[str] = ()
    locale: str = "en_US.UTF-8"
    keymap: str = "us"
    timezone: str = "UTC"
    hostname: str = "localhost"
    zero_disks: bool = False
    dry_mode: bool = True


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

CONFIG = Config(
    #
    # The ZFS root pool on which to install the operating system and from which to boot.
    # Each disk configured here will be set up with 2-3 GPT partitions (1. EFI, 2. swap
    # if it is configured, 3. partition for ZFS pool). ZFSBootMenu will be installed to
    # the EFI partition of each disk, and each disk will be registered for the UEFI Boot
    # Manager. The following file system layout will be created:
    #
    #   NAME                       MOUNTPOINT
    #   <pool>                     <mountpoint>
    #   <pool>/ROOT                none
    #   <pool>/ROOT/fedora_<uuid>  /
    #   <pool>/home                /home
    #   <pool>/home/root           /root
    #   <pool>/home/<user>         /home/<user>
    #
    # Where the last line will be repeated for every created user.
    # If a password is configured it will be stored in /etc/zfs/<pool>.key such that
    # the pool can be booted with only enter the password once (in ZFSBootMenu). This is
    # acceptable security, since the pool where the .key file is stored is itself
    # encrypted with that password.
    root_pool=ZfsPool(
        name="rpool",
        mountpoint="none",
        disks=[
            Path("/dev/disk/by-path/pci-0000:05:00.0"),
        ],
        kind="single",  # Use "mirror", "raidz1", "raidz2", etc. for more than one disk.
        password="change this pass",  # Empty string means unencrypted pool.
        pool_properties={
            "ashift": "13",
            "autotrim": "on",
        },
        file_system_properties={
            "acltype": "posixacl",
            "compression": "zstd",
            "dnodesize": "auto",
            "normalization": "formD",
            "utf8only": "on",
            "relatime": "on",
            "xattr": "sa",
        },
    ),
    #
    # Other ZFS pools to set up. Zero to arbitrary many pools may be configured. Each
    # will be created with settings and cache files will be set up such that pools
    # auto-mount at boot. Similarly to the root pool, if a password is given it will be
    # stored in /etc/zfs/<pool.key> for auto-mounting.
    data_pools=[
        ZfsPool(
            name="rdata",
            disks=[
                Path("/dev/disk/by-path/pci-0000:06:00.0"),
                Path("/dev/disk/by-path/pci-0000:07:00.0"),
            ],
            kind="mirror",
            password="change this pass",
            pool_properties={
                "ashift": "12",
                "autotrim": "on",
            },
            file_system_properties={
                "acltype": "posixacl",
                "compression": "zstd",
                "dnodesize": "auto",
                "normalization": "formD",
                "utf8only": "on",
                "relatime": "on",
                "xattr": "sa",
            },
        ),
    ],
    #
    # Size of the swap partition of each disk of the root pool. The swap partition will
    # be encrypted with a random password on each boot that is taken from /dev/urandom.
    # The value given here must be understandable by sgdisk. If just the empty string,
    # no swap partition will be created. Note that Fedora (additionally) configures swap
    # on zram by default. To disable that, run "dnf remove zram-generator" once in the
    # installed system
    swap_size="1G",
    #
    # Password for the root user. Empty string means that the root password will be
    # locked (i.e., "passwd --lock root").
    root_password="",
    #
    # User accounts to set up. Zero to arbitrary many users may be configured.
    users=[
        User(
            name="myuser",
            comment="John Doe",  # Empty string means no comment.
            password="change this pass",  # Empty string means no password.
        ),
    ],
    #
    # Packages to install in the new system. Do not change this for a system as close to
    # stock Fedora workstation as possible. Change to "@server-product-environment" for
    # the Fedora server variant. To see a full list of possible group names under, run
    # "dnf group list --hidden -v" on a running Fedora system.
    packages=[
        "@workstation-product-environment",
    ],
    #
    # The default locale for the new system.
    locale="en_US.UTF-8",
    #
    # The keymap for the new system.
    keymap="us-altgr-intl",
    #
    # The timezone for the new system.
    timezone="Europe/Berlin",
    #
    # The hostname for the new system.
    hostname="myhostname",
    #
    # Whether to overwrite all configured disks with zeros before writing to them.
    zero_disks=False,
    #
    # In dry mode this scripts will only print out the shell commands that it would run.
    # Change this to "False" to actually do something. Note that this will irrevocably
    # wipe all data from the disks that you have configured above!
    dry_mode=True,
)

# --------------------------------------------------------------------------------------
# End Configuration
# --------------------------------------------------------------------------------------


LOGGER = logging.getLogger(__name__)
CONFIG_POOLS = (CONFIG.root_pool, *CONFIG.data_pools)
NEW_SYSTEM_ROOT = Path("/rpool")


def main() -> None:
    logging.basicConfig(
        format="{asctime} {levelname:.1} {message}", style="{", level=logging.DEBUG
    )

    check_config_for_errors()

    sys("setenforce", "0")

    version_id = int(
        sys_output(
            'source /etc/os-release; echo "$VERSION_ID"',
            shell=True,
            dry_mode_output="36",
        )
    )
    install_zfs_to_live_environment(version_id)

    blkdiscard_and_zero_disks()
    partition_root_pool_disks()
    pool_key_files = create_zfs_pools_and_file_systems()
    efi_disk_uuids = setup_efi_partitions_and_install_zfsbootmenu()

    for rbind_dir in ("dev", "proc", "sys"):
        mkdir(NEW_SYSTEM_ROOT / rbind_dir)
        sys("mount", "--rbind", f"/{rbind_dir}", str(NEW_SYSTEM_ROOT / rbind_dir))

    install_packages(version_id)

    write_crypttab()
    write_fstab(efi_disk_uuids)
    write_dracut_zfs_conf(pool_key_files)
    write_zfs_mount_generator_cache()
    copy2(Path("/etc/hostid"), NEW_SYSTEM_ROOT / "etc")
    for pool_key_file in pool_key_files:
        copy2(pool_key_file, NEW_SYSTEM_ROOT / "etc/zfs")

    run_systemd_firstboot_and_enable_services()

    with chroot(NEW_SYSTEM_ROOT):
        sys("fixfiles", "-F", "onboot")

        for pool in CONFIG_POOLS:
            sys("zpool", "set", "cachefile=/etc/zfs/zpool.cache", pool.name)

        compile_kernel_dkms_modules_and_generate_initramfs()

        setup_users()

    # TODO: snapshot before first boot?
    sys(
        "zfs",
        "snapshot",
        "-r",
        f"{CONFIG.root_pool.name}@{date.today():%Y%m%d}-before-first-boot",
    )

    sys("umount", "--recursive", "--lazy", str(NEW_SYSTEM_ROOT))
    sleep(10)
    sys("zpool", "export", "-a")


def check_config_for_errors() -> None:
    seen_pool_names = set()
    seen_disks = set()
    for pool in CONFIG_POOLS:
        if pool.name in seen_pool_names:
            raise Exception(f"Name '{pool.name}' configured for multiple pools.")
        seen_pool_names.add(pool.name)

        if not pool.disks:
            raise Exception(f"Pool {pool.name} configured without disks.")
        elif pool.kind == "single" and len(pool.disks) > 1:
            raise Exception(
                f"Pool {pool.name} kind 'single' configured with more than one disk."
            )
        elif pool.kind != "single" and len(pool.disks) == 1:
            raise Exception(
                f"Pool {pool.name} kind '{pool.kind}' configured with only one disk."
            )
        for disk in pool.disks:
            if disk in seen_disks:
                raise Exception(f"Disk '{disk}' configured multiple times.")
            seen_disks.add(disk)

    seen_user_names = set()
    for user in CONFIG.users:
        if user.name in seen_user_names:
            raise Exception(f"Name '{user.name}' configured for multiple users.")
        seen_user_names.add(user.name)


def install_zfs_to_live_environment(version_id: int) -> None:
    sys("rpm", "--erase", "--nodeps", "zfs-fuse")
    # TODO: remove version hacks once official ZFS repos for Fedora 38 are released.
    dnf_install(
        f"https://zfsonlinux.org/fedora/zfs-release-2-2.fc{min(version_id, 37)}.noarch.rpm"
    )
    if version_id > 37:
        sys(
            "sed",
            "--in-place",
            "s/$releasever/37/g",
            str(Path("/") / "etc" / "yum.repos.d" / "zfs.repo"),
        )
    dnf_install(f"kernel-devel-{os.uname().release}", "zfs")
    sys("modprobe", "zfs")


def blkdiscard_and_zero_disks() -> None:
    for pool in CONFIG_POOLS:
        for disk in pool.disks:
            sys("blkdiscard", "--force", str(disk))
            if CONFIG.zero_disks:
                sys(
                    *("dd", "if=/dev/zero", f"of={disk}", "bs=4096", "status=progress"),
                    ignore_exit_code=False,
                )


def partition_root_pool_disks() -> None:
    for disk in CONFIG.root_pool.disks:
        sys("sgdisk", "--zap-all", str(disk))
        sys(
            "sgdisk",
            "--new=0:1M:+512M",
            "--typecode=0:EF00",
            "--change-name=0:EFI System Partition",
            str(disk),
        )
        if CONFIG.swap_size:
            sys(
                "sgdisk",
                f"--new=0:0:+{CONFIG.swap_size}",
                "--typecode=0:8200",
                "--change-name=0:Swap",
                str(disk),
            )
        sys(
            "sgdisk",
            "--new=0:0:0",
            "--typecode=0:BF00",
            f"--change-name=0:ZFS Pool {CONFIG.root_pool.name}",
            str(disk),
        )

    # We sleep here for the kernel to register the new partition layout. I could not
    # find a better way to do this than sleeping. None of "partprobe", "partx",
    # "blockdev --rereadpt", or "systemctl restart systemd-udevd" worked for me.
    sleep(2)


def create_zfs_pools_and_file_systems() -> Sequence[Path]:
    sys("zgenhostid")

    pool_key_files = [
        zpool_create(
            CONFIG.root_pool,
            altroot=NEW_SYSTEM_ROOT,
            partition=("-part3" if CONFIG.swap_size else "-part2"),
        )
    ]
    for pool in CONFIG.data_pools:
        pool_key_files.append(zpool_create(pool))

    zfs_create(CONFIG.root_pool, "ROOT", properties={"mountpoint": "none"})

    root_fs_name = f"ROOT/fedora_{''.join(choices(ascii_lowercase + digits, k=6))}"
    zfs_create(
        CONFIG.root_pool,
        root_fs_name,
        properties={
            "mountpoint": "/",
            "canmount": "noauto",
            "org.zfsbootmenu:rootprefix": "root=zfs:",
            "org.zfsbootmenu:commandline": "ro quiet",
        },
    )
    sys("zfs", "mount", f"{CONFIG.root_pool.name}/{root_fs_name}")
    sys(
        *("zpool", "set", f"bootfs={CONFIG.root_pool.name}/{root_fs_name}"),
        CONFIG.root_pool.name,
    )

    zfs_create(CONFIG.root_pool, "home", properties={"mountpoint": "/home"})
    zfs_create(CONFIG.root_pool, "home/root", properties={"mountpoint": "/root"})
    for user in CONFIG.users:
        zfs_create(CONFIG.root_pool, f"home/{user.name}")

    return [f for f in pool_key_files if f is not None]


def setup_efi_partitions_and_install_zfsbootmenu() -> Sequence[str]:
    sys(
        "curl",
        "--remote-name",
        "--remote-header-name",
        "--location",
        "https://get.zfsbootmenu.org/efi",
    )
    zfsbootmenu_image = (
        next(Path.cwd().glob("zfsbootmenu-*"))
        if not CONFIG.dry_mode
        else Path("zbfsbootmenu.efi")
    )

    efi_disk_uuids = []
    efis_dir = NEW_SYSTEM_ROOT / "boot/efis"
    for disk in CONFIG.root_pool.disks:
        sys("mkfs.fat", "-F", "32", "-s", "1", "-n", "EFI", f"{disk}-part1")

        efi_disk_uuid = sys_output(
            "blkid",
            *("--match-tag", "UUID"),
            *("--output", "value"),
            f"{disk}-part1",
            dry_mode_output="A48C-0D61",
        )
        efi_disk_uuids.append(efi_disk_uuid)

        disk_efi_dir = efis_dir / efi_disk_uuid
        mkdir(disk_efi_dir)
        sys("mount", f"{disk}-part1", str(disk_efi_dir))

        copy2(zfsbootmenu_image, disk_efi_dir)
        sys(
            "efibootmgr",
            "--create",
            *("--disk", str(disk)),
            *("--part", "1"),
            *("--label", f"ZFSBootMenu ({efi_disk_uuid})"),
            *("--loader", f"\\{zfsbootmenu_image.name}"),
        )

    return efi_disk_uuids


def install_packages(version_id: int) -> None:
    # For example: "en_US.UTF-8" -> "en".
    locale_lang = CONFIG.locale[: CONFIG.locale.index("_")]

    # TODO: remove version hacks once official ZFS repos for Fedora 38 are released.
    dnf_install(
        "https://zfsonlinux.org/fedora/"
        f"zfs-release-2-2.fc{min(version_id, 37)}.noarch.rpm",
        "@core",
        "kernel",
        "kernel-devel",
        "kexec-tools",
        "efibootmgr",
        "glibc-minimal-langpack",
        f"glibc-langpack-{locale_lang}",
        "zfs",
        "zfs-dracut",
        *CONFIG.packages,
        installroot=NEW_SYSTEM_ROOT,
        releasever=version_id,
    )
    if version_id > 37:
        sys(
            "sed",
            "--in-place",
            "s/$releasever/37/g",
            str(NEW_SYSTEM_ROOT / "etc" / "yum.repos.d" / "zfs.repo"),
        )


def write_crypttab() -> None:
    if CONFIG.swap_size:
        contents = ""
        for i, disk in enumerate(CONFIG.root_pool.disks):
            contents += (
                f"cryptswap{i + 1} {disk}-part2 /dev/urandom "
                f"swap,cipher=aes-xts-plain64:sha256,size=256,discard\n"
            )
        cat_to_file(NEW_SYSTEM_ROOT / "etc/crypttab", contents)


def write_fstab(efi_disk_uuids: Sequence[str]) -> None:
    contents = ""
    for disk, efi_disk_uuid in zip(CONFIG.root_pool.disks, efi_disk_uuids):
        contents += (
            f"/dev/disk/by-uuid/{efi_disk_uuid} /boot/efis/{efi_disk_uuid} "
            "vfat umask=0077,shortname=winnt,nofail 0 2\n"
        )
    if CONFIG.swap_size:
        for i in range(len(CONFIG.root_pool.disks)):
            contents += (
                f"/dev/mapper/cryptswap{i + 1} none swap "
                "x-systemd.requires=cryptsetup.target,defaults 0 0\n"
            )
    cat_to_file(NEW_SYSTEM_ROOT / "etc/fstab", contents)


def write_dracut_zfs_conf(pool_key_files: Sequence[Path]) -> None:
    contents = 'add_dracutmodules+=" zfs "\n'
    if pool_key_files:
        contents += f'install_items+=" {" ".join(str(f) for f in pool_key_files)} "\n'
    cat_to_file(NEW_SYSTEM_ROOT / "etc/dracut.conf.d/zfs.conf", contents)


def write_zfs_mount_generator_cache() -> None:
    zfs_properties_text = Path(
        "/etc/zfs/zed.d/history_event-zfs-list-cacher.sh"
    ).read_text(encoding="UTF-8")
    zfs_properties: Optional[str] = None
    for line in zfs_properties_text.replace("\\\n", "").splitlines():
        if line.startswith('PROPS="'):
            zfs_properties = line[len('PROPS="') : -len('"')]
    if not zfs_properties:
        raise Exception("Could not determine properties for /etc/zfs/zfs-list.cache")

    zfs_list_cache_dir = NEW_SYSTEM_ROOT / "etc/zfs/zfs-list.cache"
    mkdir(zfs_list_cache_dir)

    for pool in CONFIG_POOLS:
        properties_of_pool = sys_output(
            *("zfs", "list", "-H", "-r"),
            *("-t", "filesystem"),
            *("-o", zfs_properties),
            pool.name,
            dry_mode_output=(
                f"{pool.name}	/	off	on	on	on	on	off	on	off	{pool.name}	"
                f"prompt	-	-	-	-	-	-	-	-"
            ),
        )
        if pool == CONFIG.root_pool:
            # Remove altroot mountpoint that gets prepended to mountpoints by default.
            properties_of_pool = properties_of_pool.replace(
                f"{NEW_SYSTEM_ROOT}/", "/"
            ).replace(f"{NEW_SYSTEM_ROOT}\t", "/\t")
        cat_to_file(zfs_list_cache_dir / pool.name, properties_of_pool)


def run_systemd_firstboot_and_enable_services() -> None:
    rm(NEW_SYSTEM_ROOT / "etc/localtime")
    sys(
        "systemd-firstboot",
        f"--root={NEW_SYSTEM_ROOT}",
        f"--locale={CONFIG.locale}",
        f"--locale-messages={CONFIG.locale}",
        f"--keymap={CONFIG.keymap}",
        f"--timezone={CONFIG.timezone}",
        f"--hostname={CONFIG.hostname}",
        "--force",
    )

    sys(
        "systemctl",
        f"--root={NEW_SYSTEM_ROOT}",
        "enable",
        "systemd-timesyncd",
        "zfs-import-cache",
        "zfs-zed",
        "zfs-import.target",
        "zfs.target",
    )


def compile_kernel_dkms_modules_and_generate_initramfs() -> None:
    for kernel_version in sys_output(
        "rpm", "--query", "kernel", dry_mode_output="kernel-5.17.5-200.fc35.x86_64"
    ).splitlines():
        # For example: "kernel-5.17.5-200.fc35.x86_64" -> "5.17.5-200.fc35.x86_64".
        version = kernel_version[len("kernel-") :]
        sys("kernel-install", "add", version, f"/usr/lib/modules/{version}/vmlinuz")


def setup_users() -> None:
    passwd("root", CONFIG.root_password)
    for user in CONFIG.users:
        user_home_dir = Path("/home") / user.name
        sys(
            "useradd",
            "--user-group",
            *(("--groups", "wheel") if user.wheel else ()),
            *(("--comment", f"{user.comment}") if user.comment else ()),
            *("--no-create-home", "--home-dir", str(user_home_dir)),
            f"{user.name}",
        )
        sys(
            *("zfs", "allow", "-u", user.name, "mount,snapshot,destroy"),
            f"{CONFIG.root_pool.name}/home/{user.name}",
        )
        copytree(Path("/etc/skel"), user_home_dir)
        chown(user_home_dir, user.name, user.name, recursive=True)
        chmod(user_home_dir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        sys("restorecon", str(user_home_dir))
        passwd(user.name, user.password, lock_if_empty=False)


# --------------------------------------------------------------------------------------
# Bash primitives
# --------------------------------------------------------------------------------------


def zpool_create(
    pool: ZfsPool,
    altroot: Optional[Path] = None,
    partition: str = "",
) -> Optional[Path]:
    key_file: Optional[Path] = None
    if pool.password:
        key_file = Path(f"/etc/zfs/{pool.name}.key")
        cat_to_file(key_file, f"{pool.password}\n")
        chmod(key_file, 0)

    zpool_create_args = ["zpool", "create", "-f"]
    for key, value in pool.pool_properties.items():
        zpool_create_args += ("-o", f"{key}={value}")
    for key, value in pool.file_system_properties.items():
        zpool_create_args += ("-O", f"{key}={value}")
    if key_file:
        zpool_create_args += ("-O", "encryption=aes-256-gcm")
        zpool_create_args += ("-O", "keyformat=passphrase")
        zpool_create_args += ("-O", f"keylocation=file://{key_file}")
    if pool.mountpoint:
        zpool_create_args += ("-m", str(pool.mountpoint))
    if altroot:
        zpool_create_args += ("-R", str(altroot))
    zpool_create_args.append(pool.name)
    if pool.kind != "single":
        zpool_create_args.append(pool.kind)
    for disk in pool.disks:
        zpool_create_args.append(f"{disk}{partition}")
    sys(*zpool_create_args)

    return key_file


def zfs_create(
    pool: ZfsPool,
    filesystem: str,
    *,
    properties: Mapping[str, str] = MappingProxyType({}),
) -> None:
    zfs_create_args = ["zfs", "create"]
    for key, value in properties.items():
        zfs_create_args += ("-o", f"{key}={value}")
    zfs_create_args.append(f"{pool.name}/{filesystem}")
    sys(*zfs_create_args)


def dnf_install(
    *args: str,
    installroot: Optional[Path] = None,
    releasever: Optional[int] = None,
) -> None:
    dnf_install_args = ["dnf", "install", "--assumeyes"]
    if installroot:
        dnf_install_args.append(f"--installroot={installroot}")
    if releasever:
        dnf_install_args.append(f"--releasever={releasever}")
    dnf_install_args.extend(args)
    sys(*dnf_install_args)


def passwd(user_name: str, password: str, lock_if_empty: bool = True) -> None:
    if password:
        LOGGER.info(f"passwd {user_name} --stdin")
        if not CONFIG.dry_mode:
            passwd_proc = subprocess.Popen(
                ("passwd", user_name, "--stdin"), stdin=subprocess.PIPE
            )
            passwd_proc.communicate(password.encode())
            if passwd_proc.returncode:
                raise subprocess.CalledProcessError(
                    passwd_proc.returncode, passwd_proc.args
                )
    elif lock_if_empty:
        sys("passwd", "--lock", "root")
    else:
        sys("passwd", user_name, "--delete")


@contextmanager
def chroot(newroot: Path) -> Iterator[None]:
    LOGGER.info(f"chroot {newroot}")
    oldroot_fd = os.open(Path("/"), os.O_PATH)
    try:
        if not CONFIG.dry_mode:
            os.chdir(newroot)
            os.chroot(".")
        yield None
    finally:
        LOGGER.info(f"exit  # chroot {newroot}")
        if not CONFIG.dry_mode:
            os.chdir(oldroot_fd)
            os.chroot(".")
        os.close(oldroot_fd)


def copy2(src: Path, dst: Path) -> None:
    LOGGER.info(f"cp --archive {src} {dst}")
    if not CONFIG.dry_mode:
        shutil.copy2(src, dst)


def copytree(src: Path, dst: Path) -> None:
    LOGGER.info(f"cp --archive --recursive {src} {dst}")
    if not CONFIG.dry_mode:
        shutil.copytree(src, dst, dirs_exist_ok=True)


def chown(path: Path, user: str, group: str, recursive: bool = True) -> None:
    LOGGER.info(f"chown {'--recursive' if recursive else ''} {user}:{group} {path}")
    if not CONFIG.dry_mode:
        if recursive:
            for p in chain((path,), path.glob("**/*")):
                shutil.chown(p, user, group)
        else:
            shutil.chown(path, user, group)


def chmod(path: Path, mode: int) -> None:
    LOGGER.info(f"chmod {mode:o} {path} ")
    if not CONFIG.dry_mode:
        os.chmod(path, mode)


def mkdir(path: Path) -> None:
    LOGGER.info(f"mkdir --parents {path}")
    if not CONFIG.dry_mode:
        path.mkdir(parents=True)


def rm(path: Path) -> None:
    LOGGER.info(f"rm --force {path}")
    if not CONFIG.dry_mode:
        path.unlink(missing_ok=True)


def sleep(secs: float) -> None:
    LOGGER.info(f"sleep {secs}")
    if not CONFIG.dry_mode:
        time.sleep(secs)


def cat_to_file(file: Path, contents: str) -> None:
    LOGGER.info(f"cat > {file} << EOF")
    for line in contents.splitlines():
        LOGGER.info(line)
    LOGGER.info("EOF")
    if not CONFIG.dry_mode:
        file.write_text(contents, encoding="UTF-8")


def sys(*args: str, ignore_exit_code: bool = True) -> None:
    LOGGER.info(" ".join(arg if " " not in arg else f'"{arg}"' for arg in args))
    if not CONFIG.dry_mode:
        if ignore_exit_code:
            subprocess.check_call(args)
        else:
            subprocess.call(args)


def sys_output(*args: str, dry_mode_output: str, shell: bool = False) -> str:
    LOGGER.info(" ".join(arg if " " not in arg else f'"{arg}"' for arg in args))
    if not CONFIG.dry_mode:
        output = subprocess.check_output(args, shell=shell).decode()[: -len("\n")]
    else:
        output = dry_mode_output
    for line in output.splitlines():
        LOGGER.info(f"#> {line}")
    return output


if __name__ == "__main__":
    main()
