# 文档解析 revision 管线 — 技术设计

Status: proposal only. 本设计锁定模块 1 和模块 2 的架构，不授权实现、部署、迁移、发布或批量重解析。本文档自身的版本提交不属于实施授权。

## 1. 现有行为与缺口

当前 ingest 主线：

```text
File/FileEntry
  → pipelines registry 选择解析器
  → pipeline 读取原件
  → LLM 生成 summary/description/sections/tags
  → FTS/可选 semantic index
  → 查询后 Agent 重新读取原件片段
```

现有优点：

- 原件保持只读真相；
- 已有多格式 pipeline 和 OCR 回退；
- 已有 FTS、semantic index、journal 和引用式回答；
- 任务重试、状态和前端查看已有基础。

主要缺口：

- 没有统一、不可变的完整解析档案；
- 解析结果不能复用于 summary、索引和查看；
- 重新总结或更换 embedding 可能再次读取原件；
- 图片、表格和页码定位没有统一 `asset_id`/source map 契约；
- 多格式解析质量依赖单一 pipeline，难以按内容形态选路。

## 2. 目标架构

```text
原件（只读）
  ↓
解析调度：按内容形态/页面分路
  ↓
Parser adapter（W/L/R 或 Library 原生）
  ↓
revision 目录（不可变）
  ├─ manifest.json
  ├─ content.md
  ├─ source-map.jsonl
  ├─ assets/
  ├─ tables/
  └─ optional/parser-native.json
  ↓
Library 后处理
  ├─ summary/sections/description/tags
  ├─ FTS5
  ├─ 可选 semantic index / sqlite-vec
  └─ SearchHit 契约
  ↓
查看与 Agent 精读
  ├─ Markdown 范围
  ├─ asset
  ├─ source map locator
  └─ 原件 page/sheet/slide
```

边界原则：

1. 原件不被解析器修改。
2. revision 不被索引写入反向修改。
3. 产品 Chunk/DocItem/Hybrid 不进入 Library 公共模型。
4. summary、tags、embedding 都是 revision 的派生数据。
5. 查看器和索引读取同一份 revision。

## 3. revision 数据契约

### 3.1 目录

```text
document-revisions/<revision_id>/
  manifest.json
  content.md
  source-map.jsonl
  assets/<asset_id>.<ext>
  tables/<table_id>.json
  optional/parser-native.json
```

首期使用 Library Home 下的本地目录；后续可迁移到对象存储。目录一旦 active 不得原地修改。

### 3.2 manifest.json

```json
{
  "revision_id": "uuid",
  "file_id": "uuid",
  "entry_id": "uuid",
  "source_version": {
    "sha256": "...",
    "size_bytes": 0,
    "mtime": "..."
  },
  "parser": {
    "route": "text|layout|ocr|mixed",
    "name": "weknora-docreader|docling|deepdoc|library-native",
    "version": "...",
    "config_digest": "..."
  },
  "input": {
    "filename": "report.pdf",
    "mime": "application/pdf"
  },
  "coverage": {
    "units": "page|sheet|slide|document",
    "total": 20,
    "processed": 20,
    "failed": [],
    "partial": [],
    "image_count": 12,
    "table_count": 4
  },
  "status": "parsed|partial|failed",
  "warnings": []
}
```

要求：

- `parser.config_digest` 能区分模型、OCR 开关、语言、表格选项等关键配置。
- `coverage` 按格式选择单位；PDF 按页，XLSX 按 sheet，PPTX 按 slide。
- `status=partial` 必须有失败或低覆盖记录，不能静默当作完整。

### 3.3 source-map.jsonl

每行一个 Markdown 范围到原件位置的映射，不是通用 Block：

```json
{
  "source_map_id": "map-42",
  "revision_id": "revision-1",
  "kind": "heading|paragraph|table|image|page|slide|sheet|row",
  "content_start": 1820,
  "content_end": 2088,
  "title_path": ["第三章", "实验结果"],
  "locator": {
    "type": "pdf|docx|xlsx|pptx|html|md|image",
    "page": 12,
    "bbox": [0.12, 0.24, 0.86, 0.38],
    "paragraph": null,
    "sheet": null,
    "cell": null,
    "slide": null,
    "shape": null,
    "quote": null,
    "mapping": "exact|partial|quote-only"
  },
  "asset_ids": [],
  "table_id": null,
  "quality": {
    "engine": "text_layer|ocr|native|vision",
    "partial": false
  }
}
```

约束：

- `content_start/end` 使用 `content.md` 的字符偏移。
- 没有 bbox 时写 `null`，不得补造。
- `mapping=partial|quote-only` 时前端必须提示定位不确定。
- 同一内容范围可以有多条映射，但必须指向同一 revision。

### 3.4 assets

```text
asset_id: 内容寻址或稳定 UUID
asset bytes: 原图/裁图/页面图
asset metadata: mime、宽高、来源页/区域、sha256
```

