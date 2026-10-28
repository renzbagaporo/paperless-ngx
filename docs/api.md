# REST API

Paperless-ngx now ships with a fully-documented REST API and a browsable
web interface to explore it. The API browsable interface is available at
`/api/schema/view/`.

Further documentation is provided here for some endpoints and features.

## Authorization

The REST api provides five different forms of authentication.

1.  Basic authentication

    Authorize by providing a HTTP header in the form

    ```
    Authorization: Basic <credentials>
    ```

    where `credentials` is a base64-encoded string of
    `<username>:<password>`

2.  Session authentication

    When you're logged into paperless in your browser, you're
    automatically logged into the API as well and don't need to provide
    any authorization headers.

3.  Token authentication

    You can create (or re-create) an API token by opening the "My Profile"
    link in the user dropdown found in the web UI and clicking the circular
    arrow button.

    Paperless also offers an endpoint to acquire authentication tokens.

    POST a username and password as a form or json string to
    `/api/token/` and paperless will respond with a token, if the login
    data is correct. This token can be used to authenticate other
    requests with the following HTTP header:

    ```
    Authorization: Token <token>
    ```

    Tokens can also be managed in the Django admin.

4.  Remote User authentication

    If enabled (see
    [configuration](configuration.md#PAPERLESS_ENABLE_HTTP_REMOTE_USER_API)),
    you can authenticate against the API using Remote User auth.

5.  Headless OIDC via [`django-allauth`](https://codeberg.org/allauth/django-allauth)

    `django-allauth` exposes API endpoints under `api/auth/` which enable tools
    like third-party apps to authenticate with social accounts that are
    configured. See
    [here](advanced_usage.md#openid-connect-and-social-authentication) for more
    information on social accounts.

## Searching for documents

Full text searching is available on the `/api/documents/` endpoint. The
following query parameters cause the API to return Tantivy-backed search
results:

- `/api/documents/?text=your%20search%20query`: Search title and content
  using simple substring-style search.
- `/api/documents/?title_search=your%20search%20query`: Search title only
  using simple substring-style search.
- `/api/documents/?query=your%20search%20query`: Search for a document
  using a full text query. For details on the syntax, see [Basic Usage - Searching](usage.md#basic-usage_searching).
- `/api/documents/?more_like_id=1234`: Search for documents similar to
  the document with id 1234.

Pagination works exactly the same as it does for normal requests on this
endpoint.

Furthermore, each returned document has an additional `__search_hit__`
attribute with various information about the search results:

```
{
    "count": 31,
    "next": "http://localhost:8000/api/documents/?page=2&query=test",
    "previous": null,
    "results": [

        ...

        {
            "id": 123,
            "title": "title",
            "content": "content",

            ...

            "__search_hit__": {
                "score": 0.343,
                "highlights": "text <span class="match">Test</span> text",
                "rank": 23
            }
        },

        ...

    ]
}
```

- `score` is an indication how well this document matches the query
  relative to the other search results.
- `highlights` is an excerpt from the document content and highlights
  the search terms with `<span>` tags as shown above.
- `rank` is the index of the search results. The first result will
  have rank 0.

### Filtering by custom fields

You can filter documents by their custom field values by specifying the
`custom_field_query` query parameter. Here are some recipes for common
use cases:

1. Documents with a custom field "due" (date) between Aug 1, 2024 and
   Sept 1, 2024 (inclusive):

   `?custom_field_query=["due", "range", ["2024-08-01", "2024-09-01"]]`

2. Documents with a custom field "customer" (text) that equals "bob"
   (case sensitive):

   `?custom_field_query=["customer", "exact", "bob"]`

3. Documents with a custom field "answered" (boolean) set to `true`:

   `?custom_field_query=["answered", "exact", true]`

4. Documents with a custom field "favorite animal" (select) set to either
   "cat" or "dog":

   `?custom_field_query=["favorite animal", "in", ["cat", "dog"]]`

5. Documents with a custom field "address" (text) that is empty:

   `?custom_field_query=["OR", [["address", "isnull", true], ["address", "exact", ""]]]`

6. Documents that don't have a field called "foo":

   `?custom_field_query=["foo", "exists", false]`

7. Documents that have document links "references" to both document 3 and 7:

   `?custom_field_query=["references", "contains", [3, 7]]`

All field types support basic operations including `exact`, `in`, `isnull`,
and `exists`. String, URL, and monetary fields support case-insensitive
substring matching operations including `icontains`, `istartswith`, and
`iendswith`. Integer, float, and date fields support arithmetic comparisons
including `gt` (>), `gte` (>=), `lt` (<), `lte` (<=), and `range`.
Lastly, document link fields support a `contains` operator that behaves
like a "is superset of" check.

### `/api/search/autocomplete/`

Get auto completions for a partial search term.

Query parameters:

- `term`: The incomplete term.
- `limit`: Amount of results. Defaults to 10.

Results are ordered by how many of the user's visible documents contain
each matching word. The first result is the word that appears in the most documents.

```json
["term1", "term3", "term6", "term4"]
```

## POSTing documents {#file-uploads}

The API provides a special endpoint for file uploads:

`/api/documents/post_document/`

POST a multipart form to this endpoint, where the form field `document`
contains the document that you want to upload to paperless. The filename
is sanitized and then used to store the document in a temporary
directory, and the consumer will be instructed to consume the document
from there.

The endpoint supports the following optional form fields:

- `title`: Specify a title that the consumer should use for the
  document.
- `created`: Specify a DateTime where the document was created (e.g.
  "2016-04-19" or "2016-04-19 06:15:00+02:00").
- `correspondent`: Specify the ID of a correspondent that the consumer
  should use for the document.
- `document_type`: Similar to correspondent.
- `storage_path`: Similar to correspondent.
- `tags`: Similar to correspondent. Specify this multiple times to
  have multiple tags added to the document.
- `archive_serial_number`: An optional archive serial number to set.
- `custom_fields`: Either an array of custom field ids to assign (with an empty
  value) to the document or an object mapping field id -> value.

The endpoint will immediately return HTTP 200 if the document consumption
process was started successfully, with the UUID of the consumption task
as the data. No additional status information about the consumption process
itself is available immediately, since that happens in a different process.
However, querying the tasks endpoint with the returned UUID e.g.
`/api/tasks/?task_id={uuid}` will provide information on the state of the
consumption including the ID of a created document if consumption succeeded.

## Document Versions

Document versions are file-level versions linked to one root document.

- Root document metadata (title, tags, correspondent, document type, storage path, custom fields, permissions) remains shared.
- Version-specific file data (file, mime type, checksums, archive info, extracted text content) belongs to the selected/latest version.

Version-aware endpoints:

- `GET /api/documents/{id}/`: returns root document data; `content` resolves to latest version content by default. Use `?version={version_id}` to resolve content for a specific version.
- `PATCH /api/documents/{id}/`: content updates target the selected version (`?version={version_id}`) or latest version by default; non-content metadata updates target the root document.
- `GET /api/documents/{id}/download/`, `GET /api/documents/{id}/preview/`, `GET /api/documents/{id}/thumb/`, `GET /api/documents/{id}/metadata/`: accept `?version={version_id}`.
- `POST /api/documents/{id}/update_version/`: uploads a new version using multipart form field `document` and optional `version_label`.
- `POST /api/documents/merge_as_versions/`: merges existing top-level documents as versions of a selected root. The JSON body must contain `documents` (at least two document IDs) and `root_document_id` (one of those IDs). When merging one source document, an optional `version_label` may be provided.
- `PATCH /api/documents/{id}/versions/{version_id}/`: updates the `version_label` of a specific version.
- `DELETE /api/documents/{root_id}/versions/{version_id}/`: deletes a non-root version.

## Appending pages and replacing files

The custom branch adds two multipart endpoints:

- `POST /api/documents/{id}/append/`: OCR the uploaded PDF or image and append
  its pages after the latest searchable file of this document.
- `POST /api/documents/{id}/replace_file/`: OCR an uploaded PDF or image and use
  it as this document's current file.

Both accept `document`, `keep_only_current_file` (defaulting to
`PAPERLESS_STORE_ONLY_CURRENT_FILE`) and optional `remote_ocr`. Set
`remote_ocr=true` to request the configured remote parser, `false` to use a
local parser, or omit it to follow the configured remote OCR mode. They
return a consumption task UUID. The
completed task references the existing root document ID. Title, dates, ASN,
owner, permissions, tags, custom fields, notes and share links stay attached to
that document. The configured native parser handles only the new scan;
appending does not send existing pages through OCR again. Concurrent appends
are serialized and each uses the latest committed file.

With `keep_only_current_file=true`, earlier files and file versions are
permanently removed after processing commits successfully. The searchable PDF
becomes the root file, without an additional archive copy. Both global and
object-level change and delete permissions are required. With `false`, change
permission is enough and the combined or replacement PDF is retained as an
additional version. Permissions are checked again after OCR finishes.

Append additionally accepts `operations`, a JSON array of page operations
applied to the existing searchable PDF before the new scan is added. Page
numbers start at one; omitted pages are removed. Each operation accepts
`page` and optional `rotate` (a multiple of 90). Append produces one output,
so `doc` must be zero or omitted. An empty array retains no existing pages.
Without `operations`, all existing pages are retained.

`source_mode` defaults to `latest_version`; `explicit_selection` uses
`source_document_id` (the root or a version belonging to it). Supply
`source_checksum` from that version's `archive_checksum`, or `checksum` when
there is no archive. The editor uses these fields to reject stale edits.
Queued editor operations also reject a change to the current file while the
operation was waiting or performing OCR.

`POST /api/documents/edit_pdf/` supports `keep_only_current_file` and
`source_checksum` with `update_document=true`. It applies the same storage
and permission rules, preserves root metadata, extracts text from the edited
searchable PDF, and regenerates its thumbnail without OCR. The response is
`{"result":"OK","task_id":"<uuid>"}`; poll the ordinary tasks endpoint
until the job succeeds or fails. Split/create operations retain the upstream
behavior and return `{"result":"OK"}`.

In the document dashboard, open **Actions → PDF Editor** for appending and
page edits. Select the new scan and its OCR mode, then choose file retention.
Splitting creates new document IDs and cannot be combined with appending.
**Versions → Replace this document's file** supports replacement. Keeping
only the current file requires an explicit acknowledgement of history
removal. The ordinary **Add a file version** action remains available.

### Per-page parsing and extraction

New imports and file updates retain per-page plain text, Markdown and layout
items returned by LlamaParse. Local PDF text extraction supplies text-only
records when rich parsing is unavailable. Each record belongs to an exact
searchable-file checksum. Reorder, rotate, delete and append operations preserve
the corresponding records. Layout coordinates refer to the parsed page;
`rotation` tracks clockwise rotation, modulo 360 degrees. Subtract
`provenance.layout_rotation` (default zero) to transform layout coordinates
from the parsing orientation to the current orientation.

`GET /api/documents/{id}/page_data/` returns the latest file's page records.
An optional `version` query parameter selects a file version belonging to the
same root. The response contains `source_document_id`, `file_checksum`,
`available` and `pages`. Missing or stale records produce `available=false`;
the read endpoint never starts OCR or backfill. Document view permissions apply.
Text-only pages have null `markdown` and `layout`. Provider image download URLs
are excluded from retained layout data; extracted image files are not stored.

`POST /api/documents/{id}/split_pages/` copies selected searchable pages to a
new document without invoking a parser. Send, for example:

```json
{
  "operations": [{ "page": 3, "rotate": 90 }, { "page": 1 }],
  "source_document_id": 42,
  "source_checksum": "<file_checksum from page_data>",
  "move": false,
  "title": "Selected water notices",
  "archive_serial_number": 1000002,
  "keep_only_current_file": true
}
```

The response contains `document_id` and `new_document_id`. Page numbers are
one-based and must be unique. The source checksum is required. Omit the ASN to
leave the new document's ASN unset. An occupied ASN is rejected. Ownership,
object permissions, tags, custom fields and classification metadata are copied;
notes and share links remain on the source. A provenance note identifies the
source file and selected pages on the new document.

Set `move=true` to remove the selected pages from the source in the same
transaction. Moving every page is rejected. View and add permissions are
required for extraction; moving also requires change permission and, when
discarding history, delete permission. Root identity and ASN are preserved.
File history retains its matching parsing data; keeping only the current file
removes obsolete versions and their parsing data after commit.

In **Actions → PDF Editor**, select pages and choose **Copy selected pages to a
new document** or **Move selected pages to a new document** from **Selected
pages**. These actions preserve existing OCR and rich parsing. Append cannot be
combined with extraction. The upstream scissors/create workflow is separate.

### Migrating and enriching existing documents

Back up the database and media together before deploying the schema migration.
The schema migration only creates the page-data table; it does not change
documents, files or searchable content and does not invoke OCR. A local backfill
recovers text from existing searchable PDFs, including retained file versions.
It cannot recover the discarded Markdown or layout. Preview first, then apply
inside the Paperless runtime:

```sh
python manage.py document_page_data backfill
python manage.py document_page_data backfill --apply
```

Backfill is resumable, skips complete current records, checks file checksums and
page counts, and reports damaged or missing files without losing successes.
An optional repeated `--document` argument limits the selected documents.

Rich parsing can be added later without resetting documents or rewriting PDF
files. Enrichment processes one missing page at a time, preserving IDs, ASNs,
metadata, embedded PDF text and document-level searchable content. Rich text is
available through the page-data endpoint; later page edits rebuild searchable
content from the retained page records. Preview the missing page count before
explicitly authorizing a bounded number of paid pages:

```sh
python manage.py document_page_data enrich
python manage.py document_page_data enrich --apply --max-pages 10
```

Enrichment uses the configured LlamaParse engine and key. It saves each success
immediately, skips already enriched pages on rerun, and stops on any error,
including exhausted credits. A rerun resumes from missing pages. The page limit
is shared across the selected documents. Default selection enriches only the
latest file per root; `--include-history` includes retained versions. A crash
after the provider finishes but before the database commit can still require a
repeat provider call; billing is not guaranteed to be exactly once.

Both ordinary and split-manifest exports include page parsing records. Restoring
from Trash preserves the matching page data; permanent deletion removes it.
Restore database and media from the same backup to roll back a deployment. Reversing
the schema migration preserves documents but drops the page-data table, so it
discards newly retained parsing results.

## Permissions

All objects (documents, tags, etc.) allow setting object-level permissions
with optional `owner` and / or a `set_permissions` parameters which are of
the form:

```
"owner": ...,
"set_permissions": {
    "view": {
        "users": [...],
        "groups": [...],
    },
    "change": {
        "users": [...],
        "groups": [...],
    },
}
```

!!! note

    Arrays should contain user or group ID numbers.

If these parameters are supplied the object's permissions will be overwritten,
assuming the authenticated user has permission to do so (the user must be
the object owner or a superuser).

### Retrieving full permissions

By default, the API will return a truncated version of object-level
permissions, returning `user_can_change` indicating whether the current user
can edit the object (either because they are the object owner or have permissions
granted). You can pass the parameter `full_perms=true` to API calls to view the
full permissions of objects in a format that mirrors the `set_permissions`
parameter above.

## Bulk Editing

The API supports various bulk-editing operations which are executed asynchronously.

### Documents

For bulk operations on documents, use the endpoint `/api/documents/bulk_edit/` which accepts
a json payload of the format:

```json
{
  "documents": [LIST_OF_DOCUMENT_IDS],
  "method": METHOD, // see below
  "parameters": args // see below
}
```

The following methods are supported:

- `set_correspondent`
  - Requires `parameters`: `{ "correspondent": CORRESPONDENT_ID }`
- `set_document_type`
  - Requires `parameters`: `{ "document_type": DOCUMENT_TYPE_ID }`
- `set_storage_path`
  - Requires `parameters`: `{ "storage_path": STORAGE_PATH_ID }`
- `add_tag`
  - Requires `parameters`: `{ "tag": TAG_ID }`
- `remove_tag`
  - Requires `parameters`: `{ "tag": TAG_ID }`
- `modify_tags`
  - Requires `parameters`: `{ "add_tags": [LIST_OF_TAG_IDS] }` and `{ "remove_tags": [LIST_OF_TAG_IDS] }`
- `delete`
  - No `parameters` required
- `reprocess`
  - Optional `parameters`: `{ "remote_ocr": true }` to send the documents to the
    remote OCR engine, see [Remote OCR](usage.md#remote-ocr). Defaults to false.
- `set_permissions`
  - Requires `parameters`:
    - `"set_permissions": PERMISSIONS_OBJ` (see format [above](#permissions)) and / or
    - `"owner": OWNER_ID or null`
    - `"merge": true or false` (defaults to false)
  - The `merge` flag determines if the supplied permissions will overwrite all existing permissions (including
    removing them) or be merged with existing permissions.
- `modify_custom_fields`
  - Requires `parameters`:
    - `"add_custom_fields": { CUSTOM_FIELD_ID: VALUE }`: JSON object consisting of custom field id:value pairs to add to the document, can also be a list of custom field IDs
      to add with empty values.
    - `"remove_custom_fields": [CUSTOM_FIELD_ID]`: custom field ids to remove from the document.

#### Document-editing operations

Beginning with version 10+, the API supports individual endpoints for document-editing operations (`merge`, `rotate`, `edit_pdf`, etc), thus their documentation can be found in the API spec / viewer. Legacy document-editing methods via `/api/documents/bulk_edit/` are still supported for compatibility, are deprecated and clients should migrate to the individual endpoints before they are removed in a future version.

### Objects

Bulk editing for objects (tags, document types etc.) currently supports set permissions or delete
operations, using the endpoint: `/api/bulk_edit_objects/`, which requires a json payload of the format:

```json
{
  "objects": [LIST_OF_OBJECT_IDS],
  "object_type": "tags", "correspondents", "document_types" or "storage_paths",
  "operation": "set_permissions" or "delete",
  "owner": OWNER_ID, // optional
  "permissions": { "view": { "users": [] ... }, "change": { ... } }, // (see 'set_permissions' format above)
  "merge": true / false // defaults to false, see above
}
```

## API Versioning

The REST API is versioned.

- Versioning ensures that changes to the API don't break older
  clients.
- Clients specify the specific version of the API they wish to use
  with every request and Paperless will handle the request using the
  specified API version.
- Even if the underlying data model changes, supported older API
  versions continue to serve compatible data.
- If no version is specified, Paperless serves the configured default
  API version (currently `10`).
- Supported API versions are currently `9` and `10`.

API versions are specified by submitting an additional HTTP `Accept`
header with every request:

```
Accept: application/json; version=10
```

If an invalid version is specified, Paperless responds with
`406 Not Acceptable` and an error message in the body.

If a client wishes to verify whether it is compatible with any given
server, the following procedure should be performed:

1.  Perform an _authenticated_ request against any API endpoint. The
    server will add two custom headers to the response:

    ```
    X-Api-Version: 10
    X-Version: <server-version>
    ```

2.  Determine whether the client is compatible with this server based on
    the presence/absence of these headers and their values if present.

### API Version Deprecation Policy

Older API versions are guaranteed to be supported for at least one year
after the release of a new API version. After that, support for older
API versions may be (but is not guaranteed to be) dropped.

### API Changelog

#### Version 1

Initial API version.

#### Version 2

- Added field `Tag.color`. This read/write string field contains a hex
  color such as `#a6cee3`.
- Added read-only field `Tag.text_color`. This field contains the text
  color to use for a specific tag, which is either black or white
  depending on the brightness of `Tag.color`.
- Removed field `Tag.colour`.

#### Version 3

- Permissions endpoints have been added.
- The format of the `/api/ui_settings/` has changed.

#### Version 4

- Consumption templates were refactored to workflows and API endpoints
  changed as such.

#### Version 5

- Added bulk deletion methods for documents and objects.

#### Version 6

- Moved acknowledge tasks endpoint to be under `/api/tasks/acknowledge/`.

#### Version 7

- The format of select type custom fields has changed to return the options
  as an array of objects with `id` and `label` fields as opposed to a simple
  list of strings. When creating or updating a custom field value of a
  document for a select type custom field, the value should be the `id` of
  the option whereas previously was the index of the option.

#### Version 8

- The user field of document notes now returns a simplified user object
  rather than just the user ID.

#### Version 9

- The document `created` field is now a date, not a datetime. The
  `created_date` field is considered deprecated and will be removed in a
  future version.

#### Version 10

- The `show_on_dashboard` and `show_in_sidebar` fields of saved views have been
  removed. Relevant settings are now stored in the UISettings model. Compatibility is maintained
  for versions < 10 until support for API v9 is dropped.
- Document-editing operations such as `merge`, `rotate`, and `edit_pdf` have been
  moved from the bulk edit endpoint to their own individual endpoints. Using these methods via
  the bulk edit endpoint is still supported for compatibility with versions < 10 until support
  for API v9 is dropped.
- The `all` parameter of list endpoints is now deprecated and will be removed in a future version.
- The bulk edit objects endpoint now supports `all` and `filters` parameters to avoid having to send
  large lists of object IDs for operations affecting many objects.
- The legacy `title_content` document search parameter is deprecated and will be removed in a future version.
  Clients should use `text` for simple title-and-content search and `title_search` for title-only search.
- The task tracking system was redesigned. The tasks list (`/api/tasks/`) is now paginated, and the
  task object exposes `task_type` (formerly `task_name`) and `trigger_source` (formerly `type`). New
  read-only endpoints `/api/tasks/summary/`, `/api/tasks/status_counts/`, and `/api/tasks/active/`
  provide aggregate views, and `POST /api/tasks/run/` lets privileged users dispatch supported tasks.
  API v9 continues to serve the unpaginated list with the legacy field names until support for v9 is
  dropped.
