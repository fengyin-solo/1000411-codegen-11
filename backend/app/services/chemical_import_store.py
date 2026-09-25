"""药剂单据导入的暂存与归档存储。

预览不落库到业务表，但批次内容（原始文件、校验结果、差异快照）要写到磁盘：
- 用户关掉页面重新进入后，可以按批次号找回上一次的差异预览；
- 确认落库后，原始导入文件与对账文件一起归档，随时可以下载核对。

只依赖标准库 pathlib + json，跟内存仓库保持同一套技术口径。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class ImportArchiveStore:
    """管理导入批次目录与归档目录，目录结构见 ARCHIVE_ROOT。"""

    def __init__(self, root: Path | None = None) -> None:
        # 默认放在 backend/var/chemical_import 下；测试可通过环境变量指定临时目录
        base = root or Path(os.environ.get("CHEMICAL_IMPORT_ROOT")
                            or Path(__file__).resolve().parents[2] / "var" / "chemical_import")
        self.batch_root = base / "batches"
        self.archive_root = base / "archives"
        self.batch_root.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)

    # ---- 批次（预览暂存） -------------------------------------------------

    def new_batch_id(self) -> str:
        return uuid.uuid4().hex

    def _batch_dir(self, batch_id: str) -> Path:
        return self.batch_root / batch_id

    def save_batch(self, batch: dict[str, Any]) -> Path:
        batch_id = str(batch["batch_id"])
        directory = self._batch_dir(batch_id)
        directory.mkdir(parents=True, exist_ok=True)
        # 原始导入文件原样留底，重新进入时还能拿到上传内容
        raw = batch.get("raw_content")
        if raw is not None:
            (directory / "source.csv").write_text(raw, encoding="utf-8-sig")
        payload = {key: value for key, value in batch.items() if key != "raw_content"}
        (directory / "batch.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return directory

    def load_batch(self, batch_id: str) -> dict[str, Any] | None:
        directory = self._batch_dir(batch_id)
        meta_file = directory / "batch.json"
        if not meta_file.exists():
            return None
        batch = json.loads(meta_file.read_text(encoding="utf-8"))
        source = directory / "source.csv"
        if source.exists():
            batch["raw_content"] = source.read_text(encoding="utf-8-sig")
        return batch

    def discard_batch(self, batch_id: str) -> None:
        directory = self._batch_dir(batch_id)
        if directory.exists():
            for file in directory.iterdir():
                file.unlink()
            directory.rmdir()

    # ---- 归档（确认落库后） -----------------------------------------------

    def archive_batch(
        self,
        *,
        batch: dict[str, Any],
        files: dict[str, str],
        result: dict[str, Any],
    ) -> str:
        """把原始导入文件、对账文件与落库结果写进归档目录，返回归档编号。"""
        archive_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = self.archive_root / archive_id
        directory.mkdir(parents=True)
        for filename, content in files.items():
            (directory / filename).write_text(content, encoding="utf-8-sig")
        manifest = {
            "archive_id": archive_id,
            "batch_id": batch["batch_id"],
            "source_filename": batch.get("source_filename"),
            "committed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_rows": result["total_rows"],
            "accepted_rows": result["accepted_rows"],
            "in_total": result["in_total"],
            "out_total": result["out_total"],
            "affected_chemicals": result["affected_chemicals"],
            "files": sorted(files),
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return archive_id

    def list_archives(self) -> list[dict[str, Any]]:
        """读取全部归档清单，按归档时间倒序，供页面查看历史导入。"""
        items: list[dict[str, Any]] = []
        if not self.archive_root.exists():
            return items
        for manifest_file in self.archive_root.glob("*/manifest.json"):
            try:
                items.append(json.loads(manifest_file.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        items.sort(key=lambda item: str(item.get("committed_at", "")), reverse=True)
        return items

    def get_archive_file(self, archive_id: str, filename: str) -> Path | None:
        # 文件名只允许取归档目录内的相对文件名，杜绝目录穿越
        path = (self.archive_root / archive_id / filename).resolve()
        try:
            path.relative_to((self.archive_root / archive_id).resolve())
        except ValueError:
            return None
        return path if path.is_file() else None
