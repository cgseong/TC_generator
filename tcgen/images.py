"""첨부 그림을 검사하고 CLI·브라우저가 읽을 수 있는 PNG로 저장한다."""

from __future__ import annotations

import io
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from tcgen.boj import MAX_FIGURE_BYTES
from tcgen.workspace import Workspace

Image.MAX_IMAGE_PIXELS = 20_000_000


def normalize(data: bytes) -> bytes:
    if not data or len(data) > MAX_FIGURE_BYTES:
        raise ValueError("그림은 비어 있지 않은 16MB 이하 파일이어야 합니다.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG", "GIF", "WEBP", "AVIF", "BMP"}:
                    raise ValueError("PNG·JPEG·GIF·WebP·AVIF·BMP 그림을 올려 주세요.")
                image = ImageOps.exif_transpose(source).convert("RGBA")
                # 시각 전사에 충분한 크기. 원본의 거대한 픽셀 배열을 CLI에 넘기지 않는다.
                image.thumbnail((4096, 4096))
                sink = io.BytesIO()
                image.save(sink, format="PNG")
                return sink.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise ValueError("읽을 수 없는 그림이거나 픽셀 수가 너무 큽니다.") from error


def store(workspace: Workspace, number: int, data: bytes) -> Path:
    if not 1 <= number <= 30:
        raise ValueError("그림 번호는 1~30이어야 합니다.")
    path = workspace.figures_dir / f"figure-{number}.png"
    if not workspace.contains(path) or path.is_symlink():
        raise ValueError("작업공간 밖에는 그림을 저장할 수 없습니다.")
    converted = normalize(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".part")
    if staging.is_symlink():
        raise ValueError("그림 임시 경로가 올바르지 않습니다.")
    try:
        staging.write_bytes(converted)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    for old in path.parent.glob(f"figure-{number}.*"):
        if old != path and old.is_file() and not old.is_symlink():
            old.unlink()
    return path


def find(workspace: Workspace, number: int) -> Path | None:
    paths = sorted(workspace.figures_dir.glob(f"figure-{number}.*"))
    return next((p for p in paths if p.suffix.lower() in
                 {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".svg"}
                 and p.is_file() and not p.is_symlink() and workspace.contains(p)), None)
