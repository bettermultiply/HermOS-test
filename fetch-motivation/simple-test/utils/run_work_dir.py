from __future__ import annotations

import shutil
import subprocess as sp
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SNAPSHOTS_BUILD = ROOT / "snapshots-build"
ROOTFS_FILE_CONTROL = SNAPSHOTS_BUILD / "rootfs_file_control"
OVERLAY_EXT4 = SNAPSHOTS_BUILD / "overlay.ext4"

def run_work_dir(base_work_dir: str | Path, run_id: int) -> Path:
    return Path(base_work_dir) / f"run_{run_id}"


def socket_dir(base_work_dir: str | Path) -> Path:
    return Path("/tmp/fc-sock")


def prepare_run_work_dir(base_work_dir: str | Path, run_id: int) -> Path:
    work_dir = run_work_dir(base_work_dir, run_id)
    work_dir.mkdir(parents=True, exist_ok=True)

    overlay_target = work_dir / "overlay.ext4"
    rootfs_link = work_dir / "rootfs_file_control"

    copy_overlay(OVERLAY_EXT4, overlay_target)

    if rootfs_link.exists() or rootfs_link.is_symlink():
        rootfs_link.unlink()
    rootfs_link.symlink_to(ROOTFS_FILE_CONTROL.resolve())

    return work_dir


def copy_overlay(src: Path, dst: Path) -> None:
    try:
        sp.run(
            ["cp", "--reflink=auto", str(src), str(dst)],
            check=True,
            stdout=sp.DEVNULL,
            stderr=sp.DEVNULL,
        )
    except (FileNotFoundError, sp.CalledProcessError):
        shutil.copyfile(src, dst)
