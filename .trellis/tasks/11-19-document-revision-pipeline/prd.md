# 文档解析 revision 管线 — 需求与决策记录

Status: planning only. 本文锁定架构方向，不授权代码改造、解析器部署、数据迁移、批量重解析、发布或部署。本文档自身的版本提交不属于实施授权。

## 1. 背景与目标

Library 当前有一条“LLM 直接读取原件并生成 summary/description/sections”的既有线路：

```text
原件 → pipeline 读取 → LLM 生成元数据 → FTS/可选 semantic index
查询后 → Agent 再读取原件片段
```

这条线路初始成本低，但解析结果不作为不可变档案保存。重复总结、重新 embedding、查看器和索引各自读取原件，无法共享同一份完整解析结果。

本任务的目标是规划第二条线路：

```text
原件 → 专业解析 → 持久化 revision 档案
revision → Library 生成 summary/sections/enrichment
revision → FTS/可选 semantic index
查询 → 索引/摘要 + content range + 原件定位
```

两条线路由用户或任务策略二选一；新线路完成验收前，旧线路继续保留为 fallback。

## 2. 已锁定的模块 1 决策

### 2.1 revision 是解析结果的唯一身份

- 原件仍是唯一真相。
- 每次解析生成新的不可变 `revision_id`，不覆盖旧解析。
- 只有通过校验的 revision 才能成为 active revision。
- 删除或重建索引不能删除 revision、source map 或资产。

### 2.2 首期档案产物

```text
document-revisions/<revision_id>/
  manifest.json
  content.md
  source-map.jsonl
  assets/
  tables/            # 复杂表格按需
  optional/
    parser-native.json
```

| 产物 | 职责 |
| --- | --- |
| `manifest.json` | 原件版本、解析器、配置摘要、覆盖率、页级状态、警告 |
| `content.md` | 统一阅读投影；summary、导出、查看和 Agent 精读的共同输入 |
| `source-map.jsonl` | Markdown 字符范围到 PDF/Word/Excel/PPT/HTML 原件位置的映射 |
| `assets/` | 图片、裁图、页面图；每张图只有一个 `asset_id` |
| `tables/` | 复杂表格网格、合并单元格和定位 |
| `optional/parser-native.json` | 上游原生结构，仅供排查和迁移，不是公共契约 |

### 2.3 不采用 Library 通用 Block 模型

- 不新增跨格式公共 `Block` 核心表。
- 检索内部如需切分，只生成可重建的内部 segment。
- segment 可删除、可重建、可更换策略，不反向定义档案。
- 查看和精读使用 `document_id + revision_id + content_range + source_map_id + asset_id + locator`。

## 3. 已锁定的模块 2 决策

### 3.1 W/L 对比，R 做 PDF 基线

```text
W：WeKnora/docreader
L：LinkSense/Docling
R：RAGFlow/DeepDOC（仅 PDF 质量基线）
```

- W 重点验证多格式解析、Markdown、SourceBlocks、SourceLocator、图片和引用回链。
- L 重点验证 Docling JSON、Markdown、图片资产、reading order、provenance、checkpoint 和激活边界。
- R 只验证 PDF 文字层、扫描 OCR、版面、表格 TSR、图片/crop 和 page/position，不引入完整 RAGFlow。
- 三条路线统一转换为 Library revision，再比较质量、定位、资产、耗时和部署成本。
- 不比较各产品自己的 Chunk、ES 或 Hybrid 检索分数。

### 3.2 项目是应用，不等于解析组件

- WeKnora、RAGFlow、LinkSense 是完整应用，不是 AnyDoc、DeepDOC、Docling 的别名。
- 只接解析能力和产物，不搬 Knowledge/Chunk/ES/MinIO/workspace 数据模型。
- 任何解析器输出都必须归一为 Library 的 `content.md + source-map.jsonl + assets + tables`。

## 4. 范围

### In scope

- revision 档案的数据契约和目录布局。
- 解析适配器接口：输入原件，输出 Markdown、source map、assets、tables、coverage、warnings、optional native sidecar。
- 解析路线选择：文字结构、PDF 版面、OCR 图像、混合页、视觉增强。
- W/L/R 的隔离样本对比方案。
- summary/sections 基于持久化 Markdown 重新生成。
- 查询结果携带 document、revision、content range、source map、locator 和 assets。
- 前端图文阅读与原件核验的接口边界。
- 失败、部分覆盖、旧 revision 保留和回退。

### Out of scope

