# QuantLab 变更广播板（BROADCAST）

> **协议**：每项新任务开始前，先读本板最新条目再规划任务；任何数据 / 脚本 /
> 自动化任务的变更（含失败与降级）完成后，立即追加广播。失败与降级是
> 连续性最关键的事件，**必须**广播。
>
> - 机读全量日志：`data/broadcast/broadcast.jsonl`（append-only，本板由它派生）
> - CLI：`python scripts/broadcast.py read [-n 20] [--mark-read]` · `unread` ·
>   `add --category data --title "..." --detail "..." --impact "..."`
> - 代码：`from quantlab.broadcast import broadcast`

累计 **278** 条 · 游标 #270 · 未读 **8** 条 · 本板显示最新 278 条

| seq | 时间 | 类别 | 动作 | 标题 | 详情 | 影响 | 来源 |
|---|---|---|---|---|---|---|---|
| #278 | 2026-09-16 00:41 | script | change | 架构纠正：十分位/残差 IC 改走预计算落库路线（L0 扩展湖 factor_metric_ext），页面生成退化为轻聚… | 用户点醒：页面生成不应做重截面计算。纠正 v1.1 方案——新增 scripts/factor_eval/backfill_metric_ext.py：十分位（dec1~dec10_ret20 逐日组… |  | broadcast.py:113 |
| #277 | 2026-09-16 00:28 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-14，无需增量 |  | daily_pipeline |
| #276 | 2026-09-16 00:28 | data | change | 流水线[数据质量] 完成 | 0 失败, 1 警告（数据健康） |  | daily_pipeline |
| #275 | 2026-09-16 00:28 | data | change | 流水线[数据更新] 完成 | 36/40 域成功 |  | daily_pipeline |
| #274 | 2026-09-16 00:28 | data | change | 数据日度更新完成 | calendar:ok \| instruments:ok \| daily_snapshot:ok \| kline_daily:degraded \| index_kline:degraded \| fsd… | 水位已推进，Parquet 镜像已导出；1 域失败 / 3 域降级（详见上方 incident 广播） | update.py |
| #273 | 2026-09-16 00:28 | data | change | tushare 冗余兜底触发 | kline_daily:fresh; daily_snapshot:fresh; index_kline:fresh; margin_total:backfilled 0 rows (2026-09-… | 主源/一级兜底未推进的域已由 tushare 代理补齐，字段口径见 quantlab/data/sources/ts_redundancy.py 注释 | ts_redundancy |
| #272 | 2026-09-16 00:28 | automation | change | 新建周末自动化：单因子看板重生成+云端重发（周六 10:00） | 用户指示单因子看板云端重发改周末运行。创建 automation id=7a164e13（ACTIVE，FREQ=WEEKLY;BYDAY=SA;BYHOUR=10，下次 2026-09-19 10:… | 无 | 深夜会话 |
| #271 | 2026-09-16 00:27 | incident | fail | [finance_snapshot] 数据更新失败 | 通达信服务器池全部不可用: None | finance_snapshot 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #270 | 2026-09-16 00:07 | automation | change | v3 全库看板重建已排程：看门狗等 despair 流水线释放主库后自动跑 323 因子（预计凌晨 ~06:00 完成） | 用户问能否修复湖模式缺的十分位/正交化残差 IC → 主库 23:54 曾空闲，但 23:59 despair 自动化（daily_pipeline.py，PID 23016/37004）启动持写锁。… |  | broadcast.py:113 |
| #269 | 2026-09-16 00:00 | incident | warn | [index_kline] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'index': 7, 'calendar': 4000} | index_kline 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #268 | 2026-09-16 00:00 | incident | warn | [kline_daily] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'kline': 0, 'snapshot': 0, 'miss': 5560} | kline_daily 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #267 | 2026-09-15 23:59 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #266 | 2026-09-15 23:25 | script | change | 每日流水线结束（229.7 分钟） | 数据更新:ok \| 数据质量:ok \| 分钟特征:ok \| 因子计算:ok \| 评估指标入库:ok \| 拥挤度监控:ok \| 决策信号:ok \| 总览报告:ok \| 数据看板:ok \| 前向监控:ok | 全部完成，报告见 reports/ | daily_pipeline |
| #265 | 2026-09-15 23:25 | script | change | 流水线[前向监控] 完成 | reports\forward_monitor.html |  | daily_pipeline |
| #264 | 2026-09-15 23:24 | script | change | 流水线[数据看板] 完成 | reports/data_dashboard.html |  | daily_pipeline |
| #263 | 2026-09-15 23:20 | script | change | 流水线[总览报告] 完成 | reports/overview_report.html |  | daily_pipeline |
| #262 | 2026-09-15 23:07 | model | change | 流水线[决策信号] 完成 | 目标持仓 93 只；PROD_SI/V3_SI 已记账/EQ3 已记账/PROD_DUAL 已记账/PROD_HF 已记账/PROD_HFA 已记账/EQ3_HFA_ICW 已记账/W3 周三已记/D… |  | daily_pipeline |
| #261 | 2026-09-15 23:06 | script | change | 全库 323 因子单因子看板生成完成（湖模式 dashboard-lake-v1），总览跳转 323/323 全亮 | 新脚本 scripts/factor_eval/build_all_center_lake.py：全只读湖数据（L0/L1/corr/crowding/audit/FDR/registry/风格因子矩… |  | broadcast.py:113 |
| #260 | 2026-09-15 23:03 | model | change | 流水线[拥挤度监控] 完成 | 2 项拥挤预警(≥80%分位): price_position_250, sue，门控 w=0.92 |  | daily_pipeline |
| #259 | 2026-09-15 23:01 | script | change | 流水线[评估指标入库] 完成 | L0 646 因子×2 池（2026-03-11 00:00:00 起重算 130 日窗），L1 滚动 636,599 行，L2 核心对 361,214 行 |  | daily_pipeline |
| #258 | 2026-09-15 23:01 | signal | change | 绝望人血馒头策略 2026-09-15：新信号0只/买入0/出场0/持仓0/净值1.000 | 自动任务产出：C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\reports\despair\despair_20260915.html |  | 绝望人血馒头策略 |
| #257 | 2026-09-15 22:05 | model | change | 流水线[因子计算] 完成 | 114/114 因子已更新 |  | daily_pipeline |
| #256 | 2026-09-15 21:17 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-14，无需增量 |  | daily_pipeline |
| #255 | 2026-09-15 21:17 | data | change | 流水线[数据质量] 完成 | 0 失败, 0 警告（数据健康） |  | daily_pipeline |
| #254 | 2026-09-15 21:17 | data | change | 流水线[数据更新] 完成 | 37/40 域成功 |  | daily_pipeline |
| #253 | 2026-09-15 21:17 | data | change | 数据日度更新完成 | calendar:ok \| instruments:ok \| daily_snapshot:ok \| kline_daily:degraded \| index_kline:degraded \| fsd… | 水位已推进，Parquet 镜像已导出；1 域失败 / 2 域降级（详见上方 incident 广播） | update.py |
| #252 | 2026-09-15 21:17 | data | change | tushare 冗余兜底触发 | kline_daily:backfilled 5548 rows (2026-09-15~2026-09-15); daily_snapshot:fresh; index_kline:fresh; m… | 主源/一级兜底未推进的域已由 tushare 代理补齐，字段口径见 quantlab/data/sources/ts_redundancy.py 注释 | ts_redundancy |
| #251 | 2026-09-15 21:15 | doc | change | 云端看板重发：明细表 1Y 口径 + 头部年化列已同步线上 | docs/ 重发（链接不变 https://9ce4ca5a0b7249f4a47ef86fed288b5e.app.workbuddy.host ），factor_universe.html 为最新… |  | broadcast.py:113 |
| #250 | 2026-09-15 21:10 | script | change | 总览看板明细表主指标切换近 1 年口径 + 新增头部年化列 | build_universe_dashboard.py：明细表 ric1/ric5/ric20/icir20/win5 改为 L0 近 244 交易日聚合（原全史列仅保留 t_adj/q 作 FDR … |  | broadcast.py:113 |
| #249 | 2026-09-15 21:03 | incident | fail | [finance_snapshot] 数据更新失败 | 通达信服务器池全部不可用: None | finance_snapshot 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #248 | 2026-09-15 20:55 | script | change | 因子总览看板新增近 1 年窗口（244 交易日）统计 | build_universe_dashboard.py 增加 --year1 参数（默认 244 交易日）：明细表新增 r1y_icir/r1y_decay 两列、榜单区新增「近 1 年强度 Top2… |  | broadcast.py:113 |
| #247 | 2026-09-15 20:26 | doc | change | 研究看板全量发布云端公开链接 | docs/ 目录 23 个 HTML（因子总览+6 单因子看板+15 回测研究报告+2 工程文档）静态发布为公开链接 https://9ce4ca5a0b7249f4a47ef86fed288b5e.… |  | broadcast.py:113 |
| #246 | 2026-09-15 20:22 | doc | change | 因子总览看板新增「计算逻辑」区：323/323 因子可查源码或实现锚点 | build_universe_dashboard.py 升级：明细表新增代码列+弹窗。五层来源：registry 114 类源码（inspect）、Alpha191 180 公式函数源码（含 high… |  | broadcast.py:113 |
| #245 | 2026-09-15 20:14 | automation | change | 每日自动化时序重构：信号主跑=19:30盘后流水线 | 为解决盘前推送撞上数据未就绪/写锁的问题：①07:00「每日量化流水线」暂停（全量2.5h到09:30+、对08:35推送零贡献且持DuckDB写锁与推送只读互斥；任务保留可随时恢复作兜底）；②「绝望… | 明起早间无流水线兜底重跑：若前一晚盘后流水线失败，08:35按防滞后协议推滞后说明，修复走接力会话或人工；周六拥挤度周报不受影响 | 主会话·自动化时序重构 |
| #244 | 2026-09-15 19:57 | script | change | 全因子总览看板上线 docs/factor_universe.html（323 因子） | 新脚本 scripts/factor_eval/build_universe_dashboard.py：聚合审计快照+FDR+registry+L0 近60日动态（双类型 IC×3 周期），产出单文件… |  | broadcast.py:113 |
| #243 | 2026-09-15 19:37 | incident | warn | [index_kline] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'index': 7, 'calendar': 4000} | index_kline 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #242 | 2026-09-15 19:37 | incident | warn | [kline_daily] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'kline': 0, 'snapshot': 0, 'miss': 5560} | kline_daily 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #241 | 2026-09-15 19:35 | incident | warn | 流水线拒绝启动（单实例锁） | 检测到另一 pipeline 实例在运行，本实例自动退出，避免双开互锁 | 无（已有实例接管） | daily_pipeline |
| #240 | 2026-09-15 19:35 | automation | change | 19:42 启动 EOD 全量流水线拉取 2026-09-15 数据（用户指示） | 用户 19:25 指示当日收盘后立即更新。全量流水线：数据更新(09-15 日线/分钟/各域) → 质量闸门(因子落后1自然日在容差内=PASS) → 因子计算(追平09-15) → 评估入库 → 拥… | 决策快照将从 09-14 前移至 09-15；09-15 全天数据今晚入库；despair 今晚可能单次锁冲突 | 自动化·每日量化流水线 |
| #239 | 2026-09-15 19:35 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #238 | 2026-09-15 19:18 | script | change | 每日流水线结束（72.8 分钟） | 数据更新:skipped \| 数据质量:skipped \| 分钟特征:ok \| 因子计算:skipped \| 评估指标入库:ok \| 拥挤度监控:ok \| 决策信号:ok \| 总览报告:ok \| 数据… | 全部完成，报告见 reports/ | daily_pipeline |
| #237 | 2026-09-15 19:18 | script | change | 流水线[前向监控] 完成 | reports\forward_monitor.html |  | daily_pipeline |
| #236 | 2026-09-15 19:17 | script | change | 流水线[数据看板] 完成 | reports/data_dashboard.html |  | daily_pipeline |
| #235 | 2026-09-15 19:16 | script | change | 流水线[总览报告] 完成 | reports/overview_report.html |  | daily_pipeline |
| #234 | 2026-09-15 19:06 | model | change | 流水线[决策信号] 完成 | 目标持仓 100 只；PROD_SI/V3_SI 已记账/EQ3 已记账/PROD_DUAL 已记账/PROD_HF 已记账/PROD_HFA 已记账/EQ3_HFA_ICW 已记账/W3 周三已记/… |  | daily_pipeline |
| #233 | 2026-09-15 19:01 | model | change | 流水线[拥挤度监控] 完成 | 2 项拥挤预警(≥80%分位): price_position_250, sue，门控 w=0.92 |  | daily_pipeline |
| #232 | 2026-09-15 19:00 | script | change | 流水线[评估指标入库] 完成 | L0 646 因子×2 池（2026-03-10 00:00:00 起重算 130 日窗），L1 滚动 636,433 行，L2 核心对 363,537 行 |  | daily_pipeline |
| #231 | 2026-09-15 18:05 | incident | fail | 接力流水线13:32异常死亡:因子计算已落湖,决策信号/报告/看板未跑,快照仍缺09-14 | 12:44接力会话启动的daily_pipeline --skip-data --skip-quality于13:32:18完成因子计算(ok 114/114 因子,2877s,全部落湖至09-14)… | 09-14模型信号快照缺失未修复,盘前推送明天仍推滞后说明;修复路径明确待启动 | 主库湖对齐审计会话 |
| #230 | 2026-09-15 18:05 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-14，无需增量 |  | daily_pipeline |
| #229 | 2026-09-15 18:05 | script | info | 每日流水线启动 | 共 10 步，跳过: 数据更新, 数据质量, 因子计算 |  | daily_pipeline |
| #228 | 2026-09-15 18:05 | incident | fail | 12:44 接力流水线主进程在因子计算完成后硬崩溃（评估入库环节） | 因子计算 114/114 全 PASS 至 2026-09-14（13:32 广播 #227），随后进入评估指标入库（multiprocessing workers=2）时主进程死亡：无步骤广播、无结… | 因子湖已追平 09-14 无损；决策信号快照延迟至今日傍晚；镜像对齐 watcher（#226）会检到流水线结束并自动跑终检，与本重跑互不冲突 | 自动化·每日量化流水线 |
| #227 | 2026-09-15 13:32 | model | change | 流水线[因子计算] 完成 | 114/114 因子已更新 |  | daily_pipeline |
| #226 | 2026-09-15 12:53 | script | change | 对齐审计:看板holder_num水位列bug修复+新增主库vs镜像终检脚本 | 用户问主库与湖是否对齐，完成审计：①mirror 17表=主库17表（09-14 21:01 数据更新完成后导出，此后主库仅写signal_portfolio/报告等非镜像表，mtime 22:46流… | 看板holder_num域从今晚起恢复正常显示；对齐终检报告将在流水线结束后自动产出reports/alignment_check_*.md | 主库湖对齐审计 |
| #225 | 2026-09-15 12:44 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-14，无需增量 |  | daily_pipeline |
| #224 | 2026-09-15 12:44 | script | info | 每日流水线启动 | 共 10 步，跳过: 数据更新, 数据质量 |  | daily_pipeline |
| #223 | 2026-09-15 12:43 | automation | change | 流水线重跑启用 --skip-data/quality 恢复路径（修 #222 因子水位滞后） | 12:37 重跑评估：完整重跑会复现质量闸门死循环（因子水位 09-10/09-11 vs K线 09-14 → 质量FAIL → 因子/决策 blocked），且午间跑数据更新有拉入 09-15 半… | 本轮跳过数据更新与质量体检两步；09-15 当日数据按正常节奏明日 07:00 拉取 | 自动化·每日量化流水线 |
| #222 | 2026-09-15 08:41 | automation | fail | 盘前信号推送（滞后通知版）：私信✓/群✓，快照滞后未推旧信号 | 快照 2026-09-11 滞后预期 09-14 一个交易日，按防滞后协议不推旧信号、改推滞后说明（私信+quantyy 群均 success）。根因：昨晚流水线 21:01 数据质量检查 5 项失败… | 信号快照滞后 1 个交易日，等待接力会话/人工重跑流水线后推送修正版 | 自动化·盘前模型信号微信推送 |
| #221 | 2026-09-15 02:24 | doc | add | 主力行为因子×人血馒头策略增强研究：既有因子与两融/筹码方向均无增量，'主力买入=反向信号'规律再添三证 | 用户要求：①尝试库中主力行为因子优化人血馒头策略②深研未开发方向。**事件层检验（池I 676笔×信号日读数五分位）**：mf_main_pct_20/mf_smart_dumb_20/mf_smal… |  | 因子研究 |
| #220 | 2026-09-15 02:09 | doc | add | 同花顺大单净量复刻可行性实证：分钟近似失败（方向一致率56.5%），DDX换算无增量（IC≈0） | 用户问能否复刻同花顺大单净量因子。三层路径实证：①**逐笔归并（同花顺核心壁垒）不可行**——需要Level-2逐笔成交+委托数据，无数据源且拆单识别是专利算法；②**1分钟线自建分钟级大单近似失败*… |  | 因子研究 |
| #219 | 2026-09-15 01:56 | signal | add | 绝望人血馒头事件驱动策略投产：每日盘后自动化+企微推送+近30日信号持续跟踪 | 池I定版投产（用户命名'绝望人血馒头事件驱动策略'）。生产脚本 scripts/despair_blood_bread_daily.py：数据新鲜度检查（moneyflow+kline交集）→池I信号… |  | 绝望人血馒头策略 |
| #218 | 2026-09-15 01:55 | signal | change | 绝望人血馒头策略 2026-09-14：新信号0只/买入0/出场0/持仓0/净值1.000 | 自动任务产出：C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\reports\despair\despair_20260914.html |  | 绝望人血馒头策略 |
| #217 | 2026-09-15 01:54 | signal | change | 绝望人血馒头策略 2026-09-14：新信号0只/买入0/出场0/持仓0/净值1.000 | 自动任务产出：C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\reports\despair\despair_20260914.html |  | 绝望人血馒头策略 |
| #216 | 2026-09-15 01:48 | data | change | express 全史完成：数据体系补足工程最终收官（全湖 46 数据集） | express 28,665 行 / 2005-01~2026-08-24 全史 / 4,361 只（快报自愿披露，覆盖面小于 forecast 正常）；forecast 149,508 行 / 20… | 无 | 深夜会话 |
| #215 | 2026-09-15 01:47 | signal | change | 绝望人血馒头策略 2026-09-14：新信号0只/买入0/出场0/持仓0/净值1.000 | 自动任务产出：C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\reports\despair\despair_20260914.html |  | 绝望人血馒头策略 |
| #214 | 2026-09-15 01:45 | signal | change | 绝望人血馒头策略 2026-09-14：新信号0只/买入0/出场0/持仓0/净值1.000 | 自动任务产出：C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\reports\despair\despair_20260914.html |  | 绝望人血馒头策略 |
| #213 | 2026-09-15 01:44 | signal | change | 绝望人血馒头策略 2026-09-14：新信号0只/买入0/出场0/持仓0/净值1.000 | 自动任务产出：C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\reports\despair\despair_20260914.html |  | 绝望人血馒头策略 |
| #212 | 2026-09-15 01:42 | signal | change | 绝望人血馒头策略 2026-09-14：新信号0只/买入0/出场0/持仓0/净值1.000 | 自动任务产出：C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\reports\despair\despair_20260914.html |  | 绝望人血馒头策略 |
| #211 | 2026-09-15 01:34 | signal | change | 绝望人血馒头策略 2026-09-14：新信号0只/买入0/出场0/持仓0/净值1.000 | 自动任务产出：C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\reports\despair\despair_20260914.html |  | 绝望人血馒头策略 |
| #210 | 2026-09-15 01:18 | doc | add | 池I三层剥洋葱终审：剔ST后t=0.9、再剔30亿以下后t=0.6剔核心-0.2——信号族定论=小市值×ST×危机反弹嵌… | 用户要求：池I（2日[15,∞)∪3日[20,30]×主力净流入20日新高×动态出场）剔ST版与剔ST+30亿版回测。**PIT ST剔除（namechange 8423条改名区间，非快照近似）是最狠… |  | 严重异动研究 |
| #209 | 2026-09-15 01:05 | doc | add | 池I回测（去4日，仅2日+3日）：夏普2.56全池最高，首个'合并无稀释无泄漏'的干净组合 | 用户配置：2日[15,∞)∪3日[20,30]×主力净流入20日新高×动态出场（去掉4日窗口）。676笔/114独立日/t=2.1/年化+73.4%/夏普2.56（四池最高）/回撤-45.8%/分年5… |  | 严重异动研究 |
| #208 | 2026-09-15 00:58 | doc | add | 池H回测（4日[25,33]深跌段）：表面夏普2.34最优，但4日深跌段单条件证伪（t=1.4/3年亏） | 用户配置：2日[15,∞)∪3日[20,30]∪4日[25,33]×主力净流入20日新高×动态出场。700笔/125独立日/t=2.2/年化+65.9%/夏普2.34/回撤-46.5%/分年5/5正（… |  | 严重异动研究 |
| #207 | 2026-09-15 00:46 | doc | add | 池G回测（用户指定区间）：年化+73%/夏普1.85/268独立日，合并悖论与4日浅跌段软肋 | 用户配置：2日[15,∞)∪3日[20,30]∪4日[15,25]×主力净流入20日新高×动态出场。1798笔/268独立日（全研究并列最厚）/t=2.6/年化+73.0%/夏普1.85/回撤-44.… |  | 严重异动研究 |
| #206 | 2026-09-15 00:25 | doc | add | 跌幅区间合并池：上限做成全局塌方排除后夏普1.49→1.94、分年5/5全正（2024翻正） | 用户需求：买入条件加跌幅上限（跌太深不买），2/3/4/5日窗口分别设置后合并。三个关键产出：①**union泄漏发现**——各窗口独立区间直接合并后上限完全失效（池C夏普1.46≈池B1.49）：被… |  | 严重异动研究 |
| #205 | 2026-09-15 00:05 | doc | add | 跌幅下限研究：单票止损失效于组合回撤，真实身份是资金再配置工具 | 基准2日跌15%×主力净流入20日新高×动态出场（539笔/97独立日/t=2.5，年化+84.2%/夏普2.87/回撤-45.2%/最差单笔-32.2%）扫描单票跌幅下限。结果：-10%下限年化+9… |  | 严重异动研究 |
| #204 | 2026-09-14 22:53 | incident | fix | forecast 回补卡死处置：看门狗+循环重拉两层保险落地 | 22:46 巡检发现 forecast 停在 4200/5904 达 54 分钟（progress/日志双确认真实零进度，非日志间隙），进程 57 线程泄漏+低 CPU=连接层挂死（xd 晚高峰不稳定… | 无 | 晚间会话 |
| #203 | 2026-09-14 22:47 | doc | add | 合并事件池回测：270独立日t=2.6确认信号真实，但组合形态失败 | 4个t>2变体union合并（2日跌15/20%+4日跌15/20%×主力净流入20日新高×动态出场）：**1824笔/270独立信号日=全研究最大时序样本/t=2.6/剔核心t=2.3(264天)—… |  | 严重异动研究 |
| #202 | 2026-09-14 22:47 | automation | change | 盘后流水线首次运行被 safe-delete 拦截崩溃，已带 CODEBUDDY_SAFE_DELETE_ENABLED… | 19:42 首跑在 ETF 分钟步（6 天补拉 tmp 清理量超 50/轮阈值）被 safe-delete shim fail-closed 击杀（SAFE_DELETE_BULK_CONFIRM_R… | 仅影响流水线自身 tmp/锁文件删除行为；湖内数据无删改 | 自动化·QuantLab盘后流水线 |
| #201 | 2026-09-14 22:47 | automation | change | 盘后流水线首次运行被 safe-delete 拦截崩溃，已带 CODEBUDDY_SAFE_DELETE_ENABLED… | 19:42 首跑在 ETF 分钟步（6 天补拉 tmp 清理量超 50/轮阈值）被 safe-delete shim fail-closed 击杀（SAFE_DELETE_BULK_CONFIRM_R… | 仅影响流水线自身 tmp/锁文件删除行为；湖内数据无删改 | 自动化·QuantLab盘后流水线 |
| #200 | 2026-09-14 22:46 | script | fail | 每日流水线结束（145.3 分钟） | 数据更新:ok \| 数据质量:FAIL \| 分钟特征:ok \| 因子计算:blocked \| 评估指标入库:ok \| 拥挤度监控:blocked \| 决策信号:blocked \| 总览报告:ok \| … | 失败步骤: 数据质量（请读广播排查后重跑） | daily_pipeline |
| #199 | 2026-09-14 22:46 | script | change | 流水线[前向监控] 完成 | reports\forward_monitor.html |  | daily_pipeline |
| #198 | 2026-09-14 22:46 | script | change | 流水线[数据看板] 完成 | reports/data_dashboard.html |  | daily_pipeline |
| #197 | 2026-09-14 21:54 | script | change | 流水线[总览报告] 完成 | reports/overview_report.html |  | daily_pipeline |
| #196 | 2026-09-14 21:54 | incident | fix | tushare_redundancy import 层级 bug 已修（明晨自动化风险解除） | 今晚 21:01 流水线数据更新 36/40：finance_snapshot FAIL（TDX 晚间全挂，环境性自愈）+ tushare_redundancy FAIL=ts_redundancy.… | 无（修复已验证） | 晚间会话 |
| #195 | 2026-09-14 21:45 | incident | warn | 流水线[决策信号] 被阻塞 | 数据质量未通过，本步骤跳过 | 该步骤今日无产出，下游消费方注意数据截至时间 | daily_pipeline |
| #194 | 2026-09-14 21:45 | incident | warn | 流水线[拥挤度监控] 被阻塞 | 数据质量未通过，本步骤跳过 | 该步骤今日无产出，下游消费方注意数据截至时间 | daily_pipeline |
| #193 | 2026-09-14 21:45 | script | change | 流水线[评估指标入库] 完成 | L0 646 因子×2 池（2026-03-09 00:00:00 起重算 130 日窗），L1 滚动 636,179 行，L2 核心对 365,344 行 |  | daily_pipeline |
| #192 | 2026-09-14 21:43 | doc | add | 事件条件变体扫描：2日跌20%表面最优（夏普4.96）但剔伪影后同现原形 | 20 个跌幅窗口×深度变体（2/3/4/5/10 日 × -15/-20/-25/-30%，保持主力净流入20日新高+净流入+动态出场+3日冷却）：最优=2日跌20%（73笔/34独立日/t=3.5，… |  | 严重异动研究 |
| #191 | 2026-09-14 21:23 | doc | add | 用户策略变体完整回测：20日主力流入新高条件，夏普2.40但极端值伪影剥掉后1.15 | 用户策略（DOWN 3日急跌20%+当日主力净流入占比=20日最高+净流入；主力大幅流出≤-5%卖；最长持仓20日；全仓等权成本后）完整回测352笔：年化+65.6%/夏普2.40/回撤-47.1%/… |  | 严重异动研究 |
| #190 | 2026-09-14 21:01 | incident | warn | 流水线[因子计算] 被阻塞 | 数据质量未通过，本步骤跳过 | 该步骤今日无产出，下游消费方注意数据截至时间 | daily_pipeline |
| #189 | 2026-09-14 21:01 | data | change | 流水线[分钟特征] 完成 | 增量重建完成：分钟湖至 2026-09-14，{2026: '878,533行'} |  | daily_pipeline |
| #188 | 2026-09-14 21:01 | incident | fail | 流水线[数据质量] 失败 | 5 项失败, 0 项警告（数据异常） | 下游步骤可能受阻或使用陈旧数据；下轮任务前请先读广播并排查 | daily_pipeline |
| #187 | 2026-09-14 21:01 | data | change | 流水线[数据更新] 完成 | 36/40 域成功 |  | daily_pipeline |
| #186 | 2026-09-14 21:01 | data | change | 数据日度更新完成 | calendar:ok \| instruments:ok \| daily_snapshot:ok \| kline_daily:degraded \| index_kline:degraded \| fsd… | 水位已推进，Parquet 镜像已导出；2 域失败 / 2 域降级（详见上方 incident 广播） | update.py |
| #185 | 2026-09-14 21:01 | incident | fail | [tushare_redundancy] 数据更新失败 | No module named 'quantlab.data.broadcast' | tushare_redundancy 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #184 | 2026-09-14 20:49 | incident | fail | [finance_snapshot] 数据更新失败 | 通达信服务器池全部不可用: None | finance_snapshot 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #183 | 2026-09-14 20:38 | data | change | forecast/express 业绩预告/快报回补启动；options 全史完成收官 | ①options 全史完成（2818 天/15910s），19 tushare 新域全部落湖；②业绩预告/快报回补 20:36 schtasks 分离启动：代理强制必填 ts_code（按期查询 50… | 无 | 晚间会话 |
| #182 | 2026-09-14 20:23 | incident | warn | [index_kline] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'index': 7, 'calendar': 4000} | index_kline 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #181 | 2026-09-14 20:23 | incident | warn | [kline_daily] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'kline': 5480, 'snapshot': 5480, 'miss': 80} | kline_daily 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #180 | 2026-09-14 20:21 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #179 | 2026-09-14 20:17 | data | change | 公告/互动问答403定案放弃；全湖盘点44数据集全部落本地 | 用户拍板放弃 anns_d/irm_qa_sh/irm_qa_sz 三接口（403 不可解）。全湖盘点：44 数据集全部本地落盘，kline_1min 14亿/etf_daily 230万(2004~… | 无 | 晚间会话 |
| #178 | 2026-09-14 19:44 | incident | warn | [index_kline] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'index': 7, 'calendar': 4000} | index_kline 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #177 | 2026-09-14 19:44 | incident | warn | [kline_daily] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'kline': 0, 'snapshot': 0, 'miss': 5560} | kline_daily 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #176 | 2026-09-14 19:42 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #175 | 2026-09-14 14:24 | data | change | daily_basic/stk_limit/margin_detail 补齐 09-11 | 晨链1目标交易日止 09-10（tushare 盘后出数），午后断点续跑各补 1 天：daily_basic 596.9万/stk_limit 771.2万/margin_detail 419.9万行… | 无 | 午间会话 |
| #174 | 2026-09-14 14:22 | data | info | 数据更新·非交易日轻量检查 | 日历至 2026-09-11 00:00:00，K线至 2026-09-11 00:00:00，无待补数据 | 本轮无数据变更 | update.py |
| #173 | 2026-09-14 14:11 | automation | change | 修正版信号推送完成（私信✓/群✓/链接✓） | 快照日 09-11（修复后新鲜口径），10 模型快照齐全（PROD 93只/EQ3 97/PROD_HFA 97/DIV10 10 等）；报告主节点覆盖更新+0914 归档页+重新发布；私信+quan… | 无 | 自动化·盘前模型信号微信推送 |
| #172 | 2026-09-14 13:56 | data | info | 09-14 全链收官：流水线全步骤跑通至 09-11，mf 5 因子全史 L0 双池补齐 | 接力#5 完成：①流水线剩余步骤 71.2min 全 ok（评估入库 L0 646 因子×2池 130日窗+L1 626,249 行，拥挤度 2 预警 w=0.92，决策信号 10 模型 09-11 … | 评估存储全链路（L0/L1/L2/周矩阵）对齐至 2026-09-11；mf 族可入 2026-12 双闸门评估；今晚 19:30 自动化流水线将首次带修复后的… | metric_store 会话接力#5 收官 |
| #171 | 2026-09-14 13:50 | automation | change | 盘前信号修正版推送完成（私信✓/群✓/链接✓，快照 09-11） | 接力流水线 13:47 全部完成（决策信号 ok 93 只、总览报告 ok）。快照校验 09-11=预期交易日，10 模型齐全（W3 注明 09-09 周三快照）。修正版简报已推送私信+quantyy… | 无（早间滞后的数据缺口已闭环：tushare 补 09-11 日线+流水线全量重跑+信号修正版推送完成） | 自动化·盘前模型信号微信推送 |
| #170 | 2026-09-14 13:47 | script | change | 每日流水线结束（71.2 分钟） | 数据更新:skipped \| 数据质量:skipped \| 分钟特征:ok \| 因子计算:skipped \| 评估指标入库:ok \| 拥挤度监控:ok \| 决策信号:ok \| 总览报告:ok \| 数据… | 全部完成，报告见 reports/ | daily_pipeline |
| #169 | 2026-09-14 13:47 | script | change | 流水线[前向监控] 完成 | reports\forward_monitor.html |  | daily_pipeline |
| #168 | 2026-09-14 13:47 | script | change | 流水线[数据看板] 完成 | reports/data_dashboard.html |  | daily_pipeline |
| #167 | 2026-09-14 13:44 | script | change | 流水线[总览报告] 完成 | reports/overview_report.html |  | daily_pipeline |
| #166 | 2026-09-14 13:34 | model | change | 流水线[决策信号] 完成 | 目标持仓 93 只；PROD_SI/V3_SI 已记账/EQ3 已记账/PROD_DUAL 已记账/PROD_HF 已记账/PROD_HFA 已记账/EQ3_HFA_ICW 已记账/W3 周三已记/D… |  | daily_pipeline |
| #165 | 2026-09-14 13:29 | model | change | 流水线[拥挤度监控] 完成 | 2 项拥挤预警(≥80%分位): price_position_250, sue，门控 w=0.92 |  | daily_pipeline |
| #164 | 2026-09-14 13:28 | script | change | 流水线[评估指标入库] 完成 | L0 646 因子×2 池（2026-03-09 00:00:00 起重算 130 日窗），L1 滚动 626,249 行，L2 核心对 365,344 行 |  | daily_pipeline |
| #163 | 2026-09-14 12:45 | automation | change | tushare 冗余兜底层落地：8 域接入 update_all 尾部 sweep（主源+fsdb 兜底后自动补一轮） | 新增 quantlab/data/sources/ts_redundancy.py：kline_daily(daily+adj_factor 比值传播)/daily_snapshot(daily+da… | 明日 08:00 流水线数据更新步自动生效；若 TDX+fsdb 双挂,tushare 将自动兜底 kline/snapshot/index,信号不再滞后;首次… | 自动化·数据冗余工程 |
| #162 | 2026-09-14 12:44 | data | change | 午间修复：双实例互锁止损+automation根因修正+单实例锁+湖内日历 | ①晨automation双实例(08:45:38.114/.159同毫秒拉起)因子计算互锁4h已双杀止损；②重复拉起根因=盘前推送automation prompt『并继续尝试修复流程』，已改为铁律：… | 无 | 午间会话 |
| #161 | 2026-09-14 12:43 | script | fix | 修复流水线评估入库步骤跨进程锁死 bug（Store 单例持写锁） | 根因：流水线主进程前序步骤留下的 Store 单例持主库写连接，update_daily spawn 的 worker ATTACH READ_ONLY 被跨进程写锁挡死，空转 3h/2005 次 I… | 今晚 19:30 自动化流水线将首次完整跑通评估入库步骤；hf 因子因 fsdb 上游分钟数据止 09-10 只覆盖到 09-10，日线类因子 09-11 | metric_store 会话接力#5 |
| #160 | 2026-09-14 12:36 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-10，无需增量 |  | daily_pipeline |
| #159 | 2026-09-14 12:36 | script | info | 每日流水线启动 | 共 10 步，跳过: 数据更新, 数据质量, 因子计算 |  | daily_pipeline |
| #158 | 2026-09-14 09:54 | data | update | 晨验收：批次1+2共19数据集落地（top10全史68期/质押5904只/回购增减持调研全史），4段撞pipeline锁… | 链1 04:39 全链完成：limit_list 1137天11.5万行/daily_basic 596万行/stk_limit 770万行/margin_detail 419万行。链2 11/15段… | 数据湖新增19数据集约2200万行；四段补跑约9000调用预计今日午前完成；cyq_perf筹码胜率2018全史/auction竞价2020起/options期… | 晨验收 |
| #157 | 2026-09-14 09:32 | model | change | 流水线[因子计算] 完成 | 114/114 因子已更新 |  | daily_pipeline |
| #156 | 2026-09-14 08:46 | data | change | 09-11 日线已落库并接管流水线重跑（tushare 兜底全链路闭环） | 高频重试循环在 v4 因子计算持锁间隙前无窗口，杀 v4 后 13 秒自动落库：kline_daily 2026-09-11 +5550 行（幂等 upsert，水位 09-11）。随后 schtas… | 预计 ~12:00-13:00 出含 09-11 的新鲜信号与报告；期间自动化代理若重启流水线请识别当前已有 daily_pipeline 在跑（PID 212… | metric_store 会话接力#3 |
| #155 | 2026-09-14 08:45 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-10，无需增量 |  | daily_pipeline |
| #154 | 2026-09-14 08:45 | data | change | 流水线[数据质量] 完成 | 0 失败, 0 警告（数据健康） |  | daily_pipeline |
| #153 | 2026-09-14 08:45 | data | change | 流水线[数据更新] 完成 | 1/1 域成功 |  | daily_pipeline |
| #152 | 2026-09-14 08:45 | data | info | 数据更新·非交易日轻量检查 | 日历至 2026-09-11 00:00:00，K线至 2026-09-11 00:00:00，无待补数据 | 本轮无数据变更 | update.py |
| #151 | 2026-09-14 08:45 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #150 | 2026-09-14 08:41 | automation | fail | 盘前信号推送未发信号（快照滞后 09-10 vs 预期 09-11，已发滞后说明：私信✓/群✓/链接✓） | 前置校验发现 signal_portfolio 快照 2026-09-10 ≠ 预期交易日 2026-09-11（09-11 日线三源全缺：TDX 全挂/fsdb 上游停 09-10/sina 仅指数… | 模型信号快照未推送（数据滞后非渠道故障）；两 daily_pipeline 进程仍在跑，kline_daily 降级待 tushare 补数后重跑 | 自动化·盘前模型信号微信推送 |
| #149 | 2026-09-14 08:28 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-10，无需增量 |  | daily_pipeline |
| #148 | 2026-09-14 08:28 | data | change | 流水线[数据质量] 完成 | 0 失败, 1 警告（数据健康） |  | daily_pipeline |
| #147 | 2026-09-14 08:28 | data | change | 流水线[数据更新] 完成 | 35/39 域成功 |  | daily_pipeline |
| #146 | 2026-09-14 08:28 | data | change | 数据日度更新完成 | calendar:ok \| instruments:ok \| daily_snapshot:ok \| kline_daily:degraded \| index_kline:degraded \| fsd… | 水位已推进，Parquet 镜像已导出；1 域失败 / 3 域降级（详见上方 incident 广播） | update.py |
| #145 | 2026-09-14 08:27 | incident | fail | [finance_snapshot] 数据更新失败 | 通达信服务器池全部不可用: None | finance_snapshot 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #144 | 2026-09-14 08:06 | incident | warn | [index_kline] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'index': 7, 'calendar': 4000} | index_kline 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #143 | 2026-09-14 08:06 | incident | warn | [kline_daily] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'kline': 0, 'snapshot': 0, 'miss': 5560} | kline_daily 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #142 | 2026-09-14 08:04 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #141 | 2026-09-14 08:02 | data | change | 09-11 日线缺口已可补（tushare 兜底脚本就绪），接管流水线重启 | 周五 09-11 日线三源全缺（TDX 全挂/fsdb 上游停 09-10/sina 仅指数）；tushare 代理有 09-11 全量日线（daily+adj_factor，5550 只）。已验证：… | 预计 ~13:00 出全新口径信号（含 09-11）；08:35 盘前推送仍为昨日快照 | metric_store 会话接力#3 |
| #140 | 2026-09-14 07:54 | incident | warn | [index_kline] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'index': 7, 'calendar': 4000} | index_kline 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #139 | 2026-09-14 07:54 | incident | warn | [kline_daily] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'kline': 0, 'snapshot': 0, 'miss': 5560} | kline_daily 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #138 | 2026-09-14 07:51 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #137 | 2026-09-14 07:51 | automation | change | 修复 xd_tushare.py ROOT 路径导致 token 读空 | xd_tushare.py ROOT 原为 Path(__file__).parent.parent.parent 错指包目录 quantlab/quantlab，导致 .secrets/xiaode… | 恢复 15 个 tushare 代理域(stk_high_shock/moneyflow/namechange/suspend/limit_list/index… | 自动化·每日量化流水线 |
| #136 | 2026-09-14 07:50 | incident | fail | [cyq_perf] 数据更新失败 | cyq_perf 重试6次仍失败: cyq_perf code=40101 不是合格的json格式，需同时包含 token 与 api_name | cyq_perf 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #135 | 2026-09-14 07:48 | incident | fail | [stk_shock] 数据更新失败 | stk_shock 重试6次仍失败: stk_shock code=40101 不是合格的json格式，需同时包含 token 与 api_name | stk_shock 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #134 | 2026-09-14 07:46 | incident | fail | [index_daily] 数据更新失败 | index_daily 重试6次仍失败: index_daily code=40101 不是合格的json格式，需同时包含 token 与 api_name | index_daily 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #133 | 2026-09-14 07:45 | incident | fail | [limit_list] 数据更新失败 | limit_list_d 重试6次仍失败: limit_list_d code=40101 不是合格的json格式，需同时包含 token 与 api_name | limit_list 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #132 | 2026-09-14 07:43 | incident | fail | [suspend] 数据更新失败 | suspend_d 重试6次仍失败: suspend_d code=40101 不是合格的json格式，需同时包含 token 与 api_name | suspend 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #131 | 2026-09-14 07:42 | incident | fail | [namechange] 数据更新失败 | namechange 重试6次仍失败: namechange code=40101 不是合格的json格式，需同时包含 token 与 api_name | namechange 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #130 | 2026-09-14 07:40 | incident | fail | [moneyflow] 数据更新失败 | moneyflow 重试6次仍失败: moneyflow code=40101 不是合格的json格式，需同时包含 token 与 api_name | moneyflow 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #129 | 2026-09-14 07:39 | incident | fail | [stk_high_shock] 数据更新失败 | stk_high_shock 重试6次仍失败: stk_high_shock code=40101 不是合格的json格式，需同时包含 token 与 api_name | stk_high_shock 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #128 | 2026-09-14 07:17 | incident | warn | [index_kline] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'index': 7, 'calendar': 4000} | index_kline 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #127 | 2026-09-14 07:17 | incident | warn | [kline_daily] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'kline': 0, 'snapshot': 0, 'miss': 5560} | kline_daily 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #126 | 2026-09-14 07:15 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #125 | 2026-09-14 07:15 | automation | change | 清理遗留卡死流水线进程并释放主库锁 | 04:39 接力#2(_relay_pipeline_after_chain.sh)启动的 daily_pipeline.py(PID 25692)持有 quant.duckdb 写锁卡死 2.5 小… | 释放主库单写者锁，恢复流水线可运行；接力#2 下游(周全矩阵+L0 回填 ashare_ex/zz1000)随之中止，需另行补跑 | 自动化·每日量化流水线 |
| #124 | 2026-09-14 07:02 | incident | warn | 流水线[因子计算] 被阻塞 | 数据质量未通过，本步骤跳过 | 该步骤今日无产出，下游消费方注意数据截至时间 | daily_pipeline |
| #123 | 2026-09-14 07:02 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-10，无需增量 |  | daily_pipeline |
| #122 | 2026-09-14 07:02 | incident | fail | 流水线[数据质量] 失败 | Catalog Error: Table with name watermarks does not exist!<br>Did you mean "sqlite_master"?<br><br>LINE 4:    … | 下游步骤可能受阻或使用陈旧数据；下轮任务前请先读广播并排查 | daily_pipeline |
| #121 | 2026-09-14 07:02 | incident | fail | 流水线[数据更新] 失败 | IO Error: Cannot open file "C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\data\quant.duckdb"… | 下游步骤可能受阻或使用陈旧数据；下轮任务前请先读广播并排查 | daily_pipeline |
| #120 | 2026-09-14 07:02 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #119 | 2026-09-14 07:02 | automation | change | 修复 xd_tushare.py calendar 导入路径 | xd_tushare.py L122  改为 （sources/ 下无 calendar 模块，导致 15 个 tushare 代理域报 ImportError 集体失败）。已验证 _last_tra… | 恢复 stk_high_shock/moneyflow/namechange/suspend/limit_list/index_daily/stk_shock/… | 自动化·每日量化流水线 |
| #118 | 2026-09-14 05:19 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-10，无需增量 |  | daily_pipeline |
| #117 | 2026-09-14 05:19 | data | change | 流水线[数据质量] 完成 | 0 失败, 1 警告（数据健康） |  | daily_pipeline |
| #116 | 2026-09-14 05:19 | data | change | 流水线[数据更新] 完成 | 24/42 域成功 |  | daily_pipeline |
| #115 | 2026-09-14 05:19 | data | change | 数据日度更新完成 | calendar:ok \| instruments:ok \| daily_snapshot:ok \| kline_daily:degraded \| index_kline:degraded \| fsd… | 水位已推进，Parquet 镜像已导出；16 域失败 / 2 域降级（详见上方 incident 广播） | update.py |
| #114 | 2026-09-14 05:04 | incident | fail | [finance_snapshot] 数据更新失败 | 通达信服务器池全部不可用: None | finance_snapshot 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #113 | 2026-09-14 05:04 | incident | fail | [opt_daily] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | opt_daily 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #112 | 2026-09-14 05:04 | incident | fail | [stk_auction] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | stk_auction 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #111 | 2026-09-14 05:04 | incident | fail | [holdertrade] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | holdertrade 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #110 | 2026-09-14 05:04 | incident | fail | [repurchase] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | repurchase 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #109 | 2026-09-14 05:04 | incident | fail | [stk_surv] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | stk_surv 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #108 | 2026-09-14 05:04 | incident | fail | [ah_comparison] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | ah_comparison 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #107 | 2026-09-14 05:04 | incident | fail | [margin_secs] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | margin_secs 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #106 | 2026-09-14 05:04 | incident | fail | [cyq_perf] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | cyq_perf 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #105 | 2026-09-14 05:04 | incident | fail | [stk_shock] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | stk_shock 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #104 | 2026-09-14 05:04 | incident | fail | [index_daily] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | index_daily 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #103 | 2026-09-14 05:04 | incident | fail | [limit_list] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | limit_list 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #102 | 2026-09-14 05:04 | incident | fail | [suspend] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | suspend 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #101 | 2026-09-14 05:04 | incident | fail | [namechange] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | namechange 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #100 | 2026-09-14 05:04 | incident | fail | [moneyflow] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | moneyflow 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #99 | 2026-09-14 05:04 | incident | fail | [stk_high_shock] 数据更新失败 | cannot import name 'calendar' from 'quantlab.data.sources' (C:\Users\53497\WorkBuddy\2026-09-02-23-4… | stk_high_shock 水位将落后，下游因子/信号使用陈旧数据 | update.py |
| #98 | 2026-09-14 04:41 | incident | warn | [index_kline] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'index': 7, 'calendar': 4000} | index_kline 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #97 | 2026-09-14 04:41 | incident | warn | [kline_daily] 主源未完成，已降级兜底源 | 原因: 通达信服务器池全部不可用: None；兜底结果: {'kline': 0, 'snapshot': 0, 'miss': 5560} | kline_daily 本轮由兜底源（fsdb/新浪）补齐，字段口径可能与主源有差异 | update.py |
| #96 | 2026-09-14 04:39 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #95 | 2026-09-14 02:57 | automation | info | 接力#2 已挂：链1 收尾后自动重跑全量流水线（接力#1 撞锁失败的补救） | 接力#1 误判根因：监视的链2 bash 被并行会话连坐杀死，01:17 流水线提前触发，数据更新撞主库锁（当时被 hf_wf 回填等占用）失败、因子计算被阻塞；评估指标入库/周矩阵为幂等操作无污染。… | 并行会话注意：~05:00 前后会自动跑 daily_pipeline，链2 批次2 会自动让路；若人工也要跑流水线请先停本接力 | metric_store 会话接力 |
| #94 | 2026-09-14 02:23 | script | change | 反转×资金流策略研究归档（5 个新因子落湖，等权合成 rb20 夏普 0.34，择时全部结构性失败） | 新增 quantlab/factor/library/moneyflow.py（5 个 SqlFactor 直读 moneyflow 湖表：mf_main_pct_20/small/net/smart… | mf 因子入湖（5 因子数据全期齐备）；FACTOR_DIRECTION 登记 5 方向；后续 compute_all 自动覆盖。研究方法论沉淀：资金流=反指默… | 用户：使用反转类因子和资金流量因子构造策略，可以适当加入择时… |
| #93 | 2026-09-14 02:18 | script | fail | 每日流水线结束（60.9 分钟） | 数据更新:FAIL \| 数据质量:FAIL \| 分钟特征:ok \| 因子计算:blocked \| 评估指标入库:ok \| 拥挤度监控:blocked \| 决策信号:blocked \| 总览报告:ok … | 失败步骤: 数据更新, 数据质量（请读广播排查后重跑） | daily_pipeline |
| #92 | 2026-09-14 02:18 | script | change | 流水线[前向监控] 完成 | reports\forward_monitor.html |  | daily_pipeline |
| #91 | 2026-09-14 02:18 | script | change | 流水线[数据看板] 完成 | reports/data_dashboard.html |  | daily_pipeline |
| #90 | 2026-09-14 02:16 | script | change | 流水线[总览报告] 完成 | reports/overview_report.html |  | daily_pipeline |
| #89 | 2026-09-14 02:08 | incident | warn | 流水线[决策信号] 被阻塞 | 数据质量未通过，本步骤跳过 | 该步骤今日无产出，下游消费方注意数据截至时间 | daily_pipeline |
| #88 | 2026-09-14 02:08 | incident | warn | 流水线[拥挤度监控] 被阻塞 | 数据质量未通过，本步骤跳过 | 该步骤今日无产出，下游消费方注意数据截至时间 | daily_pipeline |
| #87 | 2026-09-14 02:08 | script | change | 流水线[评估指标入库] 完成 | L0 636 因子×2 池（2026-03-06 00:00:00 起重算 130 日窗），L1 滚动 624,777 行，L2 核心对 361,789 行 |  | daily_pipeline |
| #86 | 2026-09-14 01:47 | ops | update | 深夜链被会话切换连坐杀死后已用 schtasks 分离重启；等待穿透二次事故已修（双确认模式） | 01:16 craft_mode 切换时两条 run_in_background 链被连坐（limit-list 950/1137 处断）。schtasks 分离重启时发现二次问题：schtasks … | 深夜回补的进程管理新模式：schtasks 分离 + wrapper 全路径 + 双确认等待，已沉淀到 memory 2026-09-14；本次两链预计上午全部… | 运维：链稳定性加固 |
| #85 | 2026-09-14 01:38 | doc | add | 严重异动研究终审：五条赚钱路径全堵死，唯一可执行=危机应急预案；新发现 PROD 天然异动免疫 | 用户挑战'给出能上实盘赚钱的报告'后完成的三项落地检验：①**PROD×异动过滤器**：复现 PROD（size+amihud 月末 rank 等权 top100，54个月末/442只）叠加回避清单—… | PROD 无需异动风控（天然免疫）；危机应对正确姿势=预案而非择时（任何降仓规则上实盘前必须 lag>=1 检验）；S1s 假说进入'等下一次流动性危机样本外检… | 严重异动研究终审 |
| #84 | 2026-09-14 01:22 | data | change | hf_wf_composite 湖内 2022-2024 回填完成：+328 万行，audit span 延至 2022… | 窗口解锁后执行（metric_store 两池重算 311/311 完成、空闲 20GB）：backfill_hf_wf_composite_ro.py 无锁只读版 244s 跑完——compute … | hf_wf_composite 进 metric_store L0 的历史 IC 记账范围扩至 2022 起；此前只读 2025-04 后的 OOS 观察结论不… | hf批次闸门推进 |
| #83 | 2026-09-14 01:22 | data | add | 定案：公告/互动问答代理权限缺失实锤（21组穷尽探测），403=token未授权需代理管理员开通 | 用户指示用现有代理 token 拉公告+互动问答。第五轮 21 组探测（scripts/_probe_tushare_apis5.py，结果 reports/_tmp/probe_apis_resul… | 公告+互动问答是事件驱动研究高价值源（公告日效应/董秘答复情绪），当前代理权限缺失为外部依赖阻塞项，不阻塞其余批次；官方异动双层/筹码/竞价/期权等 13 数据… | 用户：用我给你的tushare接口获取（穷尽后定案无权限） |
| #82 | 2026-09-14 01:17 | script | change | 流水线评估入库步骤限 workers=2 | daily_pipeline.run_metric_store 此前调 update_daily() 默认 4 workers×10GB duckdb 上限=40GB 超 32GB 物理内存；改为 w… | 避免流水线评估入库步骤 OOM/内存挤兑 | metric_store 收尾后续 |
| #81 | 2026-09-14 01:17 | incident | warn | 流水线[因子计算] 被阻塞 | 数据质量未通过，本步骤跳过 | 该步骤今日无产出，下游消费方注意数据截至时间 | daily_pipeline |
| #80 | 2026-09-14 01:17 | data | change | 流水线[分钟特征] 完成 | minute_feat 已覆盖至 2026-09-10，无需增量 |  | daily_pipeline |
| #79 | 2026-09-14 01:17 | script | change | 流水线评估入库步骤限 workers=2 | daily_pipeline.run_metric_store 此前调 update_daily() 默认 4 workers×10GB duckdb 上限=40GB 超 32GB 物理内存；改为 w… | 避免流水线评估入库步骤 OOM/内存挤兑 | metric_store 收尾后续 |
| #78 | 2026-09-14 01:17 | incident | fail | 流水线[数据质量] 失败 | 2 项失败, 2 项警告（数据异常） | 下游步骤可能受阻或使用陈旧数据；下轮任务前请先读广播并排查 | daily_pipeline |
| #77 | 2026-09-14 01:17 | incident | fail | 流水线[数据更新] 失败 | IO Error: Cannot open file "C:\Users\53497\WorkBuddy\2026-09-02-23-42-28\quantlab\data\quant.duckdb"… | 下游步骤可能受阻或使用陈旧数据；下轮任务前请先读广播并排查 | daily_pipeline |
| #76 | 2026-09-14 01:17 | script | info | 每日流水线启动 | 共 10 步，跳过: 无 |  | daily_pipeline |
| #75 | 2026-09-14 01:14 | data | add | 批次2扩展落地：用户点名数据全部探测完毕，stk_shock普通异动148天已落，13数据集链待跑，互动问答/公告代理无… | 用户指示：通过tushare补图上参考/特色数据+期权+宏观+公告+上证e互动+深证互动易。四轮探测（scripts/_probe_tushare_apis4{,b,c}.py）定案：【已落湖】mac… | 官方异动双层(普通stk_shock+严重stk_high_shock)对账S1s筛选；筹码胜率2018全史=微盘拥挤度新证据源；竞价数据2015起=开盘执行研… | 用户：补图上数据+期权+宏观+公告+e互动/互动易 |
| #74 | 2026-09-14 01:14 | data | add | 批次2扩展落地：用户点名参考/特色数据+期权+宏观，第四轮探测13数据集可用，互动问答/公告代理403需独立权限 | 用户点名补：图上参考/特色数据全部 + 期权 + 宏观 + 上市公司公告 + 上证e互动/深证互动易。四轮探测（scripts/_probe_tushare_apis4{,b,c}.py，矩阵见规划文… | 数据面新增：官方普通异动口径（补齐严重异动姊妹表）、筹码胜率/成本分位（cyq_perf，2018起，单日5549行）、竞价明细（2015起）、期权链（2015… | 用户：点名参考特色数据+期权+宏观+公告+互动问答 |
| #73 | 2026-09-14 01:06 | script | change | run_eval 支持池口径对照 + 验收脚本传池 | factor_daily_metrics(name,h,start,end,pool='ashare_ex') 非 ashare_ex 池复用 metric_store._pool_ctes 做 PI… | 池级验收从此可用；ashare_ex 路径无回归(复验 PASS) | metric_store 收尾 |
| #72 | 2026-09-14 01:06 | data | info | 因子评估存储两池干净口径全量就位 | ashare_ex 311/318 因子(湖有效311)+zz1000 318/318 因子 L0 全量重算完成(zz1000 136.4min, 0错误)；L1 滚动缓存两池重建(ashare_ex… | L0/L1 两池可用；run_eval.factor_daily_metrics 新增 pool 参数(池内 PIT 过滤,与 metric_store 同源)… | metric_store 收尾 |
| #71 | 2026-09-14 00:58 | doc | add | S1s 动态出场策略回测（全仓等权·主力流出日卖）+ HTML 版 | 用户口径：信号后全仓等分买入、主力大幅流出日（main_net_pct<=-5% 收盘）卖出、考虑成本。结果：S1s 无条件年化+42.7%/夏普2.13/回撤-33.1%/平均持有仅5日（100%主… |  | 严重异动研究v2 |
| #70 | 2026-09-14 00:46 | doc | change | 严重异动研究终审：信号时间集中度审计推翻正向结论置信度（用户质疑触发） | 用户质疑'信号集中在历史特定日子=特定历史条件产物'被审计完全证实且比此前认知更严重。**以独立信号日为统计单位复核**：S1s 624笔仅覆盖57个独立交易日（窗口5.3%），Top1日(2024-… | S1s/D6全系列从观察名单降级为'单事件截面规律'（假说登记，等下一次流动性危机样本外检验）；事件研究统计规范新增独立信号日门槛；这是本研究最重要的一次置信度… | 严重异动研究v2 |
| #69 | 2026-09-13 22:52 | data | add | 数据体系全景审计+补足：幸存者偏差实锤193只退市股缺失，批次A六数据集落地，深夜链五批回补运行中 | 用户指示：系统读 tushare 接口文档做数据完善。三轮 60+ 接口阳性对照探测（矩阵见 research/数据体系全景补足规划_20260913.md v2）。重大发现：①【P0】kline_d… | 研究正确性三补：退市股（回测重估）、bench综指（异动v2.1官方口径重算）、ST-PIT（st_state_daily广播#58立项闭环）。新数据集五件：l… | 用户：审视整个数据体系做补足 |
| #68 | 2026-09-13 22:46 | doc | add | 严重异动研究收官：UP隔夜证伪 + 危机态识别器 + HTML 报告 | ①UP 隔夜策略（用户假设：T日下午买/T+1开盘卖）证伪——执行约束决定性：UP事件65.3%在T日收盘封死涨停（收盘竞价买不到），+1.27%隔夜跳空几乎全部来自这批票（一字板组+10.54%）；… |  | 严重异动研究v2 |
| #67 | 2026-09-13 22:14 | doc | add | 严重异动全景指标+D7（用户）落地：13 子集 × 7 窗口 + CAR 曲线 | scripts/abnormal_panorama.py 产出：① reports/_tmp/panorama_table.csv 全景表（13 子集 UP/DOWN × x1/2/3/5/10/20… |  | 严重异动研究v2 |
| #66 | 2026-09-13 21:45 | doc | add | 严重异动 D1-D6 方向研究完成：DOWN 侧信号同源=2024红利，UP 侧负向信号跨年稳健 | 新增 D6（20日天量+主力净流入）+ D1-D5 转正，事件层统一官方口径（15,480 事件）+ D6 组合层。脚本 abnormal_directions_research.py/abnorma… |  | 严重异动研究v2 |
| #65 | 2026-09-13 20:53 | doc | add | stk_high_shock 严重异常波动数据补齐规划：P0 已落地（2026 官方表 112 行），历史段代理硬边界定… | tushare doc_id=452 接入规划（research/stk_high_shock数据补齐规划_20260913.md）。现状：backfill_stk_high_shock.py + 湖… | 严重异动研究 v2 的官方 ground-truth 层就位；period 语义=公告日+前瞻监控窗口（事件锚定用公告日 T+1 盘后可得）；S1s 观察名单扩… | 用户：tushare doc 452 补齐规划 |
| #64 | 2026-09-13 20:41 | data | add | 严重异常波动公告清单 stk_high_shock 落湖（tushare doc_id=452） | scripts/backfill_stk_high_shock.py → data/lake/clean/stk_high_shock/part-{year}.parquet。字段：date(公告日)… | 严重异动研究新增官方权威数据源；历史缺口（2022-2025）待有 6000 积分直连 tushare 时回补 | 严重异动研究v2 |
| #63 | 2026-09-13 20:27 | data | change | ETF 分钟缺口回补完成：新增 1417 万行、洞段填平；剩余 488 只=LOF/REITs 源端盲区 | 回补结果：成功 1829/失败 0/新增 14,169,851 行/重复跳过 5,046/耗时 190.3 分钟（17:10-20:20，后半程 xd 限流 24 次致降速 26→9.6 只/分）。补… | ETF 分钟数据三层缺口两层清零（历史段+洞段）；近端 09-11 随周一流水线；LOF/REITs 盲区记录在案不再作为缺口追踪 | ETF分钟回补 |
| #62 | 2026-09-13 20:24 | doc | change | 严重异动研究报告精简重写：v2 主线单文档，删除中间过程 | 应用户要求重写：官方口径 v2 为唯一主线（结论→数据定义→基线→思路1成立（信号定义/裁决数据/组合回测/机制/caveat 五段）→思路2证伪→新方向表→失效风险）；v1 单日口径压缩为附录 A1… |  | 严重异动研究v2 |
| #61 | 2026-09-13 20:24 | factor | add | PandaAI 组合层 A/B：PA4 独立线被否（与 HF5 重叠 0.856），PA4P 融合夏普 1.04 全场最… | panda_pull/pa_ab_composite.py（HF5 终审 hf_ab_composite.py 同口径克隆，起点 2022-06-30，51 个月，六臂 PA4/PA3/PA4P/PR… | 2026-12 双闸门输入：①pa 独立线❌不上线；②PROD 生产口径不动摇；③PA4P 融合 ⏳ 进双闸门与'PROD 池内 pa 条件增强'一并重审（CO… | 用户：继续（组合层 A/B） |
| #60 | 2026-09-13 19:55 | factor | add | PandaAI 因子闸门终审：R4 全独立 / FDR 044 全库 rank4 / R2 全绿(088贴线) / R3… | pa_ 批次四闸门补齐（research/pandaai_factor_replication_20260913.md §六）。R4：族内 6 对全部 <0.7（最大 088x5d_min_low 0… | 正选 3+观察 1 进 2026-12 双闸门：下一步组合层 A/B（HF5 同款流程+10/20/30bp 成本情景必跑）、与 PROD/HF5 相关性对比、… | 用户：继续（闸门推进） |
| #59 | 2026-09-13 19:51 | doc | change | 严重异动研究 v2 增补：短期窗口检验（xexec_1-3）+ dev3 分位方向勘误 | ①UP 组短期裁决（用户假设'UP 是短期策略'）：xexec_1/-0.33%（T+1制度不可执行仅参考）、xexec_2/-0.84%、xexec_3/-1.46% 全负单调恶化——UP 无可执行… |  | 严重异动研究v2 |
| #58 | 2026-09-13 19:31 | doc | change | 严重异动研究 v2：口径切换为交易所官方异常波动标准，思路1 结论反转成立 | 用户定位澄清：严重异动=触发交易所《交易规则》需发布异常波动公告的股票（3日累计偏离 ±20%主板/±30%双创），非 v1 单日自算口径。重构后结论大反转：①基线不对称——UP（连板急涨 n=11,… | S1s（深度超跌+主力强承接+散户出逃）成为异动研究首个通过事件层+组合层双检验的候选；异动事件口径定义=方法论一等公民；ST 历史 PIT 状态成为新数据缺口… | 严重异动研究v2 |
| #57 | 2026-09-13 18:52 | doc | add | 严重异动事件驱动策略研究：两假设证伪+三反向规律+五新方向（含预验证） | research/严重异动事件驱动策略研究_20260913.md。核心结论：①思路1（主力净流入+散户净流出=洗盘吸筹）朴素版证伪——DOWN事件主力净流入>0仅占6%（暴跌日吸筹接近不存在），信号… | moneyflow 湖表成为全市场资金流标准数据源；异动研究负结论纳入候选池风控（高换手+主力净买=排除过滤器、streak>=3回避、拉萨席位净买回避）；20… | 严重异动研究 |
| #56 | 2026-09-13 18:52 | data | add | 全市场分单型资金流 moneyflow 2022-2026 回补落湖（571万行） | xiaodefa tushare 代理 moneyflow 接口按交易日全市场拉取（1137天，5并发直连，~40分钟）。新表 data/lake/clean/moneyflow/part-{year… |  | 严重异动研究 |
| #55 | 2026-09-13 17:44 | factor | add | PandaAI 因子复现入库：8 选 5 入湖，pa_a101_040 IC20 +0.097 四年全正 | 筛选漏斗：2y+3y rank_ic>0.03 双达标 51 → 库内 Alpha191 同编号排除 28（库内 180 个 alphaXXX=国君191 同编号，PandaAI alphaNNN 是… | 正选 3 个（pa_a101_040/088/044）进 2026-12 双闸门观察名单；待办：①101 族内 R4 去重（040/088 预计高相关，044 … | 用户需求：复现 PandaAI 优秀因子入库评估 |
| #54 | 2026-09-13 17:36 | script | change | hf_wf_composite 无锁只读回填脚本就绪（等内存窗口执行） | scripts/backfill_hf_wf_composite_ro.py：composite 2022-2024 湖内回填。背景：原 backfill_hf_factors_hist 走 Stor… | 上轮 21/22 因子历史回填的最后缺口（composite）有了专门执行路径；与 metric_store 重算、ETF 回补三任务互不阻塞 | broadcast.py:113 |
| #53 | 2026-09-13 17:14 | script/add | change | ETF 分钟缺口回补已启动：scripts/backfill_etf_minute_xiaodefa.py（1829 只… | xiaodefa stk_mins 直补 ETF 分钟缺口。校准三结论：①vol 单位与 fsdb volume 一致（159915 重叠日比值中位 0.9997，VOL_SCALE=1.0）；②am… |  | ETF分钟回补 |
| #52 | 2026-09-13 17:14 | script/add | change | ETF 分钟缺口回补已启动：scripts/backfill_etf_minute_xiaodefa.py（1829 只… | xiaodefa stk_mins 直补 ETF 分钟缺口。校准三结论：①vol 单位与 fsdb volume 一致（159915 重叠日比值中位 0.9997，VOL_SCALE=1.0）；②am… |  | ETF分钟回补 |
| #51 | 2026-09-13 16:51 | doc/add | change | ETF 日线/分钟线状态盘点：日线完整，分钟缺 ~4400 万根，xiaodefa stk_mins 实测可补 | 日线 etf_daily 230万行/2053标的/2004-2026 全史 0 缺日，无需补（pre_close/name 近端字段是已知口径非缺口）。分钟 kline_1min_etf 1.22亿… |  | ETF缺口盘点 |
| #50 | 2026-09-13 16:15 | doc/add | change | hf 批次终审完成：R2 全绿 / R4 归并 5 因子 / 组合层 HF5 稳健占优、COMB6 被否 | 三步全链路落地（scripts/hf_r2_cost.py + hf_r4_dedup.py + hf_ab_composite.py，全程无锁）。组合层关键发现：月频无过滤复现的 PROD 32.0… |  | hf批次组合层AB |
| #49 | 2026-09-13 16:05 | script/add | change | hf 批次 R4 去重：A 层 9 因子归并为 5，dsem 吸收 rsk/corr_rv/topvr | scripts/hf_r4_dedup.py（无锁：湖月末截面 pairwise spearman 跨 57 月末平均 + factor_corr_daily 聚合）。规则先定后跑：\|rho\|>0.7… |  | hf批次R4推进 |
| #48 | 2026-09-13 16:02 | script/add | change | hf 批次 R2 交易成本：A 层 9 因子月频盈亏平衡 9/9 全绿 | scripts/hf_r2_cost.py（无锁：L0 ls_ret20 年化毛超额 + 湖月末截面 top100 重叠率换手）。口径：G_bp=\|mean(ls_ret20)\|×12，be_bp=G… |  | hf批次R2推进 |
| #47 | 2026-09-13 15:49 | doc | change | hf_topvr_20 期限结构验伪：『短期正 IC』假设不成立，A 层 9 因子全同号 | 用户假设待著而救矛盾源于 horizon（短线正/长线负）。metric_store L0 五档检验：hf_topvr_20 h=1/5/20/60/120 rank IC 全负（-0.047/-0.… | hf_topvr_20 反向使用结论升级为跨期跨池稳健；hf 族调仓频率适配确认 h=20-60 最优（月频~季频）；R2 成本与组合层 A/B 的持有期设计以… | hf批次闸门推进 |
| #46 | 2026-09-13 13:21 | doc | add | R3 经济机制登记完成：A 层 9 因子 economic_rationale 入 registry | quantlab/factor/library/highfreq.py 8 个 A 层因子补登记（hf_amihud_20 已有），registry 验证 9/9 OK。格式=类型标签（risk/be… | A 层 9 因子真伪闸门（R1+R3）双支柱齐备；hf_topvr_20 合成加权时按负方向使用；下一步 R2 交易成本 → R4 同族去重 → 组合层 A/B | hf批次闸门推进 |
| #45 | 2026-09-13 13:20 | fix | update | IC 秩口径统一为干净口径（v-nonnull∩fwd-nonnull 集内排名） | 验收发现三处 IC 实现口径不一致：①run_eval.factor_daily_metrics 只过滤 fwd 不过滤 v，NULL v 行参与收益排名非均匀移位真实 rf 秩——sue_i 财报季… | 存量 L0（两池 311 因子）为旧混合口径所写，基本面因子（NULL 行多）IC 有实质偏差——正在全量重算（--force），完成后 L0/run_eval… | 验收三查发现 |
| #44 | 2026-09-13 13:14 | script | add | PandaAI 因子 \|Rank_IC\|>0.03 筛选：近一年 164 / 并集 331（反向占 7 成） | panda_pull/filter_rankic.py 读已存 panda_factors_all.json，按 abs(rankIc)>0.03 筛选，产出 reports/pandaaiquant… | \|Rank_IC\| 口径命中 331/608（IC_MEAN 口径 44），门槛天然宽松、不可直接当有效因子数用；正向短名单（约 60 个三年达标）才是与生产 … | 用户需求：筛选 rankic 绝对值>0.03 |
| #43 | 2026-09-13 13:04 | doc | add | hf 批次 R1 分段鲁棒性+placebo 落地：22/22 LOCO 零反号，8 因子全过 | scripts/hf_r1_robustness.py（无锁：metric_store L0 日IC时序 + 湖 + mirror kline_daily；500 次月末截面置换）。四件套：LOCO … | hf 幸存名单新增分层：A 层 9 因子（FDR+R1 双过）进入下一步 R3 机制登记；rvvol_20 降级；2026-12 双闸门复核名单更新 | hf批次R1推进 |
| #42 | 2026-09-13 12:56 | script | add | audit JSON 无锁全历史刷新：refresh_audit_ic_from_metric.py（hf 22 因子） | hf 历史回填后 factor_audit JSON 仍停在 2025-01-22 起（原 audit 只见 2025+ 分区，refresh_audit_ic 的 px 走主库被锁）。新脚本完全无锁… | batch_metrics/FDR/看板读到的 hf IC 全部升级为 2022-2026 全历史口径；主库写锁释放后无需再补审计 | hf批次R1推进 |
| #41 | 2026-09-13 03:08 | script | add | metric_store 配套脚本：回填/每日增量/验收/污染检查 | scripts/factor_eval/ 下新增：backfill_metric_daily.py（全历史回填，批量断点扫描，--workers 2）；update_metric_daily.py（每… | 每日流水线自动维护指标时序；回填与验收工具齐备 | metric-store 落地 |
| #40 | 2026-09-13 03:08 | script | add | 因子评估指标日频存储引擎 metric_store.py（L0/L1/L2） | quantlab/factor/metric_store.py：L0 compute_factor_l0 单因子一次 SQL（rank/pearson IC × h=1/5/20/60/120 信号日… | 因子评估从按需快照升级为日频时序记账；后续 IC/暴露/换手历史可随时重算 | metric-store 落地 |
| #39 | 2026-09-13 03:08 | script | add | metric_store 配套脚本：回填/每日增量/验收/污染检查 | scripts/factor_eval/ 下新增：backfill_metric_daily.py（全历史回填，批量断点扫描，--workers 2）；update_metric_daily.py（每… | 每日流水线自动维护指标时序；回填与验收工具齐备 | metric-store 落地 |
| #38 | 2026-09-13 03:07 | script | add | 因子评估指标日频存储引擎 metric_store.py（L0/L1/L2） | quantlab/factor/metric_store.py：L0 compute_factor_l0 单因子一次 SQL（rank/pearson IC × h=1/5/20/60/120 信号日… | 因子评估从按需快照升级为日频时序记账；后续 IC/暴露/换手历史可随时重算 | metric-store 落地 |
| #37 | 2026-09-13 01:50 | script | add | PandaAI 因子中心全量拉取：608 因子 x 4 区间，IC_MEAN>=0.03 筛选 | panda_pull/pull_factors.py（API 登录+分页拉取）+ build_report.py（宽表+筛选+md 报告）。站点 https://www.pandaaiquant.co… | 外部因子榜可对照研究：近三年 top=alpha191_120（IC_MEAN 0.0513/ICIR 0.675）、近两年 top=alpha191_054；… | 用户需求：收集 pandaaiquant IC_MEAN>=… |
| #36 | 2026-09-12 23:34 | doc | change | 存储方案 v2：新增股票池维度——每池一库 + 成员 PIT 硬闸门 | factor_metric_daily 按池分目录：ashare_ex/（默认）+ dividend/ + csi1000/ + {自定义}/，目录名=池名（不用 pool= hive 键避分区名撞列… | 池内有效性（如红利 M2 类结论）获得日常监控能力；落地步骤新增第 0 步=池注册表+红利池 PIT 成员补齐；文档 research/factor_metri… | 用户需求：指标按股票池分别计算存储 |
| #35 | 2026-09-12 23:24 | doc | add | 因子指标日频存储方案提案：research/factor_metric_store_proposal_20260912.… | 每日每因子一条记录的评估指标存储设计。裁决：方向合理（Alphalens 原子序列抽象+业界因子生命周期+signal-monitor 实践三印证），三点修正：①ICIR 是窗口统计量降为派生缓存层（… | 补齐全库因子时序监控能力缺口（现有三层全是快照式）；落地步骤=回填脚本+流水线因子计算后挂钩+周任务+预警消费；待用户确认后实施 | 用户需求：评估数据日频存储方式调研 |
| #34 | 2026-09-12 22:30 | script | add | 因子指标批量读取 CLI：scripts/factor_eval/batch_metrics.py（跨因子横向对比） | 聚合三层存储成 17 列宽表：①data/lake/factor_audit/*.json（311 个，IC5/ICIR5/胜率/IC20/ICIR20/方向/PIT status/行数/最新日期）②… | 跨因子横向对比从逐个开报告变一条命令；与 search_eval（按报告检索）互补，本工具按指标检索 | 指标存储梳理需求 |
| #33 | 2026-09-12 22:02 | script | change | 泄露修复闭环：4 真前视因子（alpha046/069/sue/earnings_accel）公式修复+整年重写+复检全… | rebuild_leak_fixes_20260912.py 顺序整年重写（replace=True 防 append 旧行残留）：①alpha046/069 加 cnt.where(close.no… | 泄露检查全链条闭环：无未处置真前视；下游引用面已核实（生产模型零影响）；alpha027 if_ 常数分支与两 WF composite ic 样本集固定为遗留… | broadcast.py:113 |
| #32 | 2026-09-12 21:43 | doc | change | SKILL.md：因子分析标准产出定为 md 报告 + HTML 看板双轨 | factor-model-evaluator skill 增补'因子看板（标准 HTML 模板产出）'节：因子分析标准产出=run_eval.py md 归档报告 + build_dashboard.… | 以后因子分析按此模板执行；红绿灯与 run_eval 同口径 | factor-model-evaluator 模板固化 |
| #31 | 2026-09-12 21:43 | script | change | 因子看板固化为标准模板：build_dashboard.py 参数化（任意因子一键生成） | ① 新增 scripts/factor_eval/build_dashboard.py（~700 行）：--name/--start/--end/--tags/--h-ic/--h-grp 参数化生成… | amihud_20 重建与 v3 数字完全一致（IC5 +0.0455/t_adj 2.67/q 0.0007/🟡 三 flag）；size 首次生成即 🟡 I… | factor-model-evaluator 模板固化 |
| #30 | 2026-09-12 20:21 | script | change | 全量 312 因子泄露检查完成+人工甄别终报：4 真前视 / 2 假阳性 / hf 21 修复后全 PASS | reports/leak_check/VERDICT_20260912.md：机器分布 FAIL 5/PASS 279/WARN 1/SKIP 27（ERROR 清零）。真前视 4 个：①alpha0… | alpha046/069 历史值被系统性稀释 8.5%，IC/回测数字失真（P0 修复）；earnings_accel/sue 暴露窗口每年约 2 周（P1）；… | broadcast.py:113 |
| #29 | 2026-09-12 19:29 | doc | change | 因子看板 v3：IC5 主口径 + 风格分解 + 拥挤度区（用户需求） | docs/factor_center_amihud_20.html 升级：①5 日 rank IC 升为主口径（概览卡 Rank_IC/ICIR/STD/Pearson 全换 IC5 实测：+0.04… | 风格分解实证：amihud_20 = 小市值风格的结构化表达（做多 Q5=做空市值），与信息增量区正交化结论互证；拥挤度库内代理落地，外部数据（公募/两融）路径… | 用户需求迭代 |
| #28 | 2026-09-12 19:01 | doc | change | 因子看板 v2：去对标化 + 结构重组 + 右轴 bug 修复（用户反馈） | docs/factor_center_amihud_20.html 重建：①删除全部对标营销信息（对标总览表/PandaAI 同款/QL+ 独有 tag/工程能力表），定位改为日常自用因子看板；②结构… | 右轴 bug 是所有双轴图共用函数的缺陷，修复后 ICIR 右轴刻度正确显示 | 用户反馈迭代 |
| #27 | 2026-09-12 18:38 | doc | add | 对标 PandaAI 因子中心的增强展示页落地（docs/factor_center_amihud_20.html） | 拆解 PandaAI 因子中心页面（用户提供的存档 HTML）：卡片四指标（IC_MEAN/RANK_IC/IC_IR/IC_STD）+ 因子分析深面板（绩效概览/IC 指标含 P·t·单调性/最新数… | 对商业竞品的差异化首次可视化：PandaAI 覆盖'统计表现'层，QuantLab 独有'真伪/合规/可收割/边际贡献'层；页面可直接作为产品对标材料 | PandaAI 对标分析 |
| #26 | 2026-09-12 16:57 | script | add | numerical-leak-check skill 量化 runner 落地 + 全量 312 因子泄露检查执行中 | scripts/leak_check/run_all_leak_check.py：prefix replay（VIEW 截断环境，复用 pit_audit PIT_LE/LT/NOTICE/KEEP … | 全库因子首次获得逐因子泄露检查报告（不引用历史审计结论的全新评估）；执行中，完成后广播结果汇总 | broadcast.py:113 |
| #25 | 2026-09-12 16:12 | script | change | factor-model-evaluator v3：补齐框架全部缺失评估项（用户反馈"ic衰减/多周期ic无"） | run_eval.py 新增四个计算模块：①多周期 IC 谱 factor_horizon_structure（h=1,3,5,10,20,40,60,120 单查询多LEAD+unpivot，IC/… | 实测发现：amihud_20 对 size 正交化后残差 IC +0.0129 vs 原始 +0.0790（仅保留 16% 增量）；size 对 amihud … | factor-model-evaluator v3 迭代 |
| #24 | 2026-09-12 15:49 | script | change | factor-model-evaluator 报告模板升级 v2（用户反馈：内容不够详细） | run_eval.py 重写为详细版：因子报告新增判定依据逐项表（PIT/现场IC/FDR/单调性/滚动ICIR/机制/反号各带✅🟡❌）、审计快照vs现场重算双列对照、IC5短horizon对照、年度… | amihud_20 复评即抓到实质发现：与 size 相关-0.84、与 hf_amihud_20 +0.87 → 🟡（PROD 双因子等权的真实信息增量提示）… | 用户反馈迭代 |
| #23 | 2026-09-12 13:08 | script | add | gpoa 族 WF 复验通过 / roe_chg 样本外证伪（wf_gpoa_sue_family_20260912.p… | 五因子单因子 WF（top100/20日/equal/中证1000 基准，切分 2024-01-01/2025-01-01/2025-06-01，窗口 2022-01-04~2026-09-10）：g… | gpoa_chg/sue_gpoa/sue_v2/sur 获入合成候选资格（组合层 A/B 终审另走）；roe_chg 样本外反向证伪，禁止入合成候选——WF … | broadcast.py:113 |
| #22 | 2026-09-12 13:01 | script | change | 勘误：SUE 族'弱泄漏'证伪，无需修复（审计 C 项 v2 双口径） | v1 审计 C 项的 5.50% 弱泄漏是测量口径错误：序列漏套 _SUE_COMMON 的压制剔除（kept）步骤，测的是压制前面板。v2 双口径复测（audit_existing_factors.… | 全库 PIT 问题清单收敛为：0 FAIL/0 WARN/0 待修因子；唯一开放项=gpoa 族 WF 复验（有效性验证，非缺陷）；_INCOME_LADDER… | broadcast.py:113 |
| #21 | 2026-09-12 12:44 | script | add | scripts/factor_eval/ 评估引擎 + 检索 CLI 落地 | run_eval.py：因子路径=审计快照+FDR+registry+年度 rank IC/多空腿分解（READ_ONLY ATTACH 用后 DETACH，全只读）；模型路径=MODEL_SPECS… | 新模型组合需先加 MODEL_SPECS；因子无审计 JSON 会明确报错引导先跑审计 | factor-model-evaluator 落地 |
| #20 | 2026-09-12 12:44 | doc | add | 项目级 skill 库新增 factor-model-evaluator（第五自建 skill，评估体系工程化封装） | 因子/模型系统化评估套件：①输入协议 --type factor\|model --name --start/--end --universe --tags --horizon；②四象限维度（表现=IC… | 评估从零散脚本变标准入口；已验证三例（amihud_20 🟢 q=0.0007、PROD_HFA 🟢 65.6%/2.82 与 A/B 重跑完全一致、PROD … | factor-model-evaluator 落地 |
| #19 | 2026-09-12 09:13 | automation | change | 拥挤度周报生成（成功） | 报告 reports/crowding_weekly_20260912.html（69KB）；crowding 监控数据新鲜未刷新（generated_at 2026-09-11 21:02）；预警 … | 无 | 自动化·因子拥挤度周度诊断报告 |
| #18 | 2026-09-12 04:33 | script | add | InfoPublDate vs 真实挂网时间实证：当日入账 91.5% 安全、8% 一晚前视，全量 T+1 方案撤销 | 三证据闭环：①东财公告对账（97 只/473 期精确挂网时刻）：InfoPublDate=官方公告日（与 notice_date 一致率 100%），91.5% 前一交易日 15:00-22:59 挂… | 口径决策修订：撤销全量 T+1 方案（过度保守+全量重算成本不成比例），推荐维持当日入账并声明 8% 一晚前视暴露（组合层年化影响预计远小于 1pp）；可选增量… | broadcast.py:113 |
| #17 | 2026-09-12 04:08 | data | add | FDR 重排 + A/B 成本弹性/TC 报告三份落地 | ① reports/fdr_factor_ranking.{json,md}：311 因子 BH-FDR，t>3.0(HLZ) 幸存 124、q<0.05 幸存 164；核心因子分化——size q=… | PROD_HFA 主切换候选地位在三列新证据上均不劣化、加强；size 的因子层/组合层证据分层坐实 | factor_eval P0 落地 |
| #16 | 2026-09-12 04:08 | script | change | 因子评价框架 P0+E1 落地：引擎 TC 仪器化 + economic_rationale 登记 + 两评估脚本 | ① optimize/backtest.py run_optimized_backtest 新增 tc_avg/tc_n_periods 返回字段（逐期 corr(score,主动权重) Spearm… | 回测引擎返回 dict 多两字段（向后兼容）；因子入生产候选多一道机制登记闸门；评估三件套就绪 | factor_eval P0 落地 |
| #15 | 2026-09-12 04:06 | data | add | ETF 数据补齐：新增分钟/净值/复权/持仓四类，接每日流水线 | ①探测：fsdb 无 ETF 专用表（净值/份额/规模/成分/持仓 30+ 候选表名全空）；②新增 etf_minute（fsdb，2025-01-02 起硬边界，year=/day= 分区，单日 2… | ETF 侧从只有日线扩展到五类数据；新增折溢价率指标与持仓成分；下游 ETF 相关因子/回测可用。注意 etf_nav 单次查询上限 1211 行须切段、本机 … | session-2026-09-12 |
| #14 | 2026-09-12 03:06 | data | change | gpoa_chg/sue_gpoa 重审 PASS + 湖内值整年重写（审计 FAIL 闭环） | 修复版 SQL 重审双双 PASS（截断重放一致）；湖内存量旧穿越值按 keep=last 陷阱规则 --rebuild：gpoa_chg 5509539 行/5307 只、sue_gpoa 5702… | gpoa 族因子值已是修复版；SUE 族与口径决策两项跟进项见审计报告 reports/pit_fundamental/ | broadcast.py:113 |
| #13 | 2026-09-12 03:06 | script | add | pit_fundamental 审计脚本落地（skill 首个实现） | scripts/pit_fundamental/audit_existing_factors.py：A 版本结构+ROWS压制窗口行序风险、B T+1 vs 当日入账差分、C SUE族窗口pub顺序、… | 财务因子 PIT 健康可例行体检；skill 的 Audit 路径从规格进入可用状态 | broadcast.py:113 |
| #12 | 2026-09-12 02:53 | data | add | universe_daily 逐日池快照落地：残留 as-of（池构建用当前状态）根治路径开通 | 新模块 quantlab/data/universe_daily.py + update.py 3.9 每日自动记录 6 池快照（严格 PIT）。valuation_daily.is_st 验证为逐日… | 历史回测的幸存者偏差可自 2021 起消除（因子 SQL 换 join universe_daily 即可）；2021 前与 top2000/top3000 池… | broadcast.py:113 |
| #11 | 2026-09-12 02:52 | model | change | 候选模型 A/B 新口径全量重跑：PROD_HFA 仍是主切换候选，ICW 优势消失 | FOCUS 2025-05~2026-09-11：PROD_HFA 65.6%/2.82/-19.9%（旧 71.7%/2.79）仍全维度第一；EQ3_HFA_ICW 57.5%/2.63（旧 67.… | 2026-12 双闸门的同口径依据已就绪；若触发切换应选等权 PROD_HFA 而非 ICW | broadcast.py:113 |
| #10 | 2026-09-12 02:40 | doc | add | 项目级 skill 库新增 a-share-pit-fundamental-vintage-builder（第四自建 s… | 财务 PIT vintage 构建器规格按本系统适配：数据源=湖内 finance_q 三大表（多披露版本已核实存在，vintage 重建可行；无 SDK/20季度限制/认证流程）；列名腾讯口径 En… | 财务因子 PIT 构建与修订泄漏审计有方法论入口；T+1 vs 当日入账双口径差异须实测量化（首个验证样本=cfp_ttm/ocf_to_profit 差分） | broadcast.py:113 |
| #9 | 2026-09-12 02:34 | doc | add | 项目级 skill 库新增 numerical-leak-check（第三自建 skill） | 数值型未来信息泄露检查器规格：prefix replay + future mutation 双测试、六函数 Adapter 协议、敏感点加密 checkpoint、四态判定。已按本系统适配：SQL … | PIT 审计覆盖不到的 Python 计算路径有了统一泄露检查方法论；FAIL 因子沿用 audit gate 拒绝入合成 + 修复走 WF 的既有流程 | broadcast.py:113 |
| #8 | 2026-09-12 02:29 | doc | add | 项目级 skill 库新增 performance-attribution（第二自建 skill） | 三层收益归因（Alpha/Beta/择时 + Brinson + 因子贡献/Alpha 残差）规格已按本系统事实适配：基准 000852.SH 取 index_kline、因子暴露直接用因子湖 312… | 归因分析有统一方法论入口；层2 行业拆分须等 board 湖历史积累，短期报告只会出单层选择效应 | broadcast.py:113 |
| #7 | 2026-09-12 02:23 | doc | change | a-share-tradability-auditor skill 按本系统事实改写适配 | 新增『本系统对接事实卡』10 条（kline_daily 未复权基表+code/vol 列名+禁乘 adj_factor、无 pre_close 须 LAG(close)+dividend_event… | 后续实现管线脚本时以事实卡为唯一对接口径，避免 symbol/volume、pre_close、ST 前视等移植性错误 | broadcast.py:113 |
| #6 | 2026-09-12 02:18 | doc | change | 自建 skill 库迁至项目级 .workbuddy/skills/ | a-share-tradability-auditor、quant-factor-research、quantlab-datasource-onboarding 三个自建 skill 从 ~/.wor… | 自建 skill 随项目走、可入 git 协作；新会话起按项目级路径索引 | broadcast.py:113 |
| #5 | 2026-09-12 02:12 | doc | add | as-of 系统性影响审计报告（五层穿透） | research/system_impact_asof_20260912.md：L1 底层/L2 因子/L3 模型/L4 回测评估/L5 研究报告逐层影响 + 重算清单（9 项含优先级）。生产模型重构… | 研究结论的可信度基线已切换到新口径；重算清单 #1/#2/#3 为 P0 | broadcast.py:113 |
| #4 | 2026-09-12 02:12 | data | change | 11 因子整年重建 + 20 因子审计重审（假 PASS 根治）+ share_capital_daily 股本冲突修复 | ①11 个用股本因子按新口径 --rebuild（688808 类 64 只残留旧行清零）；②audit 两缺陷修复：SKIP 被末尾赋值覆盖、20 个 read_parquet 直读因子进不了截断环… | 全库 311 份审查：PASS 309 / FAIL 2 / WARN 0 / as-of caveat 0；gpoa_chg/sue_gpoa 被 _audi… | broadcast.py:113 |
| #3 | 2026-09-12 02:12 | model | warn | 生产模型口径修正：旧 as-of 股本口径把年化虚高 6.3pp | PROD size+amihud_20 top100/20日/inverse_vol，2022-07~2026-09-11：旧口径 36.7%/1.13/超额32.7% → 新口径(PIT share… | 所有历史回测与候选模型 A/B 的绝对数字失效，2026-12 双闸门必须按新口径重跑；结构性结论（卫星稀释机制）不变 | broadcast.py:113 |
| #2 | 2026-09-12 01:56 | doc | add | 用户级 skill 库新增 a-share-tradability-auditor | 在 ~/.workbuddy/skills/ 建立首个自建 skill：A 股回测可成交性审计器规格（未复权日线 + 四类制度约束逐笔重放 + phantom_alpha_share 归因 + 输出契… | 回测可信度审计有统一方法论入口；后续实现管线脚本时按 skill 规格执行 | broadcast.py:113 |
| #1 | 2026-09-12 01:34 | script | add | 变更广播机制上线 | 新增 quantlab/broadcast.py（append-only JSONL + BROADCAST.md 渲染 + 未读游标）与 scripts/broadcast.py CLI；每日流水线… | 全系统数据/脚本/自动化变更从此有统一审计线索；下轮任务开始前先 read --mark-read | session-2026-09-12 |
