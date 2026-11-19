# 模块 3：双层检索与 Agentic Search（待商榷）

> **状态：DISCUSSION / 未锁定。**
>
> 本文是架构讨论记录，不是实现授权。不得据此修改代码、部署服务、迁移数据、批量重解析或发布。

## 1. 当前结论

Library 的检索不应在“页面摘要定位”和“`content.md` 正文搜索”之间二选一。两者应并存：

```text
第一层：摘要 / 元数据 / sections
  → 快速找到候选文档和候选页面

第二层：候选 revision 的 content.md
  → grep / BM25 / 可选语义搜索
  → 找到具体正文命中范围

第三步：source-map 回链
  → PDF 页 / Word 段落 / Excel 单元格 / PPT slide
  → 邻域读取、图片核验和引用验证
```

### 第一层：摘要导航层

现有 Library 能力：

- `summary`；
- `description.sections`；
- tags、catalog、journal；
- metadata FTS；
- 可选摘要/section semantic index；
- `read_entries_metadata` 获取章节图和元数据。

职责：

> 判断哪些文档、章节或页面值得先看。

### 第二层：正文证据层

模块 2 生成并持久化：

```text
revision/
  content.md
  source-map.jsonl
  assets/
  tables/
```

职责：

> 判断具体原文哪里包含数字、批号、表头、句子或表格证据。

正文搜索命中后返回：

```text
document_id
revision_id
content_range
source_map_id
snippet
locator
asset_ids / table_id
```

## 2. 为什么两层要并存

摘要层和正文层解决不同问题：

| 层 | 擅长 | 不擅长 |
|---|---|---|
| 摘要/元数据 | 主题导航、候选缩小、低成本检索 | 精确数字、批号、表格单元、未写入摘要的细节 |
| `content.md` 正文 | 原文数字、术语、表头、句子、正文引用 | 不一定理解图片像素和图表语义 |
| source-map | 回到原件页、段、sheet、slide | 不负责相关性排序 |
| visual asset read | 曲线、图表、图片表格 | 不能弥补解析时未保存的资产 |

不能让每页 summary 取代正文搜索。每页 summary 仍可能丢失：

- 数字和单位；
- 表头和合并关系；
- 批号和型号；
- 跨页表格关系；
- 法律或实验原文措辞。

## 3. Agentic Search 循环

```text
问题
  → summary / metadata / sections 导航
  → 候选文档 / 候选页面
  → 在候选 revision 的 content.md 中定位正文
       ├─ 精确匹配 / GREP
       ├─ BM25
       └─ 可选语义搜索
  → source-map 回链
  → 邻域读取与图片核验
  → 引用核验
```

如果摘要层没有候选：

```text
扩大 content.md 搜索范围
  → 返回新的文档候选
  → 再进入摘要/正文联合验证
```

如果正文层没有精确命中：

```text
改写主题 / 换关键词 / 换语义路径
  → 返回摘要层或更宽正文范围
  → 记录“未找到”与“搜索失败”的区别
```

## 4. 对比项目提供的机制

### Library

保留：

- summary/sections/metadata 导航；
- `list_folder`、catalog 浏览；
- 已知文件的 `read_files(pattern/page/section)`；
- `query_sql` 表格计算；
- `finish_research` 原文引用校验。

待补：

- 跨文档 `content.md` 正文搜索；
- revision 范围和 source-map 回链；
- 可继续的正文搜索结果和邻域读取；
- 独立 asset/page 视觉核验入口。

### WeKnora

可借鉴：

- hybrid / keyword / semantic 模式分路；
- `read_document(query/regex)` 文档内扫描；
- `doc://`、`chunk://` 阅读句柄；
- 邻域读取、offset 和 next_offset；
- 搜索失败、空结果和预算裁剪区分。

### RAGFlow Agentic RAG

可借鉴：

- `grep_chunks` 精确/正则定位；
- BM25 关键词召回；
- 纯向量 semantic bridge；
- `list_chunks` 按锚点读取前后邻域；
- 缺失事实驱动下一轮检索；
- 搜索 snippet 与深读正文分离。

### LinkSense

可借鉴：

- child 正文检索和 parent 上下文；
- 持久化 Markdown 分页读取；
- cursor、byte range、complete 状态；
- 每次读取前后重新检查授权。

## 5. 当前推荐候选（仍需商榷）

### 候选：双层 content.md Agentic Search

