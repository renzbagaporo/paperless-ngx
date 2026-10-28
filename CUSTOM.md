# Custom Paperless-ngx

This fork starts at upstream **v3.3.4**. `stable` records the last integrated
upstream release; `personal` is the default branch and carries the additions.
Future release updates merge an upstream stable tag into a review branch from
`personal`. Advance `stable` to that upstream tag after the update is accepted.

The document file controls support an ongoing collection such as “Water Supply
Notices”: new scans become additional searchable pages under the existing
document ID and archive serial number. Its title, dates, metadata, permissions,
custom fields and notes remain attached to that document.

Open **Actions → PDF Editor** to append a new scan, remove pages, rotate or
reorder them, or split the file into new documents. Choosing a scan selects
**Update this document**. Page edits and the scan are saved together;
only the scan's new pages enter OCR. Splitting creates new documents and
cannot be combined with appending in the same operation.

Updates offer **Keep only the current file** or file history. Removing earlier
files requires confirmation and delete permission. The editor checks the file
checksum before saving so another completed edit cannot be silently overwritten.
**Versions → Replace this document's file** uses the same storage choice.

## OCR and titles

Choose **LlamaParse** as the remote OCR engine in Settings → Application
Configuration, supply its API key, and choose which documents use remote OCR.
The equivalent environment settings are:

```dotenv
PAPERLESS_REMOTE_OCR_ENGINE=llamaparse
PAPERLESS_REMOTE_OCR_API_KEY=your-llamacloud-api-key
PAPERLESS_REMOTE_OCR_MODE=always
```

This uses LlamaParse during ingestion, including ordinary dashboard uploads and
the consume folder. The parser adds a text layer to the existing PDF pages,
retaining their compressed images and vector content. The uploaded source is
deleted from LlamaCloud after parsing and source caching is disabled. LlamaCloud
may retain parse job results according to its service policy.

Paperless's native AI suggestions provide document titles. Configure your
OpenAI-compatible Qwen server using Application Configuration, or:

```dotenv
PAPERLESS_AI_ENABLED=true
PAPERLESS_AI_LLM_BACKEND=openai-like
PAPERLESS_AI_LLM_ENDPOINT=http://llama-server:8080/v1
PAPERLESS_AI_LLM_MODEL=Qwen3.5-4B-Q4_K_M.gguf
```

Use the document's **Suggest** control to review a title. For automatic titles,
create a workflow for newly added documents with an **Apply AI suggestions**
action and select only **Title**. Scope that workflow to an inbox tag if you
want to choose which uploads receive an automatic name. Existing collections
keep their title and date when pages are appended.

Optional local checks validate issue dates printed in OCR text, including
Japanese eras, kanji numerals and full-width digits. Only a single valid,
explicitly issue-labeled date can replace the ordinary date selection; a date
supplied by the uploader takes priority. Ambiguous dates produce a private
advisory note. A separate check flags formatted OCR numbers supported only by
another page's plain OCR in the same scan batch. Neither check makes an extra
AI request. Enable them with:

```dotenv
PAPERLESS_OCR_VALIDATE_PRINTED_DATES=true
PAPERLESS_OCR_REVIEW_CROSS_PAGE_NUMBERS=true
```

These checks are off by default. Appending preserves the collection's title
and date; review notes identify page numbers after the retained pages. The
cross-page comparison requires LlamaParse's plain and formatted OCR results.

## Storage

The explicit **keep only current file** mode discards superseded versions and
stores the current searchable PDF under the original document ID. This removes
the earlier bytes permanently from Paperless after the update succeeds; they
remain recoverable only through any separately retained backups.

To use single-file storage for newly consumed documents as well, opt in with:

```dotenv
PAPERLESS_STORE_ONLY_CURRENT_FILE=true
```

Ordinary upstream version uploads remain available when file history is wanted.
The storage setting is off by default. It also supplies the API default for
append, replace and existing-document PDF edits when their retention choice
is omitted. Those APIs accept an explicit choice for each update.

## Build

The custom Dockerfile layers this fork's application and compiled UI onto the
pinned upstream v3.3.4 runtime:

```sh
docker build \
  --file Dockerfile.custom \
  --tag paperless-ngx-custom:3.3.4 .
```

The **Build custom image** workflow can build the same image on GitHub. Its
`publish` option defaults to false. When enabled, the workflow publishes
`ghcr.io/renzbagaporo/paperless-ngx:personal` and a commit-specific
`personal-<full-commit-sha>` tag. Deployments pin the commit-specific tag.
The optional `revision` input selects the source commit independently of the
workflow revision, so a reviewed commit can be built after workflow updates.

Use this image with the existing Paperless deployment configuration. This fork
does not automatically deploy or reset any instance.
