from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


@contextmanager
def _asset_directory(
    root: Path, parts: tuple[str, ...], *, create_parents: bool = False
) -> Iterator[int | None]:
    # Keep directory descriptors pinned through the operation and reject symlinks.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        try:
            if create_parents:
                root.mkdir(parents=True, exist_ok=True)
            parent = os.open(root, flags)
            stack.callback(os.close, parent)
            for part in parts:
                if create_parents:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                parent = os.open(part, flags, dir_fd=parent)
                stack.callback(os.close, parent)
        except OSError:
            yield None
        else:
            yield parent


@contextmanager
def _asset_entry(
    data_dir: Path, directory: str, value: str | Path | None, *, create_parents: bool = False
) -> Iterator[tuple[int, str] | None]:
    root = data_dir.absolute()
    try:
        parts = Path(value).absolute().relative_to(root / directory).parts if value and "\x00" not in str(value) else ()
    except ValueError:
        parts = ()
    if not parts or ".." in parts:
        yield None
        return

    with _asset_directory(root, (directory, *parts[:-1]), create_parents=create_parents) as parent:
        yield (parent, parts[-1]) if parent is not None else None


def asset_names(data_dir: Path, directory: str, child: str | None = None) -> list[str]:
    if child is not None and (child in ("", ".", "..") or Path(child).name != child):
        return []
    parts = (directory,) if child is None else (directory, child)
    with _asset_directory(data_dir.absolute(), parts) as parent:
        return os.listdir(parent) if parent is not None else []


@contextmanager
def open_asset(
    data_dir: Path, directory: str, value: str | Path | None
) -> Iterator[BinaryIO | None]:
    with _asset_entry(data_dir, directory, value) as entry:
        if entry is None:
            yield None
            return
        parent, name = entry

        def open_entry(path: str, flags: int) -> int:
            return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)

        try:
            asset = open(name, "rb", opener=open_entry)
        except OSError:
            yield None
            return
        with asset:
            yield asset if stat.S_ISREG(os.fstat(asset.fileno()).st_mode) else None


def asset_stat(data_dir: Path, directory: str, value: str | Path | None) -> os.stat_result | None:
    with open_asset(data_dir, directory, value) as asset:
        return os.fstat(asset.fileno()) if asset is not None else None


def read_asset(
    data_dir: Path, directory: str, value: str | Path | None, limit: int
) -> bytes | None:
    try:
        with open_asset(data_dir, directory, value) as asset:
            if asset is None:
                return None
            data = asset.read(limit + 1)
            return data if len(data) <= limit else None
    except OSError:
        return None


def remove_asset(
    data_dir: Path, directory: str, value: str | Path | None, *, older_than: float | None = None
) -> None:
    with _asset_entry(data_dir, directory, value) as entry:
        if entry is None:
            return
        parent, name = entry
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode) and (
                older_than is None or max(metadata.st_mtime, metadata.st_ctime) < older_than
            ):
                os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass


def write_asset(data_dir: Path, directory: str, value: str | Path, data: bytes) -> None:
    with _asset_entry(data_dir, directory, value, create_parents=True) as entry:
        if entry is None:
            raise OSError("The saved asset path is outside managed storage or unavailable.")
        parent, name = entry
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(current.st_mode):
                raise OSError("The saved asset is not a regular file.")
        temporary = f".upload-{uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as asset:
                asset.write(data)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