```text
摘要/元数据导航
  → 候选文档/页面
  → 搜索候选 revision 的 content.md
  → source-map 回到页/段/sheet/slide
  → 证据不足时扩大 content.md 范围或请求视觉
  → 引用核对与有界停止
```

建议初期：

1. 保留摘要/元数据 FTS 和向量作为第一层候选导航。
2. 将 `content.md` 作为第二层正文搜索面。
3. 优先支持精确匹配和关键词搜索；是否增加正文向量后置决定。
4. 先在候选文档/页面范围内搜索，证据不足时扩大到授权 revision 范围。
5. 返回 `document_id + revision_id + content_range + source_map_id + locator`。
6. 返回截断、继续读取和覆盖状态。
7. 图片路径/图题可被正文搜索命中，但图片像素理解必须由独立视觉读取完成。
8. 检索内部 segment 可以重建，不能成为 Library 公共 Block 或产品 Chunk。

## 6. 必须商榷的问题

### 6.1 正文搜索范围

- 默认只搜索摘要层选出的 revision，还是默认搜索全部授权 revision？
- 摘要无候选时，何时自动扩大正文范围？
- 文件数量很大时，如何限制成本和延迟？
- 是否需要用户指定 folder/catalog/revision 范围？

### 6.2 正文搜索实现

- 复用 SQLite FTS5 建 `content.md` 正文表；
- 对候选 revision 做受控扫描；
- 增加精确匹配/GREP 索引；
- 何时需要正文向量；
- 是否需要 rerank；
- 是否需要内部 segment；
- 是否需要引入 Elasticsearch。

### 6.3 content.md 格式

- PDF 是否每页写稳定标记，例如 `<!-- page:12 -->`；
- Word 是否保留 paragraph/table 标记；
- Excel 是否保留 sheet/row/cell 标记；
- PPT 是否保留 slide/shape 标记；
- Markdown 字符范围如何在拼接和修订后保持稳定；
- 表格正文和 `tables/` sidecar 如何同时搜索；
- 图片 caption/OCR 是否进入正文搜索面。

### 6.4 Agent 停止条件

- 首轮摘要命中后是否必须做一次正文核验；
- 精确问题是否必须走 GREP/字面匹配；
- “所有/分别/有几个”是否要求穷尽读取；
- 何时判定“没有命中”；
- 如何区分搜索失败、索引不完整和证据不存在；
- 如何报告尚未覆盖的页/文档。

### 6.5 视觉边界

- 什么时候自动请求 asset/page 视觉读取；
- 文本层存在时是否仍允许主动查看图片；
- 图表读数是否必须保留视觉证据；
- 图片 caption 与像素观察如何区分；
- 视觉失败是否触发旧 LLM 直读 fallback。

## 7. 验收方式（未执行）

将同一批问题分为三档：

| 档位 | 工具 | 目的 |
|---|---|---|
| 单次搜索 | 只允许第一层摘要/元数据 | 测首轮候选召回 |
| 当前 Agent | 现有目录、摘要、文件内读取 | 测现有 Agent 补救能力 |
| 双层 Agent | 增加 `content.md` 跨文档搜索 | 测正文入口的增量价值 |

样本至少包括：

1. 文件已知、细节未进摘要；
2. 文件未知、只有数字/批号/罕见术语；
3. 问题与正文措辞不同；
4. 200 页文档后部证据；
5. 跨页表格；
6. 跨文档两跳问题；
7. 仅图像可见的曲线数值；
8. 要求完整枚举的问题；
9. 检索后端失败；
10. 材料确实无答案。

指标：

- 首轮候选召回；
- 漏召回后恢复率；
- 正文范围定位准确率；
- 引用正确率；
- 穷尽问题的覆盖率；
- 错误肯定和错误无答案；
- Agent 调用轮数、耗时、token 和视觉调用次数。

## 8. 当前边界

- 模块 1 已锁定：revision、`content.md`、source-map、assets、tables，不采用公共 Block。
- 模块 2 已锁定执行：W/L 并行对比，R 做 PDF 基线。
- 模块 3 **未锁定**：摘要导航层与 `content.md` 正文证据层并存是当前讨论方向。
- 尚未实现正文搜索工具、revision 写入、Agent 新工具或 SearchHit 改动。
- 本 planning 文档不授权代码修改、部署、迁移、批量重解析或发布。
