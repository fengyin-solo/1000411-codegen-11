# 污水处理厂工艺管控平台

面向污水处理厂进出水监测、工艺单元、加药曝气、污泥处置与达标排放的一体化工艺管控后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 厂区单元 | `plant` | 工艺单元 | 单元编码、单元名称、处理工艺 |
| 进水监测 | `inflow` | 进水记录 | 监测编号、采样时间、进水流量 |
| 出水监测 | `effluent` | 出水记录 | 监测编号、采样时间、出水流量 |
| 曝气控制 | `aeration` | 曝气记录 | 记录编号、曝气池编号、溶解氧值 |
| 加药管理 | `dosing` | 加药单 | 加药单号、药剂名称、投加浓度 |
| 污泥处置 | `sludge` | 污泥处置单 | 处置单号、污泥来源、含水率 |
| 脱水运行 | `dewater` | 脱水记录 | 记录编号、脱水机编号、进泥量 |
| 泵站运行 | `pump` | 泵站 | 泵站编号、泵组台数、运行泵号 |
| 鼓风机组 | `blower` | 鼓风机组 | 机组编号、机组型号、额定风量 |
| 膜组件 | `membrane` | 膜组 | 膜组编号、膜型号、膜面积 |
| 在线仪表 | `online` | 在线仪表 | 仪表编号、仪表类型、测量范围 |
| 取样检测 | `sample` | 检测单 | 检测单号、取样点位、检测项目 |
| 药剂出入 | `chemical` | 药剂单据 | 单据编号、药剂名称、规格型号、出入数量、结存数量、供应商 |
| 能耗管理 | `energy` | 能耗记录 | 记录编号、统计日期、用电量 |
| 报警中心 | `alarm` | 报警事件 | 报警编号、报警类型、报警等级 |
| 设备检修 | `maint` | 检修单 | 检修单号、关联设备、检修类型 |
| 受限空间作业 | `permit` | 作业许可单 | 许可编号、作业类型、作业地点 |
| 达标审核 | `audit` | 审核记录 | 审核编号、审核周期、审核范围 |

## 药剂单据导入与结存重算

药剂出入模块支持按 CSV 批量导入出入单据，分「差异预览 → 确认落库」两段执行：

1. `GET /api/chemical/import/template` 下载导入模板（单据编号、药剂名称、规格型号、
   出入方向、出入数量、供应商、经办人员、发生日期）。
2. `POST /api/chemical/import/preview` 上传文件内容，逐行校验并模拟结存滚动：
   - 重复单据（与台账重复 / 文件内重复）、出入数量为负或非数字、供应商缺失、
     出入方向非法都会逐条拦下并说明原因，可通过
     `GET /api/chemical/import/{批次号}/errors` 下载错误明细清单（CSV）；
   - 校验通过时返回每种药剂「期初结存 → 重算后结存」的差异、既有单据回写明细与
     待导入单据的滚动结存，此阶段不写台账。批次暂存在 `backend/var/chemical_import/batches`，
     重新进入页面可用 `GET /api/chemical/import/{批次号}` 找回预览。
3. `POST /api/chemical/import/{批次号}/commit` 确认后落库：先在快照外算好全部目标值、
   再写归档文件、最后应用并做「结存=明细」自校验；任一步失败整批回滚，不会留下半算的结存。
   确认后原始导入文件、对账明细、对账汇总一并归档到
   `backend/var/chemical_import/archives`，列表与下载见
   `GET /api/chemical/archives`。

结存口径：按「药剂名称 + 规格型号」分组，仅「已出入库」单据参与，按发生日期滚动
（入库加、出库减）；出库导致负结存只告警不拦截。既有的审核单据、确认出入库、作废单据
行为不变。

后端目录下 `verify_import.py` 是该流程的端到端校验脚本（TestClient），
可用 `.venv/bin/python verify_import.py` 直接运行。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
