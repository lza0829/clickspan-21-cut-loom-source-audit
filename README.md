# 点跨（Clickspan）

点击流装载 → 按访客空闲超时切会话 → 脏事件隔离 → 会话证明互证。

**不是** 运单轨迹时间线（途迹）、变更投影流（changecast）、CDC Sink、券核销钉扎、日结封账、双边对齐、改价价签、证据包放行。  
去皮后是：**事件装载 + 会话窗物化 + 闸/隔离 + 证明**。

## 启动

```powershell
# 后端
cd backend
pip install -r requirements.txt
$env:DATA_DIR = "$PWD\data"
$env:FIXTURES_DIR = "$PWD\..\fixtures"
uvicorn app.main:app --host 127.0.0.1 --port 10900

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5900 |
| 接口 | http://127.0.0.1:10900 |

## UI

**泳道织机**：顶栏品牌 + 顶栏菜单（织机 / 收流 / 搁置 / 核证 / 切窗）+ 横向会话串珠。  
不用底栏导航、不用三栏测绘板、不用左轨编号、不用流程 ribbon。

## 织机源审计与珠串分段

- 持久化形态：**独立织机源审计表** `loom_source_audit`（切边键唯一，一条切边一行）。
- 提交形态：**切窗成功同事务重挂源审计并刷新分段投影，缺一回滚**——审计行由
  收流的 `BEGIN IMMEDIATE` 事务写入，写不出即整批回滚，并有提交前对撞兜底。
- 顶栏「当前串珠源」只取与切边同源的审计行（`/api/overview` 的
  `loom_source_session_id`），审计被篡改时退回切边的新承接会话，绝不回到切前 id。
- 珠串按会话分段返回（`/api/strands`、`/api/sessions/{id}`），跨切边混段时接口
  返回 409 而非成功。
- 核证只读接口（基线所无）：`GET /api/loom-source-audit`，逐行对撞源审计 ×
  切边 × 会话成员物化（含两侧成员哈希），分叉即 `ok=false` 并给出
  `divergences`；核证页「织机源审计核证」展示同一结果。

