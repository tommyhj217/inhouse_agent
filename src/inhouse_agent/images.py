"""채팅 이미지 → LLM(비전 모델) 입력.

- Azure OpenAI Chat Completions 의 content 배열 형식: {"type": "image_url", "image_url": {"url": "data:image/png;base64,...", "detail": "auto"}}
- 큰 이미지는 긴 변 max_side 로 줄이고(Pillow 가 있으면), 지원 형식(png/jpeg/gif/webp)이 아니면 png 로 변환합니다.
"""

from __future__ import annotations

import base64
import io
import mimetypes
from pathlib import Path
from typing import Any

SUPPORTED = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
MAX_BYTES = 15 * 1024 * 1024


class ImageError(ValueError):
    pass


def is_image(path: Path | str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXT


def image_part(path: Path | str, max_side: int = 2048, detail: str = "auto") -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ImageError(f"이미지 파일이 없습니다: {p}")
    raw = p.read_bytes()
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    try:
        from PIL import Image
    except ImportError:
        Image = None  # type: ignore[assignment]
    if Image is not None:
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as e:  # noqa: BLE001
            raise ImageError(f"이미지를 열 수 없습니다({p.name}): {e}") from e
        big = max(img.size) > max_side
        if big or mime not in SUPPORTED or len(raw) > MAX_BYTES:
            if big:
                img.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            if img.mode in ("RGBA", "LA", "P") or mime == "image/png":
                img.save(buf, "PNG", optimize=True)
                mime = "image/png"
            else:
                img.convert("RGB").save(buf, "JPEG", quality=88)
                mime = "image/jpeg"
            raw = buf.getvalue()
    elif mime not in SUPPORTED:
        raise ImageError(f"지원하지 않는 이미지 형식입니다({mime}). png/jpg/gif/webp 로 올려 주세요 (또는 Pillow 설치).")
    if len(raw) > MAX_BYTES:
        raise ImageError("이미지가 너무 큽니다 (15MB 이하).")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode()}", "detail": detail}}


def user_content(prompt: str, image_paths: list[str], max_side: int = 2048) -> tuple[list[dict[str, Any]], list[str]]:
    """(content 배열, 실제로 넣은 이미지 경로)"""
    parts: list[dict[str, Any]] = [{"type": "text", "text": prompt or "(이미지 참고)"}]
    used = []
    for ip in image_paths:
        parts.append({"type": "text", "text": f"[첨부 이미지: {ip}]"})
        parts.append(image_part(ip, max_side))
        used.append(str(ip))
    return parts, used


def downgrade_old_images(messages: list[dict[str, Any]], keep_last: int = 1) -> None:
    """오래된 user 메시지의 이미지를 '[첨부 이미지: 경로]' 텍스트로 바꿔 토큰 절약 (필요하면 view_image 로 다시 봄)."""
    idx = [i for i, m in enumerate(messages) if m.get("role") == "user" and isinstance(m.get("content"), list)]
    for i in idx[: max(0, len(idx) - keep_last)]:
        m = messages[i]
        if any(p.get("type") == "image_url" for p in m["content"]):
            m["content"] = [p if p.get("type") != "image_url" else {"type": "text", "text": "(이전 이미지 - 다시 보려면 view_image)"} for p in m["content"]]
