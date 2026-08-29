"""Safe ZIP extraction helper.

Replaces bare ``ZipFile.extractall()`` calls, which trust entry paths
and declared sizes as-is. Guards against path traversal (``../``,
absolute paths) and decompression-bomb style resource exhaustion.
"""

from pathlib import Path
import zipfile

DEFAULT_MAX_FILES = 20000
DEFAULT_MAX_TOTAL_UNCOMPRESSED = 1024 * 1024 * 1024  # 1GB


class UnsafeZipError(Exception):
    """Raised when a ZIP archive or one of its entries fails safety checks."""


def safe_extract_zip(zf: zipfile.ZipFile, dest_dir: Path,
                      max_files: int = DEFAULT_MAX_FILES,
                      max_total_uncompressed: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED,
                      chunk_size: int = 1024 * 1024) -> None:
    """Extract all entries of an open ``ZipFile`` into ``dest_dir`` safely.

    Every entry's resolved target path must stay within ``dest_dir``.
    Total bytes actually written (not the declared, and therefore
    forgeable, uncompressed size in the ZIP header) are capped by
    ``max_total_uncompressed``.
    """
    dest_root = dest_dir.resolve()
    infolist = zf.infolist()

    if len(infolist) > max_files:
        raise UnsafeZipError(f"ZIP内のファイル数が上限({max_files})を超えています")

    targets = []
    for info in infolist:
        target = (dest_dir / info.filename).resolve()
        try:
            target.relative_to(dest_root)
        except ValueError:
            raise UnsafeZipError(f"不正なパスを含むZIPエントリです: {info.filename}")
        targets.append((info, target))

    total_written = 0
    for info, target in targets:
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, 'wb') as dst:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                total_written += len(chunk)
                if total_written > max_total_uncompressed:
                    raise UnsafeZipError(
                        f"ZIP展開後の合計サイズが上限({max_total_uncompressed}バイト)を超えています"
                    )
                dst.write(chunk)
