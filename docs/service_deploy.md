# QuantLab 服务化部署指南（阶段 2 · 手把手）

> 2026-09-10 · 服务代码 `scripts/api_server.py` 已在本机端到端验证通过
> 目标：合作者通过浏览器/HTTP 只读访问数据湖、因子清单和报告；写任务仍集中在流水线

## 架构一览

```
合作者浏览器 / HTTP 客户端
        │  (Tailscale 内网 / 局域网)
        ▼
FastAPI 只读网关 scripts/api_server.py
  ├─ GET  /api/health     探活+数据新鲜度（免鉴权）
  ├─ GET  /api/universe   股票池规模
  ├─ POST /api/query      SQL 只读查询（SELECT/WITH 白名单，三层降级）
  ├─ GET  /api/factors    因子清单（88 个注册因子）
  └─ /reports/*           reports/ 静态报告挂载
        ▼
quant.duckdb（只读连接，写锁被占自动降级 Parquet 镜像）+ data/lake/
```

关键设计：**网关只读**。复用 `store.query()` 的三层降级，与每日流水线的 DuckDB
单写者完全共存——写任务在流水线，读任务走网关，互不阻塞。

## 第一步：本机先把服务跑起来（已完成 ✅）

```bash
# 1) 装依赖（quantlab venv 内，清华镜像）
$PY -m pip install fastapi "uvicorn[standard]" -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2) 设 Token 并启动（Windows PowerShell 示例）
$env:QUANTLAB_API_TOKEN="换一个长随机串"
$PY scripts/api_server.py --port 8000

# 3) 验证（本机另开终端）
curl http://127.0.0.1:8000/api/health
curl -H "Authorization: Bearer <TOKEN>" "http://127.0.0.1:8000/api/universe"
curl -H "Authorization: Bearer <TOKEN>" -X POST http://127.0.0.1:8000/api/query ^
     -H "Content-Type: application/json" ^
     -d "{\"sql\":\"SELECT COUNT(*) n FROM kline_daily\"}"
# 浏览器直接看报告：
#   http://127.0.0.1:8000/reports/overview_report.html
```

本机实测记录（2026-09-10）：health 返回 kline_daily 586 万行/最新 2026-09-09；
/api/query 对因子湖直读 Parquet 正常；DELETE/多语句注入均被 400 拦截；
无 Token 请求返回 401；/reports 正常出 HTML。

## 第二步：让合作者访问到（三选一）

| 方式 | 做法 | 适用 |
|---|---|---|
| Tailscale 组网（推荐） | 服务器与本机都装 Tailscale，服务启动 `--host 0.0.0.0`，合作者访问 `http://<tailscale-ip>:8000` | 异地团队，零公网暴露 |
| 局域网直连 | `--host 0.0.0.0`，合作者访问 `http://<内网IP>:8000` | 同办公室 |
| 云服务器 | 见第三步，公网 IP + 安全组只放行 8000/Tailscale 端口 | 7×24 在线需求 |

> 安全三件套：Token 鉴权（已内置）+ 只读白名单（已内置）+ 不暴露公网
> （Tailscale 或安全组 IP 白名单）。`/api/health` 刻意免鉴权仅暴露数据新鲜度。

## 第三步：迁到正式服务器（Linux 示例）

```bash
# 1) 环境就绪（参考 docs/collab_setup.md 完成数据迁移）
git clone https://github.com/standingflowerNoah/quantlab.git ~/quantlab
cd ~/quantlab
python3.13 -m venv envs/quantlab
envs/quantlab/bin/pip install -r requirements.txt fastapi "uvicorn[standard]" \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2) systemd 常驻服务 /etc/systemd/system/quantlab-api.service
[Unit]
Description=QuantLab Research API
After=network.target
[Service]
User=quantlab
WorkingDirectory=/home/quantlab/quantlab
Environment=QUANTLAB_API_TOKEN=<TOKEN>
ExecStart=/home/quantlab/quantlab/envs/quantlab/bin/python scripts/api_server.py --host 0.0.0.0 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target

# 3) 启动与自启
sudo systemctl daemon-reload
sudo systemctl enable --now quantlab-api
curl http://127.0.0.1:8000/api/health
```

注意：Linux 服务器上 pytdx/东财/新浪源直接可用；fsdb 分钟引擎是 Windows exe，
需要单独一台 Windows 采集机（stockdb.exe 绑内网 IP，改 fsdb_source.py 一行地址），
或保留 Windows 服务器方案——详见阶段 1 的选型结论。

## 第四步：研究员工作台（可选加分项）

```bash
# JupyterLab（每人独立 venv，只读共享数据湖）
envs/quantlab/bin/pip install jupyterlab -i https://pypi.tuna.tsinghua.edu.cn/simple
envs/quantlab/bin/jupyter lab --generate-config   # 设置密码/令牌
envs/quantlab/bin/jupyter lab --ip 0.0.0.0 --port 8888 --no-browser
# 或 VS Code Server（浏览器里获得完整 IDE）
curl -fsSL https://code-server.dev/install.sh | sh
```

研究员分析脚本遇写锁的正确姿势（已有惯例）：in-memory `duckdb.connect()` +
glob 直读 `data/lake/factor/*/part-*.parquet`，参考 `scripts/highfreq_neutral.py`。

## 第五步：日常运维

| 事项 | 做法 |
|---|---|
| 报告更新 | 流水线照常每日生成 reports/*.html，网关静态挂载即时生效，无需重启 |
| 网关升级 | git pull 后 `sudo systemctl restart quantlab-api` |
| 监控 | 定时打 /api/health，kline_daily.latest 停在昨日 → 流水线出问题 |
| Token 轮换 | 改环境变量 + restart；合作者统一通知换新 |
| 加端点 | 在 api_server.py 加只读端点，PR 合入 main，服务器 pull+restart |

## 端点清单（当前版本 v0.1.0）

| 端点 | 方法 | 鉴权 | 说明 |
|---|---|---|---|
| `/api/health` | GET | 免 | 探活 + kline_daily 行数/最新日期 + 因子域数量 |
| `/api/universe` | GET | Bearer | 全部股票池及规模 |
| `/api/query` | POST | Bearer | body `{"sql": "..."}`，仅 SELECT/WITH 单语句，自动镜像降级 |
| `/api/factors` | GET | Bearer | 88 个注册因子（name/category/description） |
| `/reports/{file}` | GET | ⚠️ 免 | reports/ 静态挂载，浏览器直接打开；**无 Header 鉴权**，依赖内网边界（Tailscale/局域网）保护，勿直接暴露公网 |
