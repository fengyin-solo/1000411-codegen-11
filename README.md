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
| 药剂出入 | `chemical` | 药剂单据 | 单据编号、药剂名称、规格型号 |
| 能耗管理 | `energy` | 能耗记录 | 记录编号、统计日期、用电量 |
| 报警中心 | `alarm` | 报警事件 | 报警编号、报警类型、报警等级 |
| 设备检修 | `maint` | 检修单 | 检修单号、关联设备、检修类型 |
| 受限空间作业 | `permit` | 作业许可单 | 许可编号、作业类型、作业地点 |
| 达标审核 | `audit` | 审核记录 | 审核编号、审核周期、审核范围 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 药剂出入：单据导入与结存重算

药剂出入模块支持按 CSV 批量导入单据，流程为「下载模板 → 上传差异预览 → 确认落库」：

- `GET /api/chemical/import/template`：下载带填写示例的导入模板（UTF-8 CSV，Excel 可直接打开）。
- `POST /api/chemical/import/preview`：只做解析、逐条校验与结存预演，不写数据；返回逐单结存、
  按药剂汇总的结存差异和一次性确认令牌。
- `POST /api/chemical/import/commit`：凭令牌复核后原子落库并全量重算结存，成功后把原始导入文件、
  对账汇总与对账明细归档到 `backend/var/archives/<归档号>/`；任何一步失败整批回滚，不留半成品。
- `POST /api/chemical/import/error-report`：把预览阶段拦截的问题行（行号、单据编号、原因）生成错误明细 CSV。
- `POST /api/chemical/recalc`：不增删单据，仅重放「已出入库」流水重算结存并核对一致性。
- `GET /api/chemical/archives` 与 `.../archives/{归档号}/files/{文件名}`：查看与下载历批对账归档。

校验口径：单据编号与库内或文件内重复、出入数量为负或为 0 或非数字、供应商等必填项缺失、
出入方向无法识别，都会逐条拦下并说明原因，整批不得确认落库。结存按「药剂名称 + 规格型号」分组、
按单据编号排序逐单累加（入库为正、出库为负），「已作废」单据不参与累加；审核、确认出入库、
作废等既有动作的状态流转与返回结构保持不变，动作后同样按上述口径刷新结存。
