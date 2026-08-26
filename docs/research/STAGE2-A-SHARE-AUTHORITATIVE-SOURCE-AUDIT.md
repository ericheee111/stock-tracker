# Stage 2A：A 股权威数据来源、修订语义与许可审计

> 审计日期：2026-08-14（Asia/Shanghai）
> 状态：`RESEARCH_COMPLETE / LICENSE_PENDING / T3_NOT_REACHED`
> 范围：SSE/SZSE Calendar + Security Identity/Status + Historical Universe + Index/Industry Membership + Corporate Actions
> 目标：为接近零成本、可复现、可保留 Point-in-Time（PIT）修订历史的 A 股研究链冻结来源、采集证据和失败关闭边界
> 非目标：本审计不证明任何真实策略收益，不授权生产采集，不把公开网页等同于可再分发数据产品

## 1. 结论摘要

1. **不存在经本轮审计证实的单一免费公开源，可以同时提供历史全量 A 股 Universe、逐时点证券状态、公司行为、指数/行业历史成分、原始版本和明确的长期机器化保存/再分发许可。** 官方公开页适合逐条事实证明，不能自行声称 `complete=true` 或 `verified=true`。
2. 接近零成本的可行路线是“三层证据栈”：
   - **Primary**：SSE、SZSE、ChinaClear、证监会/中国上市公司协会（CAPCO）、中证指数/国证指数的正式公告、规则、月报/年报和静态附件；
   - **Corroboration**：CNINFO 法定披露原文、交易所月报/统计汇编、ChinaClear 终止登记公告，以及 Tushare Pro 等二级规范化结果；
   - **Fallback**：AKShare、东方财富公开接口只用于发现缺口和故障恢复，不用于提升 Trust Tier。
