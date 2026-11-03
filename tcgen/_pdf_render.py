"""PDF 렌더러 전용 자식 프로세스. 이미지 코덱과 MuPDF를 같은 프로세스에 적재하지 않는다."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    try:
        import pymupdf
    except ImportError:
        print(json.dumps({"error": "PDF 변환에 PyMuPDF가 필요합니다. requirements.txt를 설치해 주세요."}))
        return 1
    try:
        source, folder = Path(sys.argv[1]), Path(sys.argv[2])
        offset, remaining = int(sys.argv[3]), int(sys.argv[4])
        if source.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("PDF는 20MB 이하여야 합니다.")
        names = []
        with pymupdf.open(source) as document:
            if not 1 <= document.page_count <= remaining:
                raise ValueError(f"추가로 첨부할 수 있는 PDF 페이지는 {remaining}장입니다. PDF를 나눠 주세요.")
            for page in document:
                scale = min(1.5, 2000 / max(1, page.rect.width, page.rect.height))
                name = f"image-{offset + len(names) + 1}.png"
                page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(folder / name)
                names.append(name)
        print(json.dumps({"images": names}))
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
