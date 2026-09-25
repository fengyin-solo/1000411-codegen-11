"""药剂导入端到端校验脚本：临时归档目录 + 独立内存仓库，跑完即弃。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

tmp = Path(tempfile.mkdtemp(prefix="chemical-import-test-"))
os.environ["CHEMICAL_IMPORT_ROOT"] = str(tmp)

from fastapi.testclient import TestClient

from app.main import app
from app.services.chemical import MODULE
from app.store import store

client = TestClient(app)

failures = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL"), name, detail)
    if not condition:
        failures.append(name)


# 1. 模板下载
r = client.get("/api/chemical/import/template")
check("模板下载 200", r.status_code == 200)
check("模板含必填列", "单据编号" in r.text and "出入方向" in r.text and "供应商" in r.text)

# 2. 坏文件：重复单据（库内+文件内）、数量为负、供应商缺失、方向非法、非数字
bad = """单据编号,药剂名称,规格型号,出入方向,出入数量,供应商,经办人员,发生日期
CHEM-0003,聚合氯化铝,25kg/袋,入库,50,华东供应商,张三,2026-09-20
NEW-001,聚丙烯酰胺,25kg/袋,入库,-10,华北供应商,李四,2026-09-20
NEW-002,聚丙烯酰胺,25kg/袋,出库,5,,王五,2026-09-20
NEW-003,消泡剂,20kg/桶,侧向,8,华南供应商,赵六,2026-09-20
NEW-004,消泡剂,20kg/桶,入库,abc,华南供应商,赵六,2026-09-20
NEW-005,消泡剂,20kg/桶,入库,0,华南供应商,赵六,2026-09-20
NEW-001,聚丙烯酰胺,25kg/袋,入库,12,华北供应商,李四,2026-09-21
,缺编号药剂,10kg,入库,1,供应商,钱七,2026-09-21
"""
r = client.post("/api/chemical/import/preview", json={"filename": "bad.csv", "content": bad})
check("坏文件预览 200", r.status_code == 200, str(r.status_code))
body = r.json()
check("坏文件 valid=False", body["valid"] is False)
check("错误条数为 8", body["error_count"] == 8, str(body["error_count"]))
reasons = "|".join(e["reason"] for e in body["errors"])
check("库内重复被拦", "已存在于药剂出入台账" in reasons)
check("文件内重复被拦", "导入文件中重复" in reasons)
check("数量为负被拦", "出入数量为负" in reasons)
check("供应商缺失被拦", "供应商缺失" in reasons)
check("方向非法被拦", "出入方向" in reasons and "无效" in reasons)
check("非数字被拦", "不是有效数字" in reasons)
check("数量为0被拦", "不能为 0" in reasons)
check("缺单据编号被拦", any(e["field"] == "单据编号" for e in body["errors"]))

before_rows = len(store.rows(MODULE))
# 坏批次确认必须被拒
r = client.post(f"/api/chemical/import/{body['batch_id']}/commit")
check("坏批次确认 400", r.status_code == 400)
check("坏批次未写入台账", len(store.rows(MODULE)) == before_rows)
check("坏批次未留半截结存", all(row.get("结存数量") in (10, 20, 30) for row in store.rows(MODULE)))

# 错误明细下载
r = client.get(f"/api/chemical/import/{body['batch_id']}/errors")
check("错误明细下载 200", r.status_code == 200)
check("错误明细含行号与原因", "文件行号" in r.text and "供应商缺失" in r.text)

# 缺列文件
r = client.post("/api/chemical/import/preview", json={
    "filename": "noheader.csv",
    "content": "单据编号,药剂名称\nX1,药A\n",
})
check("缺列文件 400", r.status_code == 400 and "缺少必需列" in r.json()["detail"])

# 空文件
r = client.post("/api/chemical/import/preview", json={"filename": "empty.csv", "content": "  "})
check("空文件 400", r.status_code == 400 and "为空" in r.json()["detail"])

# 3. 好文件：同药剂先入后出，校验结存滚动（与库内 CHEM-0003「药剂出入样例3」同药剂以验证既有单据回写）
good = """单据编号,药剂名称,规格型号,出入方向,出入数量,供应商,经办人员,发生日期
CHEM-IMP-001,药剂出入样例3,药剂出入样例3,入库,100,华东药剂供应站,张三,2026-09-20
CHEM-IMP-002,药剂出入样例3,药剂出入样例3,出库,25,华东药剂供应站,李四,2026-09-21
CHEM-IMP-003,次氯酸钠,20L/桶,入库,40,北方化工,王五,2026-09-22
"""
r = client.post("/api/chemical/import/preview", json={"filename": "good.csv", "content": good})
check("好文件预览 200", r.status_code == 200, str(r.status_code))
body = r.json()
check("好文件 valid=True", body["valid"] is True, str(body.get("errors")))
check("接受 3 行", body["accepted_rows"] == 3)
check("入库合计 140", body["summary"]["入库合计"] == 140, str(body["summary"]))
check("出库合计 25", body["summary"]["出库合计"] == 25)
# CHEM-0003 是该药剂既有已出入库单据（无发生日期，数量 30），按期初排在最前：30 +100 -25 => 105
pac = next(c for c in body["balance_changes"] if c["药剂名称"] == "药剂出入样例3")
check("期初结存 30", pac["期初结存"] == 30, str(pac))
check("期末结存 105", pac["期末结存"] == 105, str(pac))
check("影响既有单据 1 条", pac["影响既有单据数"] == 1)
# CHEM-0003 无日期排最前，结存维持 30（参与重算但数值不变）
check("既有单据参与重算", any(e["单据编号"] == "CHEM-0003" and e["重算前结存"] == 30 and e["重算后结存"] == 30 for e in body["affected_entries"]), str(body["affected_entries"]))
entry2 = next(e for e in body["entries"] if e["单据编号"] == "CHEM-IMP-002")
check("导入单结存滚动=105", entry2["结存数量"] == 105)
# 预览不落库
r2 = client.get("/api/chemical")
check("预览未写入台账", len(r2.json()["items"]) == 3)

# 4. 批次恢复（重新进入）
r = client.get(f"/api/chemical/import/{body['batch_id']}")
check("批次恢复 200", r.status_code == 200 and r.json()["valid"] is True)
check("恢复批次期末结存一致", any(c["期末结存"] == 105 for c in r.json()["balance_changes"]))

# 5. 确认落库
r = client.post(f"/api/chemical/import/{body['batch_id']}/commit")
check("确认落库 200", r.status_code == 200, str(r.text))
result = r.json()
check("返回已导入 3 条", result["accepted_rows"] == 3)
check("返回归档编号", bool(result["archive_id"]))

# 台账校验
items = client.get("/api/chemical", params={"size": 200}).json()["items"]
check("台账增加 3 条", len(items) == 6)
posted_pac = [x for x in items if x["药剂名称"] == "药剂出入样例3" and x["status"] == "已出入库"]
last_pac = max(posted_pac, key=lambda x: x["id"])
check("末单结存 105", last_pac["结存数量"] == 105, str(last_pac))
# 结存=明细之和
in_out = sum(x["出入数量"] if x.get("出入方向", "入库") == "入库" else -x["出入数量"] for x in posted_pac)
check("结存与明细合计一致", last_pac["结存数量"] == in_out)
# 非相关药剂的未过账单据不动
check("待审核单据结存不变", next(x for x in items if x["单据编号"] == "CHEM-0001")["结存数量"] == 10)

# 6. 重复提交幂等
again = client.post(f"/api/chemical/import/{body['batch_id']}/commit")
check("重复提交幂等", again.status_code == 200 and len(client.get('/api/chemical', params={'size': 200}).json()['items']) == 6)

# 7. 归档
archives = client.get("/api/chemical/archives").json()["items"]
check("归档列表 1 条", len(archives) == 1, str(archives))
arch = archives[0]
stamp = arch["committed_at"][:10].replace("-", "")
from urllib.parse import quote
recon = client.get(f"/api/chemical/archives/{arch['archive_id']}/files/{quote('对账汇总_'+stamp+'.csv')}")
check("对账汇总可下载", recon.status_code == 200, str(recon.status_code))
check("对账结论平衡", "结存平衡" in recon.text and "105" in recon.text)
src = client.get(f"/api/chemical/archives/{arch['archive_id']}/files/{quote('导入原始文件_'+stamp+'.csv')}")
check("原始文件已归档", src.status_code == 200 and "CHEM-IMP-001" in src.text)
detail = client.get(f"/api/chemical/archives/{arch['archive_id']}/files/{quote('对账明细_'+stamp+'.csv')}")
check("对账明细含既有与导入来源", "台账既有" in detail.text and "本次导入" in detail.text)
evil = client.get(f"/api/chemical/archives/{arch['archive_id']}/files/..%2F..%2Fbatch.json")
check("目录穿越被拦", evil.status_code == 404)

# 8. 再导入同号单据被拦
r = client.post("/api/chemical/import/preview", json={"filename": "dup.csv", "content": good})
check("已导入编号再次被拦", r.json()["valid"] is False and r.json()["error_count"] == 3)

# 8.1 二次导入补录早期单据（09-19）：其后所有单据结存都要被回写
good2 = """单据编号,药剂名称,规格型号,出入方向,出入数量,供应商,经办人员,发生日期
CHEM-IMP-020,药剂出入样例3,药剂出入样例3,入库,10,华东药剂供应站,张三,2026-09-19
"""
r = client.post("/api/chemical/import/preview", json={"filename": "good2.csv", "content": good2})
b2 = r.json()
check("二次导入预览通过", b2["valid"] is True, str(b2.get("errors")))
rewrite = {e["单据编号"]: e for e in b2["affected_entries"]}
check("上批入库单结存回写 130->140", rewrite.get("CHEM-IMP-001", {}).get("重算后结存") == 140, str(rewrite.get("CHEM-IMP-001")))
check("上批出库单结存回写 105->115", rewrite.get("CHEM-IMP-002", {}).get("重算后结存") == 115, str(rewrite.get("CHEM-IMP-002")))
check("期末结存变为 115", any(c["期末结存"] == 115 for c in b2["balance_changes"]), str(b2["balance_changes"]))
client.post(f"/api/chemical/import/{b2['batch_id']}/commit")
items = client.get("/api/chemical", params={"size": 200}).json()["items"]
posted = sorted([x for x in items if x["药剂名称"] == "药剂出入样例3" and x["status"] == "已出入库"], key=lambda x: x["id"])
# 台账 id 顺序不变：期初 30(id3)、130→140(id4)、105→115(id5)、补录新单自身结存 40(id8)
check("二次导入后结存全部对得上", [x["结存数量"] for x in posted] == [30, 140, 115, 40], str([(x['单据编号'], x['结存数量']) for x in posted]))
# 按日期滚动到最后一张的结存（id5，09-21）= 全部明细之和
detail_sum = sum(x["出入数量"] if x.get("出入方向", "入库") == "入库" else -x["出入数量"] for x in posted)
check("结存合计=明细之和 115", detail_sum == 115)
last_by_date = max(posted, key=lambda x: x.get("发生日期", ""))
check("日期末单结存=115", last_by_date["单据编号"] == "CHEM-IMP-002" and last_by_date["结存数量"] == 115)

# 9. 出库超库存 -> 警告但不阻断
over = """单据编号,药剂名称,规格型号,出入方向,出入数量,供应商,经办人员,发生日期
CHEM-IMP-010,次氯酸钠,20L/桶,出库,999,北方化工,王五,2026-09-23
"""
r = client.post("/api/chemical/import/preview", json={"filename": "over.csv", "content": over})
b = r.json()
check("超库存仍可预览", b["valid"] is True and len(b["warnings"]) == 1, str(b.get("warnings")))
client.post(f"/api/chemical/import/{b['batch_id']}/commit")
items = client.get("/api/chemical", params={"size": 200}).json()["items"]
# 自校验不阻断，结存=40-999=-959
last = [x for x in items if x["单据编号"] == "CHEM-IMP-010"][0]
check("超库存结存为负但一致", last["结存数量"] == -959, str(last))

# 10. 既有动作行为不变（审核/确认/作废照常，且未过账单据不参与结存）
r = client.post("/api/chemical/1/actions", json={"values": {"action": "审核单据"}})
check("既有审核动作不变", r.status_code == 200 and r.json()["ok"] is True)
r = client.post("/api/chemical/1/actions", json={"values": {"action": "确认出入库"}})
check("既有确认动作不变", r.json()["ok"] is True)
r = client.post("/api/chemical/2/actions", json={"values": {"action": "作废单据"}})
check("既有作废动作不变", r.json()["ok"] is True and r.json()["entry"]["status"] == "已作废")
# id=1 入库 10（药名是“药剂出入样例1”），新过账后结存为 10
row1 = client.get("/api/chemical/1").json()
check("新过账单据结存=10", row1["结存数量"] == 10)

# 11. export 路由不再被 /{entry_id} 遮蔽
r = client.get("/api/chemical/export")
check("export 正常返回清单", r.status_code == 200 and r.json()["total"] >= 6)

# 12. 落库失败整批回滚：注入自校验异常后，台账行数与每张单的结存都必须恢复
import app.services.chemical_import as ci_mod
from app.services.chemical import ChemicalService
snapshot_rows = ChemicalService().list_entries(page=1, size=200)[0]
expected = [(row["id"], row.get("结存数量"), row.get("status")) for row in snapshot_rows]
_real_assert = ci_mod.ChemicalImportService.__dict__["_assert_balances_reconcile"]

def boom(self, all_rows):
    raise ci_mod.ConsistencyError("模拟自校验失败")

ci_mod.ChemicalImportService._assert_balances_reconcile = staticmethod(boom)
rollback_csv = """单据编号,药剂名称,规格型号,出入方向,出入数量,供应商,经办人员,发生日期
CHEM-RB-001,回滚测试药剂,1kg,入库,7,回滚供应商,张三,2026-09-25
"""
r = client.post("/api/chemical/import/preview", json={"filename": "rb.csv", "content": rollback_csv})
rb_batch = r.json()["batch_id"]
r = client.post(f"/api/chemical/import/{rb_batch}/commit")
check("自校验失败返回 400", r.status_code == 400 and "已整批回滚" in r.json()["detail"], str(r.status_code))
ci_mod.ChemicalImportService._assert_balances_reconcile = staticmethod(_real_assert)
after_rows = ChemicalService().list_entries(page=1, size=200)[0]
actual = [(row["id"], row.get("结存数量"), row.get("status")) for row in after_rows]
check("回滚后台账行与结存完全一致", actual == expected)
check("回滚后找不到导入单", not any(row.get("单据编号") == "CHEM-RB-001" for row in after_rows))
# 回滚批次可重新确认（批次仍为 ready）
r = client.post(f"/api/chemical/import/{rb_batch}/commit")
check("回滚后批次可重新提交", r.status_code == 200 and r.json()["accepted_rows"] == 1)
# 重新进入：已落库批次返回 committed
r = client.get(f"/api/chemical/import/{rb_batch}")
check("重新进入批次为 committed", r.json()["status"] == "committed")

print("\n临时归档目录：", tmp)
if failures:
    print("\n失败用例：", failures)
    sys.exit(1)
print("\n全部用例通过")
