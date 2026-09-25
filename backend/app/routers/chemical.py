"""药剂出入接口：维护药剂单据，覆盖审核单据、确认出入库、作废单据，
以及单据导入（差异预览 → 确认落库 → 对账归档）与结存重算。"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import Response

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.chemical import ChemicalService
from app.services.chemical_import import import_service

router = APIRouter(prefix="/api/chemical", tags=["药剂出入"])

service = ChemicalService()

LIST_FIELDS = ["单据编号", "药剂名称", "规格型号", "出入数量", "结存数量", "供应商", "经办人员", "单据状态"]
STATUSES = ["待审核", "已审核", "已出入库", "已作废"]


class ImportPreviewPayload(BaseModel):
    """上传导入文件内容：前端按 UTF-8 读出 CSV 文本提交。"""

    content: str = Field(default="", description="CSV 导入文件全文")


class ImportCommitPayload(BaseModel):
    """确认落库：凭差异预览返回的令牌提交，并回传原始文件内容用于归档。"""

    token: str
    content: str = Field(default="", description="原始导入文件全文，随对账文件归档")
    operator: str = Field(default="", description="确认人")


class ErrorReportPayload(BaseModel):
    """错误明细清单：把预览阶段逐条拦截的问题行回传，生成可下载的 CSV。"""

    errors: list[dict[str, Any]] = Field(default_factory=list)


def _csv_response(filename: str, body: str) -> Response:
    quoted = quote(filename)
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quoted}"},
    )


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


@router.get("/import/template")
def download_template() -> Response:
    """下载药剂单据导入模板（CSV，含填写示例）。"""
    return _csv_response("药剂单据导入模板.csv", import_service.template_csv())


@router.post("/import/preview")
def preview_import(payload: ImportPreviewPayload) -> dict[str, Any]:
    """解析导入文件并给出差异预览：逐行校验、模拟重算结存，不落任何数据。"""
    return service.preview_import(payload.content)


@router.post("/import/commit", response_model=ActionResult)
def commit_import(payload: ImportCommitPayload) -> ActionResult:
    """确认落库：复核预览结果后原子写入并重算结存，成功时对账文件一并归档。"""
    result, message = service.commit_import(payload.token, payload.content, payload.operator)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.post("/import/error-report")
def download_error_report(payload: ErrorReportPayload) -> Response:
    """生成错误明细清单 CSV：每条被拦下的行号、单据编号与原因都列清楚。"""
    return _csv_response("药剂单据导入错误明细.csv", import_service.error_report_csv(payload.errors))


@router.post("/recalc", response_model=ActionResult)
def recalc_balances() -> ActionResult:
    """手动结存重算：重放有效单据流水，核对结存与明细是否对得上。"""
    result = service.recalc_balances()
    return ActionResult(ok=result["ok"], message=result["message"], entry=result)


@router.get("/archives")
def list_archives() -> dict[str, Any]:
    """列出历批导入归档的对账文件。"""
    return {"items": import_service.list_archives()}


@router.get("/archives/{archive_id}/files/{filename}")
def download_archive_file(archive_id: str, filename: str) -> Response:
    """下载某次归档里的原始导入文件或对账文件。"""
    path = import_service.archive_file_path(archive_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="归档文件不存在或已被清理")
    media_types = {
        ".json": "application/json; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
    }
    return Response(
        content=path.read_bytes(),
        media_type=media_types.get(path.suffix, "application/octet-stream"),
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(path.name)}"},
    )


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出药剂出入清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "chemical", "total": total, "items": items}


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