3. 公开官方原件即使逐字节保存，单源上限通常也只是**某一事实的 T2 候选证据**。要形成 T3 研究快照，仍需：时间区间无缺口、退市样本闭环、修订链、跨源对账、许可证确认，以及 calendar/universe/status/corporate-action 的联合 Manifest。
4. SSE/SZSE 年度休市公告提供发布日期和适用日期；临时调整依赖后续单独公告。只给“发布日期”而不给精确发布时间的来源，不能伪造日内 `known_at`，保守的 `usable_from` 应落到下一可交易会话。
5. “今天的证券列表”只是一张当前态快照。它天然遗漏过去已退市且今天不再出现的证券，也不能还原历史名称、证券类型、停复牌、ST/*ST 或退市整理状态，故不能构造历史 Universe。
6. 指数历史成分的公开 PIT 证据主要是调样公告；完整静态数据由中证指数数据服务或授权厂商提供。行业分类可从证监会旧季度结果和 CAPCO 现行半年结果拼接，但官方主题/概念板块的免费、完整、带版本历史未找到。
7. SSE、SZSE、中证指数及其信息公司均有知识产权或数据授权限制；`robots.txt` 也未给出肯定的采集许可。**技术上能下载不等于获得长期存储、批量抓取或再分发权。** 这些问题必须由人工/法务确认。
8. HiThink-Tech/Financial-API 可作为同花顺官方运营的规范化 A 股数据 API 和原始响应捕获候选，但仓库 MIT License 只覆盖代码，不自动授予接口数据的长期保存、训练或再分发权；当前项目仅将其冻结为默认关闭的 `T1_BEST_EFFORT` 日线 exact-raw 捕获源，真实 Key、覆盖、修订、许可与跨源对账通过前不得升级。

## 2. 审计方法与 Trust Tier 判定原则

### 2.1 调研方法

- 优先查验交易所、登记结算机构、法定披露平台、指数编制机构和监管/自律组织的一手页面、PDF、XLSX 或公告附件。
- 对动态页面同时区分“页面展示合同”和“网页当前实现中的查询 URL”；后者若无公开 API 文档，不冻结为稳定 API。
- 对每项来源分别审计：覆盖字段、历史深度、发布时间/生效时间、修订保留、原始字节、频率/成本、知识产权与再分发。
- 本轮做了两轮检索：先按数据域查正式来源，再反向搜索“免费、历史全量、修订、下载/API”。反证轮仍只找到当前列表、公告档案、接口规范或需授权的历史数据产品，未找到满足全部条件的单一免费来源。
- 页面写明的“发布日期”按来源原值记录；只显示日期时，不解释成 `00:00:00`，也不解释成交易前已知。

### 2.2 Tier 口径

| 结论 | 本审计口径 |
|---|---|
| `T1 BEST_EFFORT` | 当前公开快照、二级聚合、未证明历史覆盖或修订保留的结果。可用于 UI/候选发现，不能用于正式回测。 |
| `T2 OPERATIONAL_VERIFIED` | 一条事实有官方原件，或经两个独立来源对账且时间语义可解释；仍不等于历史总体完整。 |
| `T3 RESEARCH_GRADE` | 必须是联合快照：官方原始工件、可审计 Manifest、PIT Calendar、历史 Universe、逐日/逐时状态、公司行为、覆盖报告、修订链和许可边界共同通过。 |

以下事项**不会**自动升级 Tier：域名看起来官方、HTTP 200、字段名叫 `verified`、调用方传入布尔值、重新计算 identity、文件有 SHA-256。SHA-256 只能证明“捕获后的这些字节没有变”，不能证明捕获前的来源权威性、覆盖完整性、时间正确性或许可合法性。

### 2.3 时间和修订语义

每个规范化事实至少保留：

```text
source_published_at
source_published_granularity = DATE | SECOND | UNKNOWN
observed_at
retrieved_at
known_at
usable_from
effective_from
effective_to
revision_id
supersedes_revision_id
source_uri
raw_artifact_id
parser_version
```

约束仍是 `known_at <= usable_from <= as_of`。当来源只有日期：

- `source_published_at` 保存来源日期与 `DATE` 粒度，不补零成伪精确时间；
- `known_at` 只能取首次可证明观察时间或带可靠服务端时间戳的时间；
- 若无法证明盘前可得，日线研究保守使用下一开市会话作为 `usable_from`；
- 日内 Replay 对该事实失败关闭。

## 3. 必须回答的 PIT 问题

### 3.1 为什么“今天的证券列表”不能构造历史 Universe

今天的列表是条件在“存活到今天且仍被当前端点收录”的样本：

- 过去已上市、后来退市的证券会消失，产生 survivorship bias；
- 在历史截点之后才上市的证券今天会出现，不能倒灌到过去；
- 当前简称、板块和证券类型可能是后改值；深交所主板与中小板于 **2021-04-06** 合并且证券代码、简称不变，证明“代码不变”也可能伴随类型变化；相关通知发布于 **2021-03-31**（[SZSE 通知](https://www.szse.cn/disclosure/notice/general/t20210331_585343.html)）；
- 缺席只表示“端点当前没返回”，不能区分未上市、退市、暂停上市、证券类型被过滤、接口故障或页面查询条件；
- SSE 当前股票列表只展示代码、简称和上市日期等当前字段（[SSE 股票列表，访问于 2026-08-14](https://www.sse.com.cn/assortment/stock/list/share/)），SZSE 当前股票列表同样是当前市场视图（[SZSE 股票列表，访问于 2026-08-14](https://www.szse.cn/market/product/stock/list/index.html)）。二者均没有公开的 `as_of`、历史版本号或完整性声明。

因此历史 Universe 必须由“上市/重新上市/终止上市/类型变更事件 + 每个交易日状态 + 当前/退市存量对账”重建，而不是把今天的结果复制到历史日期。

### 3.2 哪些来源能证明退市样本未被遗漏

没有一个公开源单独足以证明。可形成闭环的证据组合是：

1. SSE/SZSE 的终止上市决定和实际终止上市公告；例如 SZSE 于 **2026-05-25** 发布万方发展终止上市决定，并说明是否进入退市整理期（[公告](https://www.szse.cn/disclosure/notice/t20260525_620692.html)）。决定日不能替代实际退市日。
2. SSE 暂停/终止上市列表与已退市公司入口（[暂停/终止上市公司](https://www.sse.com.cn/assortment/stock/list/delisting/)、[已退市公司信息](https://www.sse.com.cn/services/information/delisting/)），以及 SZSE 公司上市/终止上市公告档案（[公告索引](https://www.szse.cn/disclosure/notice/company/)）。
3. ChinaClear 对单只 A 股发布的“终止提供证券交易所市场 A 股登记服务”公告，构成登记结算侧的独立业务终点证据（[ChinaClear 通知公告，访问于 2026-08-14](https://www.chinaclear.cn/)）。
4. 退市后转入退市板块的衔接规则和后续挂牌证据。SSE、SZSE、BSE、NEEQ 与 ChinaClear 的过渡安排发布并施行于 **2022-04-29**（[联合通知](https://www.szse.cn/disclosure/notice/t20220429_592690.html)）。
5. 交易所月报/年报的上市公司数量、当年新增和终止数量，用于总量连续性方程：`end_count = begin_count + listings + relistings - delistings + scope_changes`。

只有逐证券闭环、年度总量连续、无未解释差异、日期/类型不冲突后，校验层才可以对一个明确边界的 Universe 版本给出完整性结论；来源 Adapter 本身仍不得写 `complete=true`。

### 3.3 哪些字段只有公告发布时间，没有可靠 `usable_from`

| 字段/来源 | 已知 | 不能直接知道 | 保守处理 |
|---|---|---|---|
| 年度休市公告 | 公告日期、休市日期范围 | 日内准确发布时间；后续临时变更是否已发生 | 日期粒度；下一开市会话可用；继续监听单独通知 |
| 当前证券列表 | 当前返回值、常见的上市日期 | 该值何时首次公开、历史更正何时发生 | 仅作观察快照，不回填历史 |
| 名称/代码/证券类型变化 | 公告日期；有时有未来生效日 | 只有结果页时缺首次披露时刻和旧版本 | 必须找到实施公告；否则日内 Replay 禁用 |
| ST、*ST、其他风险警示 | 公告日；实施公告通常写明起始交易日 | 网页发布日期常无时分秒；历史产品状态文件是否已在盘前可得 | 起始日按公告明示，`usable_from` 不早于可证明发布时点 |
| 终止上市决定 | 决定公告日 | 实际最后交易日/终止上市日可能尚未确定 | 不把决定日写成 `delisted_on` |
| 公司行为预案 | 董事会/股东会公告日 | 是否最终实施、股权登记日、除权除息日 | 仅标 `PROPOSED`；实施公告出现后另建事实 |
| 行业分类结果 | 结果文件发布日期、所属报告期 | 单家公司分类在报告期内的精确生效日通常未逐项声明 | `usable_from` 不早于结果发布；不得倒推到期初 |

相比之下，指数定期调样公告通常同时给出发布日期和未来生效日；例如国证指数调样公告于 **2026-05-29** 发布并明确 **2026-06-15** 生效（[SZSE 公告](https://www.szse.cn/disclosure/notice/t20260529_620819.html)）。这可支持日级 `effective_from`，但仍未必支持精确日内 `known_at`。

### 3.4 是否存在后补修订覆盖旧值的问题

**存在，而且是常态风险。**

- 当前股票列表、公司概况、指数成分查询是 mutable view，重拉只能得到“现在认为正确”的值；
- 更正公告、定期报告更正、公司行为方案变更会产生后续版本；
- 交易所接口规范自身有版本修订历史。SZSE `Ver1.41` 的证券状态定义含停牌、除权、除息、ST、*ST、退市整理等状态，并保留规范修订记录（[2025 年 3 月版 PDF](https://www.szse.cn/www/marketServices/technicalservice/interface/P020250328367238567039.pdf)）；SSE 规格说明书同样列出按月/版本的修订（[SSE 市场数据文件交换接口规格说明书](https://www.sse.com.cn/services/tradingtech/data/c/10813252/files/454eeb0a88c843cba9410db2bfd6b0c0.pdf)）。
- Tushare FAQ 明示部分财务数据会在更新后返回修订结果及更新标志（[Tushare FAQ](https://tushare.pro/document/1?doc_id=122)），说明二级规范化源也可能只暴露后补值。

采集端必须 append-only：每次响应作为独立 raw artifact；Parser 输出新的 `revision_id`；明确保留 `supersedes_revision_id`；绝不原地覆盖旧值。若无法获得旧版本，字段必须标记 `revision_history=UNKNOWN`，不能假装 PIT。

### 3.5 为什么单一公开源不能自行设置 `complete=true` 或 `verified=true`

- “官方”只证明发布主体，不证明端点覆盖了所有时期、板块、状态和修订；
- 当前列表、公告索引、月报各自只覆盖一个投影视图；
- 网络失败、分页、筛选参数、已撤换附件和未公开 schema 都会制造静默缺口；
- 单源与自身一致不构成独立验证；
- `complete` 是对“Universe 边界 + 时间区间 + 字段集合 + 版本”的覆盖声明，必须由覆盖审计和跨源对账产生；
- `verified` 是验证过程的结果，不能来自 Adapter、调用参数或来源自身的营销描述；
- SHA-256 只锁定已捕获 bytes，不回答“漏了什么”“何时可知”“是否可合法使用”。

结论：Adapter 只报告 provenance、抓取结果和显式缺口；验证层在闭环后生成单独的 Evidence/Manifest，且任一未解释缺口都应保持 `false`/`UNKNOWN`。

## 4. 候选来源矩阵

> “Tier 上限”是**该来源单独使用**时的保守上限；“T2 候选”仍需本地原始工件、时间语义和校验通过。

| source / owner | official or secondary | fields | historical depth | revision history | timestamp semantics | exact raw capture feasibility | license / redistribution risk | cost / rate limit | Trust Tier 上限 | gaps | recommended role |
|---|---|---|---|---|---|---|---|---|---|---|---|
| SSE 公告、股票/退市列表、月报 / 上海证券交易所 | official | 休市、上市/退市、风险警示、停复牌公告、当前证券身份、月度发行与公司行为汇总 | 公告档案和月报跨多年；本轮逐项验证到 2023 年日历，月报站点含更早档案；当前列表无历史版本 | 公告可追加；当前列表/公司页不保留旧值；规范有版本记录 | 公告多为日期；实施公告常有生效日；当前查询无 `as_of` | 静态 HTML/PDF/XLSX 可；动态查询响应可捕获但会变 | 高：网站法律声明与信息经营授权边界需确认 | 公开页零成本；未公布稳定 API 频率/SLA | 单事实 T2 候选 | 无单一历史全量身份/状态流；许可和分页完整性未定 | **primary** |
| SZSE 公告、股票列表、统计月报/年鉴 / 深圳证券交易所 | official | 日历、上市/退市、当前身份、停复牌精确时间、风险状态、公司行为、统计汇总 | 官方月度停复牌表示例可追至 2003-08；是否每月无缺口未证实 | 公告/附件可追加；当前报表 mutable；接口规范有修订历史 | 公告日期 + 部分未来生效日；月报含 `Susp.Time/Resume.Time` | HTML/PDF/XLSX 可；报表实现参数可观察但未形成稳定 API 合同 | 高：网站与深证信息授权条款需人工确认 | 公开页零成本；报表接口无公开限频/SLA | 单事实 T2 候选 | 月度档案连续性、旧附件可得性、机器使用权未证实 | **primary** |
| A 股终止登记公告 / ChinaClear | official | 终止 A 股登记服务、登记结算业务终点 | 通知档案跨年；本轮未证明自开市以来全量 | 公告追加，未见标准化修订字段 | 页面发布日期；业务终止日期依公告正文 | HTML/PDF 原件可捕获 | 中高：公开浏览不等于批量持久化/再分发 | 公开零成本；无公开 API/SLA | 单事实 T2 候选 | 不是上市/退市主表；不能覆盖全部身份状态 | **corroboration** |
| 法定信息披露原文 / CNINFO（深交所全资信息服务主体运营） | official disclosure platform | 公司公告、实施公告、公告时间、附件 | 跨多年，按公告检索；未证明所有早期文件在线无缺口 | 更正公告可并存；查询结果/接口无公开 revision schema | 页面常给发布日期/时间，正文给实施日期 | PDF 静态 bytes 可；搜索接口未公开稳定合同 | 中高：服务条款、批量下载、长期保存和再分发需确认 | 公开零成本；未公布稳定 API 频率/SLA | 单公告 T2 候选 | 不是规范化且可证明完整的公司行为主表 | **corroboration** |
| 上市公司行业分类结果 / 证监会、CAPCO | official/self-regulatory | 证券代码、简称、行业门类/大类；规则版本 | 证监会页面有 2017-2021Q3 等季度结果；CAPCO 索引现有多期历史，现行半年发布 | 期次文件可留存；无逐公司 revision/supersedes 字段 | 文件发布日期明确；分类生效点通常不逐公司明确 | HTML/PDF 可捕获 | 中：需确认长期保存、衍生分类和再分发 | 公开零成本；无 API/SLA | T2 候选 | 季度到半年制度切换；不能覆盖主题/概念；生效时点偏粗 | **primary（行业）** |
| 指数公告、方法与静态数据 / 中证指数有限公司（CSI） | official index compiler | 指数成分、权重、调样、公司行为、除数、方法 | 公开调样公告跨期；完整历史静态数据需数据服务/授权商 | 公告版本可留；公开当前查询不等于修订链 | 调样公告常有发布日期和生效日 | 公告/PDF 可；完整静态数据需授权交付 | **高**：免责声明限制存储、抓取、传输、再发布等行为，需书面授权解释 | 公告免费；完整静态数据为数据服务，公开页未给免费额度 | 公开公告单事实 T2；当前查询 T1 | 免费完整历史成分、权重和 revision 不可得 | **primary（事件）/ reject（无授权批量库）** |
| 国证指数与深市指数调样 / 深圳证券交易所、深圳证券信息有限公司（CNI） | official index compiler | 调样增删、实施日、方法、指数权属 | 公告索引跨多年；未证明免费全量静态版本 | 公告可追加；当前成分查询可能覆盖旧值 | 通常有公告日 + 未来生效日 | HTML/PDF/附件可捕获 | 高：方法文件明确指数权属及复制/再分发限制 | 公告免费；历史/商业使用授权另议 | 单事件 T2 候选 | 权重、临时调整和所有历史版本需覆盖审计 | **primary（事件）** |
| 历史行情/证券基本信息/公告数据产品 / 上证信息公司 | official authorized vendor | 历史 L1/L2、日线/分钟、证券基本信息、公告等 | 产品声明按授权范围提供历史数据 | 取决于交付合同和产品版本 | 合同/文件规范决定 | 可，前提是授权下载和本地保存 | 高但可合同化 | 官网当前报价含年度/月度收费，非接近零成本 | 可作为 T3 组成，不可单独 T3 | 仍需 Universe、状态、公司行为及修订 SLA；成本高 | **fallback（有预算时）** |
| 深市历史增强行情与授权服务 / 深圳证券信息有限公司 | official authorized vendor | 2008-01-01 起的全证券订单/成交/快照、证券信息、状态、指数快照（按产品说明） | 产品页声明自 2008-01-01 起 | 取决于交付版本和合同 | 日文件/状态字段，需合同确认 known_at | 授权后可本地日下载 | 高但可合同化；自用、场所、系统和再分发受限制 | 商务询价/授权，非公开免费 API | 可作为 T3 组成，不可单独 T3 | 2008 年以前、修订链和跨所数据仍缺 | **fallback（有预算时）** |
| HiThink Financial-API / HiThink-Tech（同花顺官方仓库） | official-operated normalized API | A 股日线/快照、财务、估值、日历、指数/行业及全市场 Market Dumps；本项目首期只接历史日线 | REST 单请求最长 10 年；全市场长期数据另有 Dumps/marketdb 路线 | 当前文档未证明 append-only 历史修订链；重复请求可能返回后补结果 | 毫秒时间戳按 Asia/Shanghai 解释；仍未证明各字段的 PIT `known_at/usable_from` | 可保存 HTTPS JSON exact bytes；Dumps 的临时下载地址与文件身份需另建 Manifest | 中高：仓库 MIT 仅覆盖代码；API 数据存储、训练和再分发权需按账户条款人工确认 | 需 API Key；配额/限流应以真实账户和官方错误码验收为准 | T1 | 未证明退市样本、Universe/Status/Corporate Action、修订保留、完整性和授权范围 | **corroboration / raw capture；默认关闭** |
| Tushare Pro / Tushare 社区与运营方 | secondary | 日历、股票基本信息、停牌、ST、指数成分、公司行为等规范化表 | 各接口不同；由平台声明，未以官方原件逐项审计 | 部分接口有更新标志；旧值可能被修订值覆盖 | 平台 update_time/ann_date 含义各异，不等于官方 known_at | 可捕获 API JSON，但不是上游 exact raw bytes | 中高：个人、非转让、非商业等协议限制；机构另议 | 积分/年费与每分钟、每日额度随方案变化 | T1 | 上游 provenance、修订旧版本、完整性和许可不能外推 | **corroboration / fallback** |
| AKShare / 开源项目 | secondary wrapper | 多公开站点的列表、状态、指数、公司行为接口 | 随上游接口而异，无统一保证 | 通常不保留上游历史版本 | 依上游展示字段；包装时间不等于来源发布时间 | 可保存包装结果；默认不等于保存上游 response bytes | 高：MIT 只覆盖代码，不授予底层数据权利；项目文档提示商业风险 | 软件免费；上游可能限流/变更 | T1 | 接口漂移、二次清洗、来源条款、无完整性保证 | **fallback** |
| 东方财富公开网页/未文档化接口 / 东方财富 | secondary commercial portal | 行情、列表、板块、公告聚合 | 端点各异，历史口径不稳定 | 未公开 revision schema | 返回时间与来源发布时间常混合 | 技术上可捕获，合同与 schema 不稳定 | 高：未找到面向本用途的正式数据许可 | 免费页面；无稳定公开 API 频率/SLA | T1 | 非权威、板块定义可变、无 PIT 完整性、许可不清 | **reject as authority / fallback only** |

## 5. 分域审计

### 5.1 SSE/SZSE 交易日历、休市、临时调整及历史修订

**官方证据：**

- SSE 2025 年休市安排发布于 **2024-12-23**，明确每段休市及恢复交易日期（[SSE 2025 通知](https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml)）；2023 年安排发布于 **2022-12-27**（[SSE 2023 通知](https://www.sse.com.cn/disclosure/announcement/general/c/c_20221227_5714458.shtml)）。SSE 另有年度/节假日通知档案（[休市安排列表，访问于 2026-08-14](https://www.sse.com.cn/disclosure/dealinstruc/closed/list/)）。
- SZSE 2024 年安排发布于 **2023-12-26**（[SZSE 通知](https://www.szse.cn/disclosure/notice/t20231226_605108.html)），2025 年安排发布于 **2024-12-23**（[SZSE 通知](https://investor.szse.cn/disclosure/notice/general/t20241223_611283.html)），2026 年安排发布于 **2025-12-22**（[SZSE 通知](https://investor.szse.cn/disclosure/notice/general/t20251222_618087.html)）。
- 单个节假日仍会有后续通知；例如 SZSE 2025 年春节通知发布于 **2025-01-23**（[通知](https://www.szse.cn/disclosure/notice/general/t20250123_611741.html)）。年度安排不能阻止后续临时调整覆盖它。
- 交易规则允许交易所因异常情况采取技术性停牌或临时停市并公告恢复，因此 Calendar 不能只生成法定节假日；历史规则档案示例见 SZSE **2006-05-15** 公布的规则文本（[历史通知](https://www.szse.cn/disclosure/notice/general/t20060515_499577.html)）。正式实现必须绑定当时有效规则版本，不能把该旧文本当作当前规则。

**审计结论：**

- 年度公告 + 每个节日/临时公告均作为独立不可变 artifact；后发公告以显式 `supersedes` 或日期级 override 关系关联，不覆盖旧 raw。
- 只凭年度安排可以得到计划 Calendar，不能声称已包含临时停市/技术停市修订。
- 两所通知页多只显示发布日期。若响应头没有可验证的发布时间，按 `DATE` 粒度保存，并对日内研究失败关闭。
- 免费公开源可支撑日级 T2 Calendar 候选；要到 T3，还需逐日覆盖报告、两所交叉核对、临时公告监听和规则版本冻结。

### 5.2 上市日、退市日、证券类型、交易所、代码变更

- SSE 当前股票下载页面包含代码、简称、上市日期等字段，但无历史版本标识（[SSE 股票列表](https://www.sse.com.cn/assortment/stock/list/share/)）。浏览器于 2026-08-14 观察到其下载实现使用 `commonExcelDd.do` 查询；该 URL 是网页实现，不是对外稳定 API 合同。
- SZSE 当前列表包含板块、A 股代码/简称、上市日期、股本和行业等当前字段（[SZSE 股票列表](https://www.szse.cn/market/product/stock/list/index.html)）。页面内观察到 `CATALOGID=1110`，但直接重放返回维护/错误页，故禁止把猜测的 `ShowReport` 参数冻结为稳定 endpoint。
- 上市和终止上市事实应来自逐条交易所公告。SSE 现行主板上市规则修订发布并施行于 **2026-04-24**，规定退市、重新上市、风险警示及名称变更公告义务（[SSE 规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/mainipo/c/c_20260424_10816589.shtml)）。
- 代码区间本身也修订。SZSE 技术服务页列出 **2024-12-12、2025-12-31、2026-03-06** 等版本（[SZSE 技术服务](https://www.szse.cn/marketServices/technicalservice/)）；SSE 代码指南修订发布并施行于 **2026-07-13**（[SSE 指南](https://www.sse.com.cn/lawandrules/guide/stock/jyglywznylc/zn/c/c_20260713_10825354.shtml)）。Parser 必须版本化。

身份不能以 `symbol` 作为永久主键。建议以 `source_security_id + exchange` 为稳定事实键，并维护 `symbol/name/security_type/board` 的有效区间。对代码变更，必须保留旧代码、公告日、生效日和新代码；找不到实施公告时不得推测连续性。

### 5.3 停牌、复牌与临时停牌历史

- SZSE 停复牌公告索引按发布日期保存临时停牌通知（[SZSE 停复牌索引](https://www.szse.cn/disclosure/notice/temp/index_1.html)）。
- 更强的官方历史证据是月度停复牌表：**2021-08** 文件含 `Susp.Time`、`Resume.Time` 及原因（[SZSE 2021-08 月表](https://docs.static.szse.cn/www/market/periodical/month/W020210907547542494089.html)）；**2023-06**（[月表](https://docs.static.szse.cn/www/market/periodical/month/W020230710387876992911.html)）和 **2024-04**（[月表](https://docs.static.szse.cn/www/market/periodical/month/W020240513367375059769.html)）保留相同语义。官方档案示例还可追到 **2003-08**（页面发布于 2004-12-29，[历史月表](https://www.szse.cn/market/periodical/documents/t20041229_522001.html)）。
- 这些表同时表明 `9999` 等特殊值需要显式字典，不能强制解析成时钟时间。
- SZSE 数据文件接口规范 `Ver1.41`（2025 年 3 月）把停牌列为证券状态，并同时定义 ST、*ST、退市整理等状态（[PDF](https://www.szse.cn/www/marketServices/technicalservice/interface/P020250328367238567039.pdf)）。更早的 **2006-08-02** 规范通知已区分长期停牌 `Y` 与盘中暂停 `H`，并增加记录更新时间（[SZSE 历史通知](https://www.szse.cn/disclosure/notice/general/t20060802_499651.html)）。这证明日级布尔 `suspended` 不足以表达盘中状态。
- SSE 有证券停复牌公告入口（[SSE 股票停复牌，访问于 2026-08-14](https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/)）及月报档案（[SSE 月报](https://www.sse.com.cn/aboutus/publication/monthly/documents/)），但本轮未找到与 SZSE 月表同等、已证实无缺口的免费全历史机器表。

T3 需要 `(effective_start, effective_end, trading_state, reason)`；只保存“当天停牌”会误处理 09:30-10:30、午后恢复等临停。SZSE 月表历史连续性和 SSE 等价数据仍需另行覆盖审计。

### 5.4 ST、*ST、风险警示与退市整理状态历史

- SSE 现行上市规则于 **2026-04-24** 发布/施行，明确 `*ST` 是退市风险警示、`ST` 是其他风险警示（[SSE 规则，第九章](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/mainipo/c/c_20260424_10816589.shtml)）。SSE 风险警示板同时包含风险警示股票和退市整理股票（[风险警示板，访问于 2026-08-14](https://www.sse.com.cn/disclosure/listedinfo/riskplate/)）。
- SSE 交易规则修订同日发布，规定风险警示板/退市整理交易规则（[SSE 交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)）。
- SZSE 接口规范状态码包括 `4-ST`、`5-*ST`、`10-退市整理期`、`18-退市整理期首日`（[Ver1.41 PDF](https://www.szse.cn/www/marketServices/technicalservice/interface/P020250328367238567039.pdf)）。规范证明字段语义，不证明公众可免费取得每天历史文件。

状态历史必须来自：实施/撤销公告 + 当日证券状态文件或月表 + 名称变化。不得从简称是否含 `ST` 反推整个历史，因为名称变化可能晚于事实、网页可能回填当前简称，而且风险警示、退市整理和停牌是可重叠的独立维度。

### 5.5 完整历史 A 股 Universe，特别是退市样本

建议将 SSE、SZSE 分别维护 `A_SHARE_SSE_ALL`、`A_SHARE_SZSE_ALL`，再在 Manifest 中并集；每个交易日每个身份必须明确为 `INCLUDED` 或 `EXCLUDED` 并给理由。

最小重建流程：

1. 抓取现存 A 股列表作为今日锚点，强制 `complete=false`、`verified=false`；
2. 反向摄取全部上市、重新上市、终止上市、证券类别/板块变更公告；
3. 以 SSE/SZSE 退市列表和 ChinaClear 终止登记公告逐只核对；
4. 以月报/年报的期初、期末、上市和终止数量做总量连续性校验；
5. 对每个交易日连接 Calendar、identity interval 和 status interval；任何缺日、重叠身份、未解释退出或数量差异均失败关闭；
6. 输出 coverage report，包括最早日期、缺口日期、未解析公告、未闭环证券和跨源冲突。

本轮未证明 SSE/SZSE 公开退市页自市场开市以来无缺口，也未证明退市后板块的所有历史标的都可免费批量取得。因此当前只能设计重建与验证流程，不能宣称完整历史 Universe 已获得。

### 5.6 指数、行业和板块历史成分的 PIT 可得性

**指数：**

- 中证指数股票指数维护细则 `V13.1` 发布于 **2023-09**，描述定期与临时调整、长期停牌等处理（[CSI 维护细则 PDF](https://oss-ch.csindex.com.cn/notice/20230908165124-%E3%80%8A%E4%B8%AD%E8%AF%81%E6%8C%87%E6%95%B0%E6%9C%89%E9%99%90%E5%85%AC%E5%8F%B8%E8%82%A1%E7%A5%A8%E6%8C%87%E6%95%B0%E8%AE%A1%E7%AE%97%E4%B8%8E%E7%BB%B4%E6%8A%A4%E7%BB%86%E5%88%99%E3%80%8B.pdf)。方法文件证明成分可能在定期调样外临时变化。
- CSI 数据服务页列明收盘行情、成分权重、公司行为、除数等静态数据通过 CSI 数据服务平台或授权信息商取得（[CSI 数据服务，访问于 2026-08-14](https://www.csindex.com.cn/#/dataService/indexData)）。公开当前成分查询不是免费历史 PIT 数据合同。
- SZSE/国证指数调样公告可重建公开事件：2019 年调样公告发布于 **2019-12-02**、**2019-12-16** 实施（[公告](https://www.szse.cn/disclosure/notice/general/t20191202_572315.html)）；2026 年调样公告发布于 **2026-05-29**、**2026-06-15** 实施（[公告](https://www.szse.cn/disclosure/notice/t20260529_620819.html)）。国证公告档案可作为索引（[CNI 公告索引](https://www.cnindex.com.cn/zh_information/notices_news/?act_menu=2)）。

**行业：**

- 证监会历史行业分类结果页保存 2017-2021Q3 等季度结果（[CSRC 历史结果](https://www.csrc.gov.cn/csrc/c100103/common_list.shtml)）。
- CAPCO 于 **2023-05-21** 发布、**2023-05-01** 起施行的指引改为每半年发布上市公司行业分类结果（[CAPCO 指引](https://www.capco.org.cn/xhdt/tzgg/202305/20230521/j_2023052117544500016846630061707656.html)）。结果档案现列多期文件（[CAPCO 结果索引](https://www.capco.org.cn/xhgg/hyfl/hyfljg/index.html)），例如 2024H2 结果发布于 **2025-04-18**（[结果](https://www.capco.org.cn/xhgg/hyfl/hyfljg/202504/20250418/j_2025041815003000017449597508305299.html)），2025H1 发布于 **2025-09-30**（[结果](https://www.capco.org.cn/xhgg/hyfl/hyfljg/202509/20250930/j_2025093014371200017592143530187679.html)），2025H2 发布于 **2026-04-03**（[结果](https://www.capco.org.cn/xhgg/hyfl/hyfljg/202604/20260403/j_2026040315001700017751997384265508.html)）。

**结论：** 指数调样事件和监管行业期次可按公开原件重建日级 PIT 候选，但完整权重、临时调整覆盖和旧版本仍需审计/授权；免费官方概念、主题、申万等商业板块的完整历史 PIT 未找到。二级板块标签不得冒充官方，也不得自动用于 T3。

### 5.7 分红、送转、拆并股、配股、增发等公司行为

公司行为必须分阶段保存，不能把预案当成已实施：

```text
PROPOSED -> APPROVED -> IMPLEMENTATION_ANNOUNCED -> EX_DATE/RECORD_DATE -> COMPLETED
                                      \-> CANCELLED/CORRECTED
```

- CNINFO 首页将自身说明为深交所法定信息披露平台，并展示公告及附件（[CNINFO，访问于 2026-08-14](https://www.cninfo.com.cn/new/index)）。它可提供法定披露原文，但该身份不等于一个已证明无缺口的规范化公司行为数据库。
- SSE 月报“上市公司”栏目公开送股、配股等字段，包括登记日、送股比例、配股比例/价格、转增比例、除权日和股份上市日（[SSE 月报公司数据](https://www.sse.com.cn/aboutus/publication/monthly/company/)）。它适合对账，不应替代逐条实施公告。
- SSE 权益分派实施公告实例发布于 **2026-06-22**，明确登记日 **2026-06-25**、除权除息/现金红利发放日 **2026-06-26**（[SSE PDF](https://star.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-06-22/688247_20260622_H9LP.pdf)）。
- SZSE 权益分派实施公告实例发布于 **2026-04-23**，明确登记日 **2026-04-29**、除权除息日 **2026-04-30**（[SZSE/CNINFO PDF](https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-04-23/328d991b-5a47-46b4-b65b-fa334d0f843b.PDF)）。
- SSE 公告格式指南修订发布并施行于 **2026-04-24**，包含权益分派实施/结果、名称变更和更正等公告格式（[SSE 指南](https://www.sse.com.cn/lawandrules/guide/stock/zbxxpljg/ssgszljg/c/c_20260424_10816611.shtml)）。这说明更正与后补版本必须进入 revision chain。

拆并股、送转、配股、公开/非公开增发应分不同 action type；每条必须保存比例/价格的原单位、登记日、除权日、到账/上市日、适用证券和公告版本。若只有董事会预案，没有实施公告，执行模拟不得调整股数或价格。公开源未证明可提供所有早期公司行为的无缺口机器表，因此仍不能到 T3。

### 5.8 exact raw bytes、发布时间、`known_at`、`usable_from` 和 revision

**能捕获 exact raw bytes 的对象：** 静态公告 HTML、PDF、XLSX、月报/年报附件，以及动态查询的原始 HTTP response。捕获必须发生在解码、解压或字段规范化之前，并同时保存 URL、method、query/body、HTTP status、`Content-Type`、`Date`、`ETag`、`Last-Modified`（若有）和 UTC `retrieved_at`。

**不能把下列值混为一谈：**

- 网页展示日期：来源声明的日期；
- HTTP `Date`：服务器响应时间，不一定是内容发布时间；
- `retrieved_at`：本系统获取时间；
- `known_at`：研究主体最早可证明获得该信息的时间；
- `effective_from`：事实开始生效；
- `usable_from`：在避免前视偏差后允许进入特征/Universe 的时间。

同一 URL 返回不同 bytes 时必须新建 artifact。静态附件 URL 也不能假定永不替换；只有定期重抓、hash 差异和响应元数据才能发现静默替换。若来源只提供“最新修订值”，历史旧值在首次捕获前已经丢失，则 `revision_history=PARTIAL`，相关历史区间不能升 T3。

### 5.9 API、下载频率、robots、服务条款、再分发和本地长期保存

#### 5.9.1 公开网页与 API 边界

- SSE 当前列表的 Excel 下载实现可被观察，但未找到面向公众的稳定 API 版本、限频或 SLA；只能冻结“来源页面 + 本次实际请求元数据”，不能把内部 `sqlId` 当长期合同。
- SZSE 页面观察到报表 catalog 参数，但直接重放未稳定成功；禁止 Agent C 依赖猜测 endpoint。应优先保存静态公告和附件，动态报表须先取得正式接口/许可确认。
- CNINFO 搜索与附件可访问，但未找到覆盖本用途的公开、版本化 API 合同和限频承诺。
- Tushare 公布积分对应的每分钟/每日额度（[积分与频率说明，访问于 2026-08-14](https://tushare.pro/document/1?doc_id=290)），但它是二级服务；其服务协议限制个人、非转让、非商业等使用（[数据服务协议](https://tushare.pro/document/1?doc_id=405)）。

#### 5.9.2 robots 实测

2026-08-14 对站点根路径做只读 HTTP 探测：

| URL | 结果 | 审计含义 |
|---|---|---|
| `https://www.sse.com.cn/robots.txt` | HTTP 404 | 没有肯定的机器人许可；不等于允许抓取 |
| `https://www.szse.cn/robots.txt` | HTTP 200、空正文 | 没有可执行 allow/disallow 指令；不等于许可 |
| `https://www.cninfo.com.cn/robots.txt` | HTTP 404/错误页 | 没有肯定许可 |
| `https://www.csindex.com.cn/robots.txt` | HTTP 200，但返回 SPA HTML 而非 robots 指令 | 不能解释为许可 |

`robots.txt` 是爬虫协作机制，不是著作权、数据库权、合同许可或再分发授权。没有禁止也不构成允许。

#### 5.9.3 条款与授权风险

- SSE 网站法律声明允许一般非商业浏览/下载，但知识产权、复制、存储、电子抓取、传输、再发布及商业用途存在限制；页面未显示明确生效日期（[SSE 法律声明，访问于 2026-08-14](https://www.sse.com.cn/home/legal/)）。
- SZSE 法律声明有相近限制，且未见明确生效日期（[SZSE 法律声明，访问于 2026-08-14](https://www.szse.cn/application/laws/index.html)）。
- SSE/SZSE 证券信息经营联合授权声明日期为 **2003-12-24**，将交易所产生的证券信息使用/经营纳入授权体系（[联合授权声明](https://www.sseinfo.com/aboutus/authstatement/)）。公开网站非商业浏览条款与批量长期机器化保存之间如何衔接，必须书面确认。
- CSI 免责声明发布日期 **2020-02-15**，对网站内容的存储、电子抓取、传输、再发布和未经许可复制设有限制（[CSI 免责声明](https://www.csindex.com.cn/#/disclaimer)）；完整静态数据另走数据服务/授权渠道。
- 深圳证券信息历史增强行情产品声明自 **2008-01-01** 起提供订单、成交、快照、证券信息/状态等并允许按授权下载保存（[产品页](https://www.szsi.cn/cpfw/fwsq/hq/yw-2.htm)），但非展示自用、系统/地点和再分发受授权流程约束（[授权流程](https://www.szsi.cn/cpfw/fwsq/hq/sqlc-3.htm)）。
- 上证信息公司提供历史行情、基本资料和公告数据等产品（[历史数据服务](https://www.sseinfo.com/services/assortment/historical/)），官网价格页显示历史 L1、日 K、分钟等为收费项（[价格页，访问于 2026-08-14](https://www.sseinfo.com/services/cpfwjg/)），不满足接近零成本目标。
- AKShare 的 MIT 许可证只覆盖代码，不自动授权底层来源数据；项目文档提示公开源研究与商业使用风险（[AKShare 项目](https://github.com/akfamily/akshare)、[说明](https://github.com/akfamily/akshare/blob/main/docs/introduction.md)）。

因此，在许可确认前只允许小规模审计性捕获和内部研究原型；不得把 raw 数据打包入 Git、公开发布、向第三方提供或声称获得再分发权。

### 5.8 HiThink Financial-API 的当前接入边界

仓库已增加 `stock_tracker/collector/hithink_finance.py` 与 `scripts/capture_hithink_bars.py`。Adapter 只允许官方 HTTPS Origin、A 股 `1d` 历史接口、单标的最长十年窗口和 exact raw JSON 留存；Key 只从 `HITHINK_FINANCE_API_KEY` 环境读取，不写入配置、日志或 Artifact descriptor。

该接入故意不参加 Runtime Quote/Snapshot/Bar 路由，且构造器强制：

```text
read_only = true
trust_tier = T1_BEST_EFFORT
allow_live_decision = false
allow_model_training = false
allow_public_redistribution = false
```

下一证据切片应使用真实 Key 做小窗口活体验收，再执行 50—100 个代表性标的与 Eastmoney/交易所事实的差异矩阵；全市场多年回填应评估官方 Market Dumps/marketdb，而不是逐标的循环 REST。详见 `docs/HITHINK-FINANCE-INTEGRATION.md`。

## 6. 结论与实施冻结项

### A. 推荐 Source Stack

| 层 | 推荐来源 | 用途 | 失败关闭条件 |
|---|---|---|---|
| Primary 1 | SSE/SZSE 年度与临时公告、上市/终止上市/风险状态/停复牌公告、月报/年报 | Calendar、identity/status 事件、公司行为实施、总量对账 | 缺页、分页不明、发布日期粒度不明、公告与月报冲突 |
| Primary 2 | CAPCO/CSRC 行业分类结果；CSI/CNI 调样公告 | 行业期次、指数成分变更事件 | 无生效日、漏期、临时调样未覆盖、许可未确认 |
| Corroboration 1 | ChinaClear 终止登记公告、退市后板块衔接证据 | 退市样本闭环 | 交易所与登记结算日期/代码冲突 |
| Corroboration 2 | CNINFO 法定披露原文、交易所月报/统计数据 | 公司行为、名称/状态实施公告、数量连续性 | 只有预案、附件被换、搜索覆盖不明 |
| Corroboration 3 | Tushare Pro 小规模查询 | 发现漏项、字段交叉核对 | 与官方冲突时以冲突状态失败，不自动选边 |
| Fallback | AKShare/东方财富 | 临时发现、可用性降级 | 绝不作为 T3 原料或权威完整性证据 |
| Licensed fallback | 上证信息、深圳证券信息、CSI 授权数据 | 当公开档案覆盖不足且预算/许可获批时补齐 | 合同未明确本地长期保存、修订重发、研究衍生和审计留存 |

当前推荐目标不是“立即 T3”，而是先生成带明确缺口的 T2 evidence bundle；许可与历史覆盖完成后，再由独立校验器决定能否晋级。

### B. 不推荐来源与理由

- **今天的 SSE/SZSE 股票列表作为历史 Universe：reject。** 只做今日锚点，天然有幸存者偏差和历史状态丢失。
- **东方财富公开/未文档化接口作为权威源：reject。** 非一手、schema/限频/修订/许可不稳定；只可 fallback。
- **AKShare 结果作为 exact raw：reject。** 它是包装/清洗结果，MIT 不覆盖底层数据权利。
- **Tushare 单源设 `verified=true`：reject。** 二级规范化便利不等于上游原始修订和历史覆盖。
- **CSI/CNI 当前成分查询直接回填历史：reject。** 需要调样事件或授权历史静态数据。
- **仅按简称识别 ST/*ST/退市整理：reject。** 名称不是独立状态合同，无法表达重叠状态和盘中变化。
- **仅保存 hash 后删除原响应：reject。** 无 raw bytes 就无法重解析、审计来源替换或证明字段提取。
- **把公司行为预案当实施：reject。** 必须等实施公告的登记日/除权日/股份上市日。

### C. Agent B/C 必须冻结的 endpoint、文件格式和 schema

#### C.1 Agent B：Calendar

**冻结来源族，而不是脆弱页面实现：**

```text
SSE archive:
  https://www.sse.com.cn/disclosure/dealinstruc/closed/list/
SSE detail family:
  https://www.sse.com.cn/disclosure/announcement/general/c/c_<id>.shtml

SZSE notice/detail families:
  https://www.szse.cn/disclosure/notice/general/
  https://www.szse.cn/disclosure/notice/general/tYYYYMMDD_<id>.html
  https://www.szse.cn/disclosure/notice/tYYYYMMDD_<id>.html
```

允许格式：原始 `text/html`、链接的 PDF/DOCX/XLSX，各自独立 artifact。HTTP redirect 前后 URL 都保存。禁止只保存解析后的日期数组。

```text
CalendarFact
  exchange: SSE | SZSE
  civil_date: YYYY-MM-DD
  session_state: OPEN | CLOSED | PARTIAL | UNKNOWN
  session_open_at: datetime[Asia/Shanghai] | null
  session_close_at: datetime[Asia/Shanghai] | null
  notice_id: str
  notice_type: ANNUAL | HOLIDAY | TEMPORARY | TECHNICAL | REVISION
  source_published_at: datetime-or-date
  source_published_granularity: DATE | SECOND | UNKNOWN
  known_at: aware datetime
  usable_from: aware datetime
  effective_from: aware datetime-or-date
  effective_to: aware datetime-or-date | null
  revision_id: str
  supersedes_revision_id: str | null
  source_uri: str
  raw_artifact_id: str
  parser_version: str
```

年度公告与单独节假日/临时公告均摄取；后者只能通过显式规则覆盖。`verified`、`complete` 不属于 Adapter 输入或输出事实字段。

#### C.2 Agent C：Security Identity / Status / Universe

**冻结公开来源：**

```text
SSE current A-share page:
  https://www.sse.com.cn/assortment/stock/list/share/
SSE observed current download implementation (provisional, not stable API):
  https://query.sse.com.cn/sseQuery/commonExcelDd.do
  sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L
  STOCK_TYPE=1
  COMPANY_STATUS=2,4,5,7,8

SSE suspended/delisted page:
  https://www.sse.com.cn/assortment/stock/list/delisting/
SSE company listing/delisting disclosures:
  https://www.sse.com.cn/disclosure/listedinfo/

SZSE current stock page:
  https://www.szse.cn/market/product/stock/list/index.html
SZSE listing/delisting announcements:
  https://www.szse.cn/disclosure/notice/company/
SZSE suspension announcements:
  https://www.szse.cn/disclosure/notice/temp/index_1.html
SZSE monthly statistics root:
  https://www.szse.cn/market/periodical/index.html
```

SSE 下载参数来自 2026-08-14 网页观察，只能作为 provisional capture recipe；一旦页面实现、字段或响应类型变化，必须新建 endpoint version。SZSE `CATALOGID=1110` 只作为观察线索，**不得**冻结为稳定 API，直至官方文档或经许可的可重复请求确认。

允许 raw 格式：HTML、PDF、XLS/XLSX、DBF/TXT（如授权取得）、JSON response bytes。压缩包和内部成员分别有 artifact identity；不得把解析后的 CSV 当 exact raw。

```text
InstrumentIdentityFact
  source_security_id: str
  exchange: SSE | SZSE
  symbol: str
  name: str
  market: CN
  security_type: str
  board: str | null
  listed_on: date | null
  delisted_on: date | null
  effective_from: aware datetime-or-date
  effective_to: aware datetime-or-date | null
  source_published_at / source_published_granularity
  known_at / usable_from
  revision_id / supersedes_revision_id
  source_uri / raw_artifact_id / parser_version

SecurityStatusFact
  source_security_id: str
  effective_start: aware datetime
  effective_end: aware datetime | null
  listing_state: PRELISTED | LISTED | SUSPENDED_LISTING | DELISTED | UNKNOWN
  trading_state: TRADING | SUSPENDED | TEMP_SUSPENDED | RESUMED | UNKNOWN
  risk_designation: NONE | ST | STAR_ST | OTHER_RISK | DELISTING_PERIOD | UNKNOWN
  reason_code: str | null
  source fields and revision fields as above

UniverseMembershipFact
  universe_id: A_SHARE_SSE_ALL | A_SHARE_SZSE_ALL
  source_security_id: str
  state: INCLUDED | EXCLUDED
  effective_from: date
  effective_to: date | null
  reason: LISTED | RELISTED | DELISTED | TYPE_CHANGE | OUT_OF_SCOPE | UNKNOWN
  evidence_ids: list[str]
  revision_id: str
```

每个构建必须另产 `CoverageReport`：期望/实际日期数、证券数、缺失页、未解析附件、未闭环退市、跨源冲突和 Universe 连续性差异。只有校验层可以基于该报告生成外部 `complete/verified` 决定。

#### C.3 通用 RawArtifact / Manifest

```text
RawArtifact
  artifact_id
  sha256
  byte_length
  media_type
  source_owner
  request_method
  request_url
  request_params_or_body_digest
  response_status
  response_headers_selected
  retrieved_at_utc
  source_published_at
  source_published_granularity
  license_snapshot_uri

Manifest
  dataset_id
  calendar_version
  universe_version
  status_version
  corporate_action_version
  source_artifact_ids
  parser_versions
  coverage_report_id
  unresolved_conflicts
  trust_tier
```

禁止将 `sha256`、`dataset_id` 或 `revision_id` 作为 Trust Tier 的替代证明。

### D. 仍无法达到 T3 的缺口

1. SSE/SZSE 自开市以来的上市、退市、暂停上市、恢复上市和代码/类型变化公告尚未证明零缺口。
2. SSE 免费公开渠道尚未找到与 SZSE 月度停复牌表等价的、全历史、含盘中起止时间的机器表；SZSE 月表本身也未完成逐月连续性审计。
3. 公开当前列表没有历史修订；早于首次本地捕获的静默更正无法恢复。
4. 完整历史 A 股 Universe 的退市样本尚未逐只完成“交易所决定/实际退市 + ChinaClear 终止登记 + 后续板块”闭环。
5. CSI/CNI 全量历史成分、权重、临时调样和修订链未获免费授权数据；官方主题/概念板块历史 PIT 未找到。
6. CAPCO/CSRC 行业结果的制度切换和每期覆盖需做无缺口审计；单公司分类的精确生效时点常不可得。
7. 分红、送转、拆并股、配股、增发的早期全量实施事件和取消/更正链尚未证明完整。
8. 公告只给日期时，日内 `known_at`/`usable_from` 不可证明；对应日内 Replay 必须禁用。
9. 官网附件可能静默替换；首次捕获之前的旧 bytes 无法补救。
10. 最关键的许可问题尚未书面确认，因而即使技术覆盖完成也不能宣称 T3 可长期合法复现。

### E. 需要人工确认的许可问题

请由项目所有者/法务向 SSE、SZSE/深圳证券信息、ChinaClear、CNINFO、CSI/CNI 分别书面确认：

1. 是否允许自动化、低频、可识别 User-Agent 的批量抓取；允许的 QPS、每日量、并发、重试和访问窗口是什么；
2. 是否允许为内部非商业量化研究长期保存 exact raw bytes、响应头、附件旧版本和 hash；保存期限是否有限；
3. 是否允许从原始公告/数据生成规范化事实、特征、回测结果和审计摘要；这些是否被视为衍生数据；
4. 是否允许在团队/云环境备份、灾备和跨设备复制；“指定系统/地点”是否适用；
5. 是否允许把 raw、规范化数据、Manifest、少量样例或错误复现材料提供给外部审计人/开源仓库；若不允许，哪些元数据可公开；
6. 公告、列表、月报、技术数据文件和指数数据分别适用哪个许可，网站“非商业浏览下载”与证券信息经营授权声明冲突时以何者为准；
7. 数据更正/重发时是否提供 revision 通知、旧版本、撤销记录和稳定 ID；
8. 历史数据产品是否包含退市证券、证券状态、代码/名称变更、公司行为和全部历史 Universe，能否在合同中写明覆盖起点和完整性 SLA；
9. CSI/CNI 指数成分、权重、除数和公司行为是否允许用于内部模型训练/回测，是否需要单独指数数据许可证；
10. 二级服务（Tushare 等）的个人/非商业许可是否覆盖本项目使用，及其上游数据再许可链是否完整。

许可确认前的默认策略是：内部、小规模、可审计、限频、无再分发；任何不确定项失败关闭。

## 7. 官方引用与访问记录

本报告的关键正式来源已在正文就近链接。为便于复核，按所有者汇总如下：

- **SSE**：休市公告与档案、股票/退市列表、上市/交易规则、风险警示板、停复牌入口、月报、代码指南、公告格式指南与公司行为实施公告。已引用页面的发布日期/施行日期见正文；无日期的列表/法律页统一标注“访问于 2026-08-14”。
- **SZSE**：年度/节假日安排、上市/终止上市和停复牌公告、月度停复牌表、接口规范、代码区间版本、指数调样、股票列表和统计档案。正文给出 2003-08 月表至 2026 公告的代表性证据，但不把代表性样本等同于全历史覆盖。
- **ChinaClear**：终止 A 股登记服务公告及 2022-04-29 退市衔接联合规则。
- **CSRC/CAPCO**：行业分类规则与 2017-2026 多期结果索引。
- **CSI/CNI**：指数维护规则、调样公告、静态数据服务和知识产权声明。
- **CNINFO**：法定披露 PDF 原件，作为实施公告 corroboration，不作为已验证完整数据库。
- **官方授权数据公司**：上证信息、深圳证券信息的历史数据产品、授权与价格页。

访问记录说明：所有在线结论均在 2026-08-14 复核；网页可能在之后修改。文档不内嵌网页全文，也不声称链接永久可用。正式采集必须将每次 exact response 固化为 RawArtifact，并单独快照当时适用的许可页面。