- 立即替换当前 LLM 直读线路。
- 引入完整 WeKnora、RAGFlow 或 LinkSense 作为 Library 底座。
- Elasticsearch、Milvus、MinIO 集群迁移。
- 公盘全量同步、员工 ACL/SSO、正式发布。
- 批量重解析现有库、删除旧解析结果或覆盖原件。
- 本任务范围内的实现代码提交、发布或部署；文档归档提交不构成实施授权。

## 5. 功能需求

R1. 一份原件每次解析产生一个不可变 revision，并能记录 parser 名称、版本和配置摘要。

R2. revision 必须包含 `manifest.json`、`content.md`、`source-map.jsonl`、assets；复杂表格按需包含 tables。

R3. `content.md` 的图片引用使用受控 `asset_id`，不得重复保存图片字节。

R4. `source-map.jsonl` 必须能表达 PDF page/bbox、DOCX paragraph/table、XLSX sheet/row/cell、PPTX slide/shape、HTML/MD 字符范围；没有精确坐标时写 `null`，不得伪造。

R5. 混合 PDF 必须按页记录实际解析路线、覆盖率、失败和 partial 状态；不得整份文件笼统标记为“OCR 成功”。

R6. 解析适配器不得写 FTS、不得调用 embedding、不得生成 summary、不得修改目录和原件。

R7. summary、sections、description 和 tags 只能基于持久化 revision 生成；更换 prompt 不重新解析原件。

R8. 更换 embedding 或索引策略必须能从 revision 重建，不重新解析原件。

R9. 查询结果不得依赖产品 Chunk ID；必须返回 document、revision、content range、source map、locator 和 assets。

R10. 解析失败、覆盖率不足或校验失败时，不得激活新 revision，也不得覆盖旧 active revision。

R11. 当前 LLM 直读线路保留为 fallback；新线路只替代解析和 summary 输入。

R12. 视觉增强只能作为 asset 或 content range 的附加 enrichment，不能覆盖原始图片和原文。

## 6. W/L/R 对比要求

### 6.1 样本

- DOCX：普通报告、带图片和表格。
- XLSX：普通表格、合并单元格和图片。
- PPTX：文本框、表格、图片、多个 slide。
- PDF：文字层、双栏、扫描页、混合页、复杂表格/图表。
- HTML/Markdown：标题、链接、表格和图片。

### 6.2 每份样本记录

- 原件 SHA-256、大小、页数/sheet/slide 数。
- Markdown 完整性、标题层级、阅读顺序。
- 表格结构、合并单元格、图片和图题。
- page/bbox/sheet/slide/paragraph 定位准确性。
- 覆盖率、失败页、警告和 partial 标记。
- 解析耗时、峰值内存、临时盘、模型/服务依赖。
- 输出转换为 Library revision 的改动量。

### 6.3 交付证据

- W、L 各自生成可回读的 Library revision。
- R 生成可比较的 PDF 文字、页面、表格、图片和定位结果。
- 三条路线使用同一批样本和同一套评分表。
- 对比结论必须附原始输出和失败样本，不得只给总体评分。

## 7. 验收标准

- [ ] 一份 PDF、一份 DOCX、一份 XLSX、一份 PPTX 都能生成完整 revision 档案。
- [ ] 同一原件可以并存两个 revision，旧 revision 仍可读取。
- [ ] 图片只有一个 `asset_id`，Markdown、source map 和查看器共用。
- [ ] 复杂表格的合并关系不只存在于 Markdown 字符串。
- [ ] 混合 PDF 有页级 engine、coverage 和失败记录。
- [ ] summary 可以仅基于持久化 `content.md` 重新生成。
- [ ] 更换 summary prompt 和 embedding 不需要重新解析原件。
- [ ] 查询结果能打开正确文档、内容范围、图片和原件位置。
- [ ] 索引删除/重建不影响 revision、source map 和 assets。
- [ ] W/L 对比完成，R 的 PDF 基线结果可复核。
- [ ] 当前 LLM 直读线路仍可作为 fallback 运行。
- [ ] 摘要/元数据导航层与 `content.md` 正文证据层可以并存。
- [ ] 双层 Agentic Search 能在摘要候选范围内定位正文，并在证据不足时扩大正文搜索范围。
- [ ] 未执行任何实施代码提交、发布、部署、批量重解析或公盘写入。

## 8. 当前状态

- 模块 1 已锁定。
- 模块 2 已锁定为 W/L 对比、R 做 PDF 基线。
- 模块 3 正在讨论：摘要/元数据导航与 `content.md` 正文证据搜索并存；尚未锁定 FTS/GREP、正文向量、rerank 或 SearchHit 最终契约。
- 尚未部署解析服务、尚未运行 W/L/R 对比、尚未修改 Library 代码。
- 模块 3 讨论记录：`module-03-retrieval-discussion.zh-CN.md`。
