# 文档解析 revision 管线 — 实施清单（未授权执行）

Status: planning only. 本文件是未来实施的任务拆分，不是执行授权。不得据此修改代码、部署服务、迁移数据或发布。文档提交不等于实施授权。

## 建议先实施的 PDF 切片（待批准）

在独立开发分支和隔离工作目录内增加默认关闭的 PDF revision 线路。先接固定版本的 `docreader builtin` 和可注入的视觉客户端；正式配置须通过文档送模权限检查。先不接生产入库、不替换当前 PDF Pipeline、不更新现有条目的 active 指针。

1. 从原件逐页分流。仅给视觉分支发送需识别且获准的图；新线路的原生页由 docreader 抽取，旧直读线路保持不变。
2. 原子写入并校验隔离档案：`manifest.json`、`content.md`、`source-map.jsonl` 和已引用资产。最终 Markdown 写定后再计算范围。
3. 在明确指定的测试 revision 上做字面命中、定版精读和原页回读。验证第二版发布到测试档案后，第一版的范围仍可回读；错版本或无访问权的请求不返回证据。
4. 用固定混合 PDF 验证仅扫描页送图、六处文字回链、扫描文字不重复。补一个视觉失败用例，确认缺页显式记录，不将 `partial` 当作完成。自动测试使用冻结的模型应答；真实模型复验另限获准样本。

首个切片完成不了生产 Agent 引文或自动激活验收：后续再接实际搜索、精读及带版本的最终引用。模块 4、复杂版面自动切换、DeepDOC 基线和全文索引不挡此切片，但仍是各自阶段的验证项。

## 完整接入前置条件

- [x] 模块 3 首期检索范围已确认；最终接口和性能选型未定。
- [ ] 模块 4（前端查看）完成决策。
- [ ] W/L 对比环境和固定版本已确定。
- [ ] 现有未提交改动已由维护者处理，避免与新改动混合。
- [ ] 解析器许可证和模型许可证已核对。
- [ ] 用户明确授权进入 implementation。

## 任务 1：revision 档案契约

- 定义 `manifest.json`、`source-map.jsonl`、`asset`、`table` 的 schema。
- 实现 revision 目录创建、校验和只读发布。
- 提供按 `revision_id` 读取 manifest、Markdown、source map 和 asset 的内部接口。
- 不新增公共 Block 表。

验收：单份 PDF/DOCX 能生成并通过 schema 校验。

## 任务 2：parser adapter 接口

- 定义 `ParsedDocument` 和 adapter 协议。
- 先接 Library 原生 PDF/DOCX 解析器。
- 统一字符偏移、asset_id、table_id 和 locator。
- 适配器不得写 FTS、summary 或业务表。

验收：同一份原件可由两个 adapter 生成可比较的 revision。

## 任务 3：混合页与覆盖率

- 实现 PDF 页面文字质量探测。
- 按页选择原生文字、版面或直接读图。混合页只送需要视觉识别的区域。
- 接入可配置的视觉模型，首期优先接入 `deepseek-v4-flash`。发图前执行文档送模策略；PaddleOCR、MinerU 不做前置步骤。
- 合并 Markdown，重算 source map 偏移；原文与视觉转录的来源分别记录。
- 记录页级 engine、coverage、failed 和 partial；不可辨认、截断及未授权不能标记为已完成。

验收：扫描页转录能回到原页；文字层 PDF 不必送图；混合页保留原生文字并补读目标区域；失败和缺页可见。

## 任务 4：W 对比接入

- 固定 WeKnora/docreader builtin 版本。接原生文字、基础版面、真实页图和定位，不接 WeKnora Go OCR/VLM。
- 接入任务 3 的 Library 视觉分支。扫描页只归档一份正文；混合页保留原生文字并仅补缺口。
- 将输出转换为 revision，记录未覆盖的区域、无法映射的字段和失败样本。
- 用双栏、复杂表同页对照 Docling；只比较实际有证据的顺序、网格和定位，不启用未经验证的自动切换。
- 不接 WeKnora Knowledge/Chunk 数据库。

验收：固定样本能回读 W 原生输出和视觉转录；简单文字页不送图，扫描页能回原页，混合页不重复；双栏或表格不合格显式标记。

## 任务 5：L 对比接入

- 固定 Docling Serve/Core 版本。
- 将 Markdown、Docling JSON、图片 ZIP 和 provenance 转换为 revision。
- 核对旧 Office 转换和 checkpoint 边界。
- 不接 LinkSense workspace/ES。

验收：L 能在固定样本上生成完整 revision，并附原始输出。

## 任务 6：R PDF 基线

- 固定 RAGFlow Go v1.0.0/DeepDOC 版本。
- 对同一批 PDF 记录 OCR、版面、表格、图片和 page/position。
- 只做质量基线，不接完整 ingestion。

验收：R 的 PDF 结果可与 W/L 按页对比。

## 任务 7：summary/sections

- 基于 `content.md` 和 source map 生成 summary/sections/description/tags。
- 支持重新生成而不重新解析。
- 保留 prompt、模型和输入 revision 记录。

验收：更换 prompt 后不触发重新解析。

## 任务 8：独立正文搜索与查询契约

- 提供字面匹配和受控正则；不依赖摘要先命中。是否另建正文 FTS5 索引由评测决定。
- 定义 SearchHit：document、revision、content range、source map、locator、assets。
- 可选内部 segment，必须可重建。

验收：查询命中可打开正确阅读范围和原件位置。

## 任务 9：查看与 Agent

- Markdown/HTML 图文阅读。
- asset API 和 Blob URL。
- 复杂表格渲染。
- PDF 页跳转；Office sheet/slide/paragraph。
- Agent 按 content range 或页/表/幻灯片读取。

验收：阅读、图片、表格、原件核验使用同一 revision。

## 任务 10：切换与回退

- 按格式/目录配置新旧线路。
- 解析失败或质量不足时回退旧线路。
- 不覆盖旧 active revision。
- 记录切换原因和版本。

验收：关闭新线路后旧线路仍可工作。

## 全局约束

- 不修改原件和公盘。
- 不引入完整 WeKnora/RAGFlow/LinkSense 应用。
- 不复制许可证受限代码。
- 不执行未授权的批量重解析、迁移、发布或推送。
- 任何 schema 变更同时更新 bootstrap 和 alembic。
