# WeKnora document ingest

The default Library parsers are unchanged. To opt in on a server, set
`DOCUMENT_INGEST_PARSER=weknora` and configure `PDF_REVISION_WEKNORA_PYTHON`
and `PDF_REVISION_WEKNORA_SOURCE` for the pinned docreader checkout and runtime.
If using PaddleOCR, set `PDF_REVISION_PADDLE_OCR_ENDPOINT` to the full layout
parsing URL for the trusted internal service. With no endpoint, the adapter
does not send document content and reports `paddle_ocr_endpoint_not_configured`.

## What the Word parser keeps

The WeKnora DOCX path removes section headers and footers from an in-memory
copy before conversion. Their text and exclusive images do not enter Markdown,
OCR, or the text sent for indexing. Body paragraphs, tables, body images, and
referenced footnotes remain available. An image also used in the body is not
removed with the header or footer.

The original upload remains unchanged. The archive records its identity and
size rather than those of the parser copy. A version number or document code
written only in a header or footer can still be read in the original Word
file, but it is not searchable through the new parse. Existing revisions
remain unchanged, including revisions already published by a retrying delivery.
