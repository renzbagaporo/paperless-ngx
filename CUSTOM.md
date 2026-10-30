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

Valid scans are saved even when LlamaParse credits are exhausted, requests time
out, or the service returns an incomplete result. The document receives
**ocr-pending** and **ocr-fail** tags and a private note with a safe failure
category. Its ID, ASN, metadata, original pages and existing OCR remain intact.
Appends save new pages under the same document and queue only those pages.
Pending pages remain pending through page edits, splits, exports and imports.

After restoring credits or provider access, use **Reprocess** or remove
**ocr-fail** to retry. Each completed page is saved before attempting another;
retries skip completed pages. Successful remote OCR is tagged **ocr-ok**;
digital PDFs that keep their existing text are tagged **ocr-skip**. The local
orientation check honors Paperless's rotation setting and confidence threshold.
Review warnings about numbers use **ocr-review** and private notes.

The management command previews saved pending work without making OCR requests:

```sh
python3 src/manage.py document_ocr
```

An explicit page budget is required to retry from the command line:

```sh
python3 src/manage.py document_ocr \
  --apply --max-pages 10
```

Add **--document ID** to restrict it to a collection. Pending OCR uses the
per-page provenance records.
Rich enrichment skips pending pages; retry their OCR first.

Retry requests survive broker outages. A scheduled reconciliation task runs every
minute, clears abandoned processing badges and resumes interrupted, unbounded
requests with backoff and a five-attempt limit for each page actually submitted.
Pages not yet submitted retain their retry allowance. An interrupted command with an
explicit page budget requires a new explicit budget. Ordinary quota failures do
not start an automatic retry loop. Ambiguous paid job-creation errors are saved
as pending rather than resubmitted automatically; polling retries reuse the job.
Known job IDs are saved privately before polling. Later page retries poll the
same job after a transient failure or worker restart, including pages retained
through splits, reordering and rotation. An interrupted submission without a
known job ID requires an explicit retry and review of the possible prior charge.
Terminal or expired jobs are not automatically replaced; an explicit retry can
authorize a new job and records a private review note.
With archive generation set to **never**, pending retries update OCR text and
page evidence without changing stored files, MIME types or thumbnails.

Initial classification, printed-date selection and document-added workflows wait
until all pending pages finish. They run once, using the workflows/actions that
were enabled at upload, and skip metadata changes if the user edited metadata
while waiting. File-update workflows wait for complete page text too. Interrupted
workflow actions receive a review note and are not replayed automatically, since
email or webhook actions may already have completed. Review the document before
explicitly running any remaining actions. Saved lifecycle state travels through
file history, page operations, and export/import.

The optional LLM search index is updated after page work completes, independently
of metadata workflow success. Its saved update request remains pending until the
worker confirms success for the current file and metadata. Broker outages and
worker loss are reconciled with leases and bounded retries. A recovery limit
leaves a private review note. Partial OCR is not embedded once per page. Broker
delivery is at least once: a crash after embedding but before its acknowledgement
can repeat an index job.
Tokenized workers verify the live document and its request after embedding,
before writing the local index. Deleted or superseded requests do not write.

Migration **0029** adds a transactional cleanup queue for retired files. Startup
migrations preserve document IDs, ASNs, content and page evidence. The new file
revision and its cleanup request commit together; scheduled reconciliation retries
cleanup after worker loss. Files still referenced by a document are retained,
and a reused path with a different file identity is retained for review. The
queue does not guess which pre-existing unreferenced files are safe to remove.

Status tags are global unless explicitly mapped to existing helper-owned tags.
For an installation that used the external helper, inspect its API user's tags:

```sh
python3 src/manage.py document_ocr \
  --legacy-helper-owner 12
```

This is a read-only candidate report. Verify the IDs before configuring them;
the helper's user ID and tag IDs are specific to the installation. For example:

```dotenv
PAPERLESS_OCR_STATUS_TAG_IDS={"ocr-fail":21,"ocr-processing":22,"ocr-ok":23,"ocr-review":24}
```

Mapping preserves tag ownership and existing workflow references. Unrelated
private tags with the same names are left alone. A mapped legacy failure tag
can queue a blank, unprocessed saved scan even if it has no native pending-page
marker. Existing successful OCR and rich page evidence are not treated as legacy
pending work. No reset or document re-upload is required.

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
The revision records its storage choice so deferred OCR retries and Reprocess
keep that choice even if the ingestion default changes. Custom history revisions
each hold one merged searchable PDF; earlier revisions supply the file history.
Automatic ingestion maintenance compacts its original root revision and preserves
later history updates. Explicit current-only compaction promotes the latest
revision and discards earlier history.

## Build

The web UI footer and system-status dialog show the personal release revision,
for example **Paperless-ngx v3.3.4-7 (personal)**. Release publication embeds its
verified release tag in the image. Local builds display **personal development**
unless built with a matching release tag:

```sh
docker build \
  --file Dockerfile.custom \
  --build-arg PERSONAL_RELEASE=paperless-3.3.4-7 \
  --tag paperless-ngx-personal:3.3.4-7 .
```

The upstream data-format version remains available for update checks, import
compatibility and frontend/backend compatibility checks.

The custom Dockerfile layers this fork's application and compiled UI onto the
pinned upstream v3.3.4 runtime:

```sh
docker build \
  --file Dockerfile.custom \
  --tag paperless-ngx-custom:3.3.4 .
```

## Personal releases

Pushes to `personal` run the full upstream backend and frontend tests, custom
feature tests, release-policy tests, lint and static analysis. Images are
published by **Publish personal release image** when a final GitHub release
is published with a tag such as `paperless-3.3.4-1`.

After integrating an upstream stable release:

1. Merge the upstream release into `personal`, resolve conflicts, and update
   the pinned runtime in `Dockerfile.custom` and the custom test image to
   that upstream release. Advance `stable` to the integrated upstream commit.
2. Wait for the checks on the exact `personal` commit to finish successfully.
3. Create and publish a final GitHub release targeting that tested commit.
   Use `paperless-<upstream-version>-<revision>`; the revision starts at `1`.
   A personal fix can get another revision without a new upstream release.
4. Wait for the image publication workflow, then update the deployment pin
   to its commit-specific image reference from the workflow summary.

The publication gate requires the tagged commit to belong to `personal` and
contain `stable`, with matching tag, application and stable versions and a
matching digest-pinned upstream runtime. The latest push run on `personal`
for each of the six required workflows must have succeeded for that exact
commit. PR checks, manual checks, skipped workflows, and older successful
attempts cannot substitute for failing or incomplete checks. Publishing a
release early fails the gate; rerun it after the checks pass.

Each release publishes two tags in `ghcr.io/renzbagaporo/paperless-ngx`:

- The release tag, such as `paperless-3.3.4-1`.
- `personal-<full-commit-sha>`, which deployments pin.

Editing a release title or Git tag does not rebuild its image. Existing image
references retain their build metadata. Commit-specific deployment pins remain
valid, so release renaming does not require redeployment.

Only release publication starts an image build; ordinary pushes and tag
creation run checks. Drafts, prereleases and upstream release names do not
publish personal images. The build checks out the verified commit even if
`personal` advances while it is running. Set the container package's visibility
to **Public** after its first publication so guests can pull without a registry
credential. The first release must include this workflow.

Use this image with the existing Paperless deployment configuration. This fork
does not automatically deploy or reset any instance.
