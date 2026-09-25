"""药剂出入接口：维护药剂单据，覆盖审核单据、确认出入库、作废单据等动作。"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.chemical import ChemicalService
from app.services.chemical_import import (
    ImportRejected,
    import_service,
)

router = APIRouter(prefix="/api/chemical", tags=["药剂出入"])

service = ChemicalService()

LIST_FIELDS = ["单据编号", "药剂名称", "规格型号", "出入数量", "结存数量", "供应商", "经办人员", "单据状态"]
STATUSES = ["待审核", "已审核", "已出入库", "已作废"]


class ImportUploadPayload(BaseModel):
    """导入文件内容：前端读 CSV 文本后整体提交，避免引入额外的文件上传依赖。"""

    filename: str = Field(default="药剂单据导入.csv", description="原始导入文件名")
    content: str = Field(description="CSV 文件文本内容（首行需为模板表头）")


def _csv_response(filename: str, content: str) -> Response:
    # 中文文件名按 RFC 5987 编码，Excel 打开 utf-8-sig 不乱码
    quoted = quote(filename)
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


# 注意：以下具体路径必须声明在 /{entry_id} 之前，否则会被整数动态路由提前匹配。


@router.get("/import/template")
def download_template() -> Response:
    """下载药剂单据导入模板：表头固定、附带两行入/出库示例。"""
    return _csv_response("药剂单据导入模板.csv", import_service.template_csv())


@router.post("/import/preview")
def preview_import(payload: ImportUploadPayload) -> dict[str, Any]:
    """按单据编号导入出入数量并模拟结存重算：只给差异预览与错误清单，不落库。"""
    try:
        return import_service.preview(filename=payload.filename, content=payload.content)
    except ImportRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/import/{batch_id}")
def get_import_preview(batch_id: str) -> dict[str, Any]:
    """按批次号取回上一次的差异预览：用户重新进入页面时无需重新上传。"""
    try:
        return import_service.get_preview(batch_id)
    except ImportRejected as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/import/{batch_id}/commit")
def commit_import(batch_id: str) -> dict[str, Any]:
    """确认差异预览后落库：追加单据、回写结存、归档原始与对账文件，整体可回滚。"""
    try:
        return import_service.commit(batch_id)
    except ImportRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/import/{batch_id}/errors")
def download_import_errors(batch_id: str) -> Response:
    """生成错误明细清单（CSV）：逐条给出文件行号、单据编号与拦截原因。"""
    try:
        batch = import_service.archives.load_batch(batch_id)
    except OSError:
        batch = None
    if batch is None:
        raise HTTPException(status_code=404, detail=f"导入批次 {batch_id} 不存在或已清理")
    filename = f"导入错误明细_{batch_id[:8]}.csv"
    return _csv_response(filename, import_service.errors_csv(batch))


@router.get("/archives")
def list_archives() -> dict[str, Any]:
    """查看已归档的导入批次：确认落库时原始导入文件与对账文件一并归档。"""
    items = import_service.archives.list_archives()
    return {"total": len(items), "items": items}


@router.get("/archives/{archive_id}/files/{filename}")
def download_archive_file(archive_id: str, filename: str) -> Response:
    """下载归档目录里的原始导入文件或对账文件。"""
    path = import_service.archives.get_archive_file(archive_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="归档文件不存在")
    return _csv_response(path.name, path.read_text(encoding="utf-8-sig"))


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出药剂出入清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "chemical", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按单据编号检索"),
    status: str | None = Query(default=None, description="待审核、已审核、已出入库、已作废"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按单据编号与状态过滤药剂出入列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条药剂单据明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"药剂单据 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条药剂单据，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="药剂单据已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条药剂单据执行审核单据、确认出入库、作废单据；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
