# 文档解析 revision 管线 — 实施清单（未授权执行）

Status: planning only. 本文件是未来实施的任务拆分，不是执行授权。不得据此修改代码、部署服务、迁移数据或发布。文档提交不等于实施授权。

## 前置条件

- [ ] 模块 3（索引与 SearchHit）完成决策。
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
- 按页选择文字结构/版面/OCR 路线。
- 合并 Markdown 并重算 source map 偏移。
- 记录页级 engine、coverage、failed 和 partial。

验收：混合 PDF 的页级状态和定位可复核，缺失页不会被静默忽略。

## 任务 4：W 对比接入

- 固定 WeKnora/docreader 版本。
- 将 Markdown、SourceBlocks、SourceLocator、图片和表格转换为 revision。
- 记录无法映射的字段和失败样本。
- 不接 WeKnora Knowledge/Chunk 数据库。

验收：W 能在固定样本上生成完整 revision，并附原始输出。

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

## 任务 8：FTS 与查询契约

- 对 Markdown 范围建 FTS5。
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