Markdown 引用：

```markdown
![图3：层析曲线](kb-asset://asset-003)
```

查看器通过受控 asset API 获取字节；不在 URL 暴露管理 token，不把 base64 写入 Markdown。

### 3.5 tables

简单表格可只用 HTML table。复杂表格保存：

```json
{
  "table_id": "table-1",
  "grid": [[...]],
  "merges": [{"row":0,"col":0,"rowspan":1,"colspan":2}],
  "locator": {"page": 12, "bbox": [...]},
  "source": "native|ocr|tsr"
}
```

## 4. 解析路线

### 4.1 五条路线

| 路线 | 适用 | 输出重点 |
| --- | --- | --- |
| 文字结构 | DOCX/XLSX/PPTX/HTML/MD/TXT；简单文字层 PDF | 原生段落、表格、sheet/cell、slide/shape |
| PDF 版面 | 双栏、复杂文字层、表格/图表页 | reading order、page/bbox、表格结构 |
| OCR 图像 | 扫描 PDF、图片文档、无可靠文字层页面 | OCR、文字框、表格、页面图 |
| 混合页 | 同一 PDF 含文字页、扫描页、复杂页 | 逐页选择路线并合并 |
| 视觉增强 | 已提取的图片/图表/曲线 | OCR/caption/VLM enrichment |

### 4.2 PDF 路由

```text
PDF
  → 页面是否有可靠文字层？
     ├─ 否 → OCR + 图像版面
     └─ 是 → 版面是否简单？
              ├─ 是 → 文字结构
              └─ 否 → PDF 版面
  → 页面/区域是否含图表或图片？
     ├─ 否 → 完成
     └─ 是 → 可选视觉增强
```

路由结果必须写回 manifest 的页级状态，而不是只记录一个全局 parser 名。

### 4.3 混合页合并

同一 revision 内允许不同页使用不同 adapter。合并前必须：

1. 按页排序；
2. 校验页数覆盖；
3. 合并 Markdown 并重算字符偏移；
4. 重写 source map 的 `content_start/end`；
5. 保留每页 engine、coverage 和 warnings；
6. 对重复页或缺失页报错，不静默拼接。

## 5. 解析器适配器

### 5.1 接口

```text
parse(source_ref, options) -> ParsedDocument

ParsedDocument:
  markdown_text
  source_map_entries
  assets
  tables
  coverage
  warnings
  parser_native_sidecar
```

适配器职责：

- 读取原件或受控转换件；
- 生成 Markdown；
- 生成 source map；
- 提取图片和表格；
- 报告覆盖率和失败；
- 可选保存原生 sidecar。

适配器禁止：

- 写 FTS/向量；
- 调用 summary/embedding；
- 修改目录、标签和原件；
- 写用户可见数据库；
- 决定查询返回结构。

### 5.2 adapter 位置

首期建议隔离在 parser worker 或独立服务中，通过任务输入/输出目录交换产物。不要在其他产品的数据库事务内直接调用 Library ORM。

## 6. W/L/R 接入边界

### 6.1 W：WeKnora/docreader

接入：

```text
原件 → docreader → Markdown + 图片 + SourceBlocks/SourceLocator
     → Library adapter → revision
```

只取：

- Markdown 文本；
- 表格和图片；
- SourceBlocks 的字符范围；
- SourceLocator 的 page/bbox/slide/sheet；
- 覆盖率、警告。

不取：

- WeKnora Knowledge/Chunk 表；
- WeKnora 的向量/全文索引；
- WeKnora 应用路由和权限模型。

风险：

- docreader 版本和依赖需要固定；
- AnyDoc 是否为启用构建需要核实；
- SourceLocator 的坐标语义必须逐个格式验证；
- 表格和图片落盘路径需要重映射为 Library asset。

### 6.2 L：LinkSense/Docling

接入：

```text
原件 → Docling Serve → Markdown + Docling JSON + 图片 ZIP
     → Library adapter → revision
```

只取：

- Markdown；
- DoclingDocument JSON；
- 图片资产；
- reading order、provenance、bbox；
- checkpoint 和阶段状态作为设计参考。

不取：

- LinkSense workspace/ES；
- Hybrid parent/child chunk 作为公共模型；
- LinkSense 应用层代码或许可证受限部分。

风险：

- Docling 服务和模型资源；
- 旧 Office 需要 LibreOffice/Unoserver 转换；
- provenance 到 Library locator 的映射；
- 图片 ZIP 与 asset_id 的稳定映射。

### 6.3 R：RAGFlow/DeepDOC

接入：

```text
PDF → 固定版本 DeepDOC → 页面/区域/表格/OCR 结果
    → Library adapter → revision（仅用于对比或 PDF 专用 adapter）
```

只取：

- 页级文字和 OCR；
- 区域/bbox；
- 表格结构；
- 图片/crop；
- page/position。

不取：

