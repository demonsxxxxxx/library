# Library Launch Kit

Use this file when posting Library to GitHub, Hacker News, Product Hunt,
Reddit, LINUX DO, V2EX, or similar communities. Keep claims concrete: Library
is a local-first private AI library and research agent, not a general SOTA RAG
benchmark claim.

## Positioning

**English one-liner**

Turn your PDFs, notes, spreadsheets, logs, and archives into a private AI
library that answers from original sources.

**Chinese one-liner**

把 PDF、笔记、表格、日志和压缩包变成一个能读原文、会引用来源的私人 AI
图书馆。

**Short tagline**

Local-first AI library with cited answers from original files.

**Best audience**

- Researchers, engineers, students, analysts, and heavy note/PDF users.
- People who want cited answers over a private file library.
- Self-hosted/local-first users who distrust cloud-only knowledge systems.
- RAG builders interested in retrieval beyond top-k chunk search.

**Avoid leading with**

- Generic "AI knowledge base" wording.
- Benchmark-heavy claims before explaining the user workflow.
- Internal architecture terms such as ReAct, RRF, rerank, and entry relations in
  the first sentence.

## GitHub Checklist

- Repository description:
  `Local-first AI library that reads private files and answers with citations.`
- Suggested topics:
  `local-first`, `rag`, `research-agent`, `knowledge-management`, `llm`,
  `ai-agents`, `pdf`, `sqlite`, `self-hosted`
- Upload a social preview under repository Settings -> Social preview.
  GitHub recommends 1280x640 for best display and an image under 1 MB.
  Use `docs/images/social-preview-en.jpg` for the repository preview; use
  `docs/images/social-preview-zh-CN.jpg` for Chinese community posts.
- Pin the latest release and make the release assets easy to scan.
- Add a 60-90 second demo video or GIF near the top of the README.

## Demo Script

Goal: show the value in one minute.

1. Start from an empty Library instance.
2. Upload a folder containing a Raft paper, Paxos notes, a meeting note, and a
   small log or spreadsheet.
3. Ask: `What are the key differences between Raft and Paxos? Cite the sources.`
4. Show the agent planning, reading original files, and producing footnotes.
5. Click one cited source or show the source metadata panel.
6. Ask a follow-up: `Which source is most useful for implementation details?`

The demo should show original-source citation, not just a chat answer.

## Hacker News

**Title**

Show HN: Library - a local-first AI library that cites original files

**Post/comment body**

I built Library because my research material was spread across PDFs, notes,
tables, logs, and archives, and a plain vector-search layer over chunks did not
give me enough confidence in the answers.

Library keeps files in a local folder tree, builds library metadata around
them, and gives the agent tools to find candidates, inspect metadata, follow
related entries, read original source windows, and answer with citations.

It has two modes:

- Quick: bounded evidence gathering for short lookups.
- Deep: a slower investigation loop for reports where coverage matters.

There is a Python CLI. It is AGPL.

Repo: https://github.com/demonsxxxxxx/library

I would especially like feedback from people who manage large private research
libraries or have built RAG systems over messy local files.

## Product Hunt

**Name**

Library

**Tagline**

Private AI library with cited answers from original files

**Description**

Library turns PDFs, notes, spreadsheets, logs, images, and archives into a
local-first AI library. It organizes your files, searches metadata and journals,
reads original source windows, and produces cited answers and research reports.

**First comment**

I built Library for people whose useful knowledge is scattered across
private files: papers, notes, tables, logs, screenshots, and archives. The goal
is not another black-box vector database. The goal is a local library where the
agent can narrow the search space, read original sources, cite its evidence,
and leave behind durable investigation notes for future questions.

The current release includes the CLI, upload/ingest pipelines,
Quick/Deep chat modes, optional embeddings/reranking, and evaluation commands.

## Reddit / Technical Communities

**Technical title**

I built a local-first research agent that reads original files before answering

**Body**

Library is an AGPL local-first AI library for private heterogeneous files:
PDFs, Markdown, DOCX, images, spreadsheets, logs, and archives.

Instead of only retrieving top-k chunks, it uses a structured retrieval funnel:
folders/catalogs/tags/metadata/journals, optional semantic recall and rerank,
related-entry discovery, targeted original-file reads, and cited answers.

What I would like feedback on:

- Whether the local-first storage model is understandable.
- Whether Quick vs Deep mode is the right UX for latency/cost tradeoffs.
- Which ingest formats or citation workflows are still missing.

Repo: https://github.com/demonsxxxxxx/library

## Chinese Communities

**标题**

我做了一个本地优先的私人 AI 图书馆:会读原文,回答带引用

**正文**

Library 是一个开源的本地优先研究 agent,面向 PDF、笔记、Office 文档、
图片、表格、日志和压缩包混在一起的个人资料库。

它不是简单把文件切块丢进向量库。它会先用文件夹、catalog、tag、metadata、
journal 和可选语义召回缩小范围,再读取原文窗口,最后输出带引用的回答。

适合的场景:

- 比较几篇论文和自己的笔记。
- 从日志、复盘文档、表格里整理事故线索。
- 把一个文件夹整理成带引用的研究简报。
- 在本地保存资料,不想把私有文件交给云端知识库。

当前有 Windows/macOS/Linux 桌面包,也有 Python CLI。

仓库: https://github.com/demonsxxxxxx/library

欢迎重点喷 README、安装体验、桌面端交互和真实资料库里的检索效果。

## Seven-Day Launch Plan

**Day -2**

- Record the 60-90 second demo.
- Compress and upload the GitHub social preview.
- Verify release assets and README links.

**Day -1**

- Post a low-key build note to an existing community where you already
  participate.
- Ask 3-5 users to try the CLI setup and report install blockers.

**Launch Day**

- Post Show HN only when the repo and demo are ready to try.
- Reply to every substantive question with concrete examples.
- Do not ask friends to upvote or leave coordinated comments.

**Day +1 to +7**

- Convert repeated questions into README/USAGE updates.
- Open issues for install failures and missing formats.
- Share one technical deep dive only after the first user feedback lands.