- 完整 RAGFlow ingestion；
- ES chunk、MinIO bucket 目录；
- MySQL/Kvrocks 和 RAGFlow 任务模型。

风险：

- 与当前 Go v1.0.0 镜像和源码版本绑定；
- 脱离 RAGFlow 运行环境后的可复现性；
- DocItems JSON 的持久化范围；
- 资源消耗和部署复杂度最高。

## 7. 数据模型与任务流

### 7.1 新增逻辑对象

```text
document_revision
  revision_id, file_id, entry_id
  source_sha256, parser_name, parser_version, config_digest
  route, status, coverage_json, manifest_path
  created_at, activated_at, superseded_by
```

首期可以先只落文件系统 + 现有文件表字段；是否新增表在实现阶段再定。任何 schema 变更必须同时更新 `db/bootstrap.py` 和 alembic revision。

### 7.2 任务流

```text
ingest_file
  → 选择路线
  → 调用 adapter（可重试）
  → 校验产物
  → 写 revision
  → 生成 summary/sections
  → 建 FTS/可选向量
  → 激活 revision
```

失败时：

- 不激活新 revision；
- 保留旧 active revision；
- 记录失败阶段和可重试原因；
- 不删除已生成的部分产物，除非明确是任务临时目录。

### 7.3 幂等

- 同一原件 + 同一 parser config digest 可以复用已有 revision。
- 不同配置或不同解析器必须生成新 revision。
- 任务重试不能产生重复 active revision。

## 8. 索引与查询（模块 3 边界）

本设计只定义边界，不在本任务实现：

- 首期对 `content.md` 范围建 FTS5。
- summary/sections 单独建可检索字段。
- 内部 segment 可选，必须可重建。
- `SearchHit` 返回 document、revision、content range、source map、locator 和 assets。
- 查询不得依赖产品 Chunk ID。

## 9. 前端边界（模块 4 边界）

- Markdown/HTML 图文阅读；
- 图片通过 asset API；
- 复杂表格用 tables sidecar 渲染；
- PDF 跳页，后续 bbox overlay；
- Office 跳 sheet/slide/paragraph；
- quote 重复或 partial 时提示定位不确定；
- 原件核验与阅读投影使用同一 revision。

## 10. 实施阶段

### 阶段 0：样本与基线

固定样本和评分表；记录当前 LLM 直读线路的结果作为基线。

### 阶段 1：revision 档案

实现 revision 目录、manifest、content.md、source-map、assets、tables 和 adapter 接口。先用 Library 原生解析器打通一份 PDF 和一份 DOCX。

### 阶段 2：W/L 对比

在隔离环境分别运行 WeKnora/docreader 和 Docling，转换为同一 revision 契约，按评分表对比。

### 阶段 3：R 基线

用固定版本 RAGFlow/DeepDOC 对同一批 PDF 生成质量基线，不接入完整 RAGFlow。

### 阶段 4：summary 与索引

基于 revision 生成 summary/sections，建 FTS5，验证更换 prompt/embedding 不重新解析。

### 阶段 5：查看与 Agent

实现 Markdown 图文阅读、asset API、表格渲染、原件定位和 Agent 按范围精读。

### 阶段 6：切换策略

按格式或目录决定使用旧线路还是新线路；保留回退开关。

## 11. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 解析器质量不稳定 | 错误 summary/索引 | 固定样本对比、覆盖率、partial 标记、旧 revision 保留 |
| 混合 PDF 拼接错误 | 阅读顺序和定位错乱 | 页级路由、页数校验、重算字符偏移 |
| 图片/表格丢失 | 查看和引用不完整 | asset/table 清单校验，缺失即 partial |
| 定位坐标不统一 | 前端跳转错误 | 统一 locator schema，逐格式验证，partial 明示 |
| 服务依赖过重 | 部署和维护成本 | 隔离 adapter，先对比后选型，不引入完整产品 |
| 许可证限制 | 不能直接复用代码 | 只参考设计和调用独立服务，不复制受限代码 |
| 双线路并存复杂 | 行为不一致 | 明确线路开关、按格式/目录切换、保留 fallback |

## 12. 验收与证据

- 单份 PDF/DOCX/XLSX/PPTX 的 revision 产物可回读。
- W/L 对比有原始输出、评分表和失败样本。
- R 的 PDF 基线有页级结果和定位证据。
- summary 可从 `content.md` 重建。
- 索引重建不影响 revision。
- 前端能按 content range 和 locator 打开阅读和原件。
- 未执行实施代码提交、发布、部署、批量重解析或公盘写入。

## 13. 开放问题

1. revision 表是首期新增，还是先复用现有 File 字段和目录？
2. W/L 对比由哪个隔离运行环境承载，模型和版本如何固定？
3. 混合 PDF 的页级路由阈值如何定义（文字覆盖率、页图比例）？
4. 复杂表格首期是否只支持 HTML table + sidecar，还是直接做结构化单元格？
5. bbox overlay 是否进入首期，还是先做页级跳转？
