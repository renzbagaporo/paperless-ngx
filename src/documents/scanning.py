"""
Scanning from the web UI through network scanners that speak eSCL (AirScan),
including SANE devices shared by AirSane.

A scan runs in a background thread of the web process so it starts at once,
independent of the Celery queue. Its state and result live in
``SCRATCH_DIR/web_scans/<job id>/``; the browser polls the job and then hands
the finished PDF to the ordinary upload or document-update endpoints, so
ingestion, permissions and OCR follow the same paths as an uploaded file.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
import img2pdf
import pikepdf
from django.conf import settings
from django.core.cache import cache
from filelock import FileLock
from filelock import Timeout

logger = logging.getLogger("paperless.scanning")

NS = {
    "scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
    "pwg": "http://www.pwg.org/schemas/2010/12/sm",
}

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
DEVICE_LINK_RE = re.compile(r"""href=['"]([^'"]+)['"]""", re.IGNORECASE)
# AirSane serves each device below a UUID path.
DEVICE_PATH_RE = re.compile(
    r"^/(?:[^/]+/)*[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/?$",
)
JOB_EXPIRY_SECONDS = 60 * 60
CAPABILITIES_CACHE_SECONDS = 5 * 60
CONNECT_TIMEOUT = 5.0
# A page is delivered only after the scanner finishes it.
PAGE_TIMEOUT = 180.0
# Seconds a job waits for the scanner to produce the next page.
NOT_READY_LIMIT = 120
# A scanning job whose state has not changed for this long has died with its
# web worker.
STALE_SECONDS = 10 * 60

# Paper sizes in 1/300 inch, the unit eSCL scan regions use.
PAPER_SIZES: dict[str, tuple[int, int]] = {
    "a4": (2480, 3508),
    "letter": (2550, 3300),
    "legal": (2550, 4200),
    "a5": (1748, 2480),
}
COLOR_MODES = {"color": "RGB24", "grayscale": "Grayscale8", "bw": "BlackAndWhite1"}

# Each source maps to an eSCL capability element and the job settings for it.
SOURCES: dict[str, tuple[str, str, bool]] = {
    "flatbed": ("scan:Platen/scan:PlatenInputCaps", "Platen", False),
    "feeder": ("scan:Adf/scan:AdfSimplexInputCaps", "Feeder", False),
    "feeder_duplex": ("scan:Adf/scan:AdfDuplexInputCaps", "Feeder", True),
}


class ScanError(Exception):
    """A scan could not start or finish; the message is shown to the user."""


class ScannerBusy(ScanError):
    pass


@dataclass(frozen=True)
class ScannerConfig:
    id: str
    url: str


@dataclass(frozen=True)
class ScanOptions:
    source: str
    color_mode: str
    resolution: int
    paper_size: str
    # Feeder only: stop after this many sheets instead of emptying the feeder.
    max_sheets: int | None = None

    def page_limit(self) -> int | None:
        if self.max_sheets is None or not self.source.startswith("feeder"):
            return None
        _, _, duplex = SOURCES[self.source]
        return self.max_sheets * (2 if duplex else 1)


def _scanner(url: str) -> ScannerConfig:
    url = url if url.endswith("/") else url + "/"
    return ScannerConfig(id=hashlib.sha256(url.encode()).hexdigest()[:12], url=url)


def _device_urls(server_url: str) -> list[str] | None:
    """
    The eSCL device URLs behind a configured URL, or None when it cannot be
    reached. An AirSane server's index page links to each device it publishes,
    so devices it adds later appear without changing the configuration. Any
    other URL is taken to be a device itself.
    """
    cache_key = "scanner-devices-" + hashlib.sha256(server_url.encode()).hexdigest()
    devices = cache.get(cache_key)
    if devices is not None:
        return devices
    try:
        with _client() as client:
            response = client.get(
                server_url,
                timeout=httpx.Timeout(15.0, connect=CONNECT_TIMEOUT),
            )
    except httpx.HTTPError as e:
        logger.warning("Scanner server %s is unavailable: %s", server_url, e)
        return None
    devices = []
    if response.status_code == 200 and "html" in response.headers.get(
        "Content-Type",
        "",
    ):
        server = urlparse(server_url)
        for href in DEVICE_LINK_RE.findall(response.text):
            device_url = urljoin(server_url, href)
            parsed = urlparse(device_url)
            if parsed.netloc == server.netloc and DEVICE_PATH_RE.match(parsed.path):
                device_url = (
                    device_url if device_url.endswith("/") else device_url + "/"
                )
                if device_url != server_url and device_url not in devices:
                    devices.append(device_url)
    if not devices:
        devices = [server_url]
    cache.set(cache_key, devices, CAPABILITIES_CACHE_SECONDS)
    return devices


def discover_scanners() -> list[tuple[ScannerConfig, bool]]:
    """Each scanner with whether its server answered."""
    scanners = []
    for raw in settings.SCANNERS:
        server = _scanner(raw)
        devices = _device_urls(server.url)
        if devices is None:
            scanners.append((server, False))
        else:
            scanners.extend((_scanner(url), True) for url in devices)
    return scanners


def get_scanner(scanner_id: str) -> ScannerConfig | None:
    return next(
        (s for s, reachable in discover_scanners() if reachable and s.id == scanner_id),
        None,
    )


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(PAGE_TIMEOUT, connect=CONNECT_TIMEOUT),
        follow_redirects=False,
    )


def _ints(element: ET.Element, path: str) -> list[int]:
    return sorted(
        {int(e.text) for e in element.findall(path, NS) if e.text and e.text.isdigit()},
    )


def parse_capabilities(xml: bytes) -> dict[str, Any]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ScanError("The scanner returned unreadable capabilities.") from e

    make_and_model = root.findtext("pwg:MakeAndModel", "", NS).strip()
    sources = []
    for source_id, (path, _, _) in SOURCES.items():
        caps = root.find(path, NS)
        if caps is None:
            continue
        color_modes = {e.text for e in caps.findall(".//scan:ColorMode", NS) if e.text}
        formats = {e.text for e in caps.findall(".//pwg:DocumentFormat", NS)} | {
            e.text for e in caps.findall(".//scan:DocumentFormatExt", NS)
        }
        sources.append(
            {
                "id": source_id,
                "color_modes": [m for m, v in COLOR_MODES.items() if v in color_modes],
                "resolutions": _ints(
                    caps,
                    ".//scan:DiscreteResolution/scan:XResolution",
                ),
                "max_width": int(caps.findtext("scan:MaxWidth", "0", NS) or 0),
                "max_height": int(caps.findtext("scan:MaxHeight", "0", NS) or 0),
                "pdf": "application/pdf" in formats,
            },
        )
    return {
        # AirSane appends the device serial after a colon.
        "name": make_and_model.split(":", 1)[0] or None,
        "sources": sources,
    }


def get_capabilities(scanner: ScannerConfig) -> dict[str, Any]:
    cache_key = f"scanner-capabilities-{scanner.id}"
    caps = cache.get(cache_key)
    if caps is None:
        try:
            with _client() as client:
                response = client.get(
                    urljoin(scanner.url, "ScannerCapabilities"),
                    timeout=httpx.Timeout(15.0, connect=CONNECT_TIMEOUT),
                )
                response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Scanner %s is unavailable: %s", scanner.url, e)
            raise ScanError("The scanner is not reachable.") from e
        caps = parse_capabilities(response.content)
        cache.set(cache_key, caps, CAPABILITIES_CACHE_SECONDS)
    return caps


def describe_scanners() -> list[dict[str, Any]]:
    result = []
    for scanner, reachable in discover_scanners():
        host = urlparse(scanner.url).hostname or scanner.url
        entry: dict[str, Any] = {"id": scanner.id, "host": host}
        try:
            if not reachable:
                raise ScanError("The scanner is not reachable.")
            caps = get_capabilities(scanner)
        except ScanError as e:
            entry.update(name=host, available=False, error=str(e), sources=[])
        else:
            entry.update(
                name=caps["name"] or host,
                available=True,
                error=None,
                sources=caps["sources"],
            )
        result.append(entry)
    names = [e["name"] for e in result]
    for entry in result:
        if names.count(entry["name"]) > 1:
            entry["name"] = f"{entry['name']} ({entry['host']})"
    return result


def build_scan_settings(options: ScanOptions, source_caps: dict[str, Any]) -> bytes:
    width, height = PAPER_SIZES[options.paper_size]
    if source_caps["max_width"]:
        width = min(width, source_caps["max_width"])
    if source_caps["max_height"]:
        height = min(height, source_caps["max_height"])
    _, input_source, duplex = SOURCES[options.source]
    document_format = "application/pdf" if source_caps["pdf"] else "image/jpeg"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{NS["scan"]}" xmlns:pwg="{NS["pwg"]}">
  <pwg:Version>2.0</pwg:Version>
  <scan:Intent>TextAndGraphic</scan:Intent>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>{width}</pwg:Width>
      <pwg:Height>{height}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>{input_source}</pwg:InputSource>
  <scan:Duplex>{"true" if duplex else "false"}</scan:Duplex>
  <scan:ColorMode>{COLOR_MODES[options.color_mode]}</scan:ColorMode>
  <scan:XResolution>{options.resolution}</scan:XResolution>
  <scan:YResolution>{options.resolution}</scan:YResolution>
  <pwg:DocumentFormat>{document_format}</pwg:DocumentFormat>
  <scan:DocumentFormatExt>{document_format}</scan:DocumentFormatExt>
</scan:ScanSettings>
""".encode()


def validate_options(options: ScanOptions, caps: dict[str, Any]) -> dict[str, Any]:
    source_caps = next((s for s in caps["sources"] if s["id"] == options.source), None)
    if source_caps is None:
        raise ScanError("The scanner does not offer this source.")
    if options.color_mode not in source_caps["color_modes"]:
        raise ScanError("The scanner does not offer this color mode.")
    if options.resolution not in source_caps["resolutions"]:
        raise ScanError("The scanner does not offer this resolution.")
    if options.paper_size not in PAPER_SIZES:
        raise ScanError("Unknown paper size.")
    return source_caps


def _check_ready(client: httpx.Client, scanner: ScannerConfig) -> None:
    # The feeder's paper sensor is not consulted: SANE drivers behind AirSane
    # often report an empty feeder while pages are loaded.
    try:
        response = client.get(
            urljoin(scanner.url, "ScannerStatus"),
            timeout=httpx.Timeout(15.0, connect=CONNECT_TIMEOUT),
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (httpx.HTTPError, ET.ParseError) as e:
        raise ScanError("The scanner is not reachable.") from e
    state = root.findtext("pwg:State", "", NS)
    if state and state != "Idle":
        raise ScannerBusy("The scanner is busy.")


# Job storage ----------------------------------------------------------------


def _jobs_dir() -> Path:
    path = Path(settings.SCRATCH_DIR) / "web_scans"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_dir(job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id):
        raise FileNotFoundError(job_id)
    return _jobs_dir() / job_id


def _write_state(job_dir: Path, state: dict[str, Any]) -> None:
    tmp = job_dir / "state.json.tmp"
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(job_dir / "state.json")


def read_job(job_id: str, user_id: int) -> dict[str, Any] | None:
    try:
        job_dir = _job_dir(job_id)
        state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if state.get("owner") != user_id:
        return None
    if (
        state["status"] == "scanning"
        and time.time() - (job_dir / "state.json").stat().st_mtime > STALE_SECONDS
    ):
        state.update(status="failed", error="The scan was interrupted.")
    return state


def job_file(job_id: str, user_id: int) -> Path | None:
    state = read_job(job_id, user_id)
    if state is None or state["status"] != "done":
        return None
    return _job_dir(job_id) / "scan.pdf"


def edit_job_pages(
    job_id: str,
    user_id: int,
    operations: list[dict[str, int]],
) -> dict[str, Any] | None:
    """
    Rewrite a finished scan with the given pages, in order, each turned
    clockwise by its rotation. Pages left out are removed.
    """
    state = read_job(job_id, user_id)
    if state is None:
        return None
    if state["status"] != "done":
        raise ScanError("Only a finished scan can be edited.")
    pages = [op["page"] for op in operations]
    if not pages:
        raise ScanError("Keep at least one page.")
    if len(set(pages)) != len(pages) or not all(
        1 <= page <= state["pages"] for page in pages
    ):
        raise ScanError("The page list does not match the scan.")

    job_dir = _job_dir(job_id)
    with FileLock(job_dir / "edit.lock"):
        # Re-read under the lock in case another edit just finished.
        state = read_job(job_id, user_id)
        if state is None or state["pages"] < max(pages):
            raise ScanError("The page list does not match the scan.")
        source_path = job_dir / "scan.pdf"
        tmp = job_dir / "scan.pdf.tmp"
        with pikepdf.Pdf.open(source_path) as source, pikepdf.Pdf.new() as edited:
            for op in operations:
                edited.pages.append(source.pages[op["page"] - 1])
                if op["rotate"]:
                    edited.pages[-1].rotate(op["rotate"], relative=True)
            edited.save(tmp)
        tmp.replace(source_path)
        state.update(pages=len(pages), revision=state.get("revision", 0) + 1)
        _write_state(job_dir, state)
    return state


def delete_job(job_id: str, user_id: int) -> bool:
    state = read_job(job_id, user_id)
    if state is None:
        return False
    job_dir = _job_dir(job_id)
    if state["status"] == "scanning":
        # The scanning thread sees this, cancels the scanner job and cleans up.
        (job_dir / "cancel").touch()
    else:
        shutil.rmtree(job_dir, ignore_errors=True)
    return True


def purge_expired_jobs() -> None:
    cutoff = time.time() - JOB_EXPIRY_SECONDS
    for job_dir in _jobs_dir().iterdir():
        if not job_dir.is_dir() or not JOB_ID_RE.match(job_dir.name):
            continue
        try:
            if job_dir.stat().st_mtime < cutoff:
                shutil.rmtree(job_dir, ignore_errors=True)
        except FileNotFoundError:
            continue


# Running a scan -------------------------------------------------------------


def start_scan(
    scanner: ScannerConfig,
    options: ScanOptions,
    user_id: int,
    continue_job: str | None = None,
) -> str:
    """
    Start a scan and return its job ID. With ``continue_job``, the new pages
    follow that finished job's pages in one PDF, and the earlier job is
    removed once the new scan succeeds.
    """
    purge_expired_jobs()
    caps = get_capabilities(scanner)
    source_caps = validate_options(options, caps)
    previous = None
    if continue_job is not None:
        previous = job_file(continue_job, user_id)
        if previous is None or not previous.exists():
            raise ScanError("The earlier scan is no longer available.")
        # Keep the earlier job from expiring while this one runs.
        previous.parent.touch()

    lock = FileLock(
        _jobs_dir() / f"scanner-{scanner.id}.lock",
        timeout=0,
        thread_local=False,
    )
    try:
        lock.acquire()
    except Timeout as e:
        raise ScannerBusy("Another scan is in progress on this scanner.") from e

    try:
        with _client() as client:
            _check_ready(client, scanner)
        job_id = uuid.uuid4().hex
        job_dir = _jobs_dir() / job_id
        job_dir.mkdir()
        state = {
            "id": job_id,
            "owner": user_id,
            "scanner": scanner.id,
            "status": "scanning",
            "pages": 0,
            # Increases with each page edit so the browser reloads the file.
            "revision": 0,
            "error": None,
        }
        _write_state(job_dir, state)
        thread = threading.Thread(
            target=_run_job,
            args=(scanner, options, source_caps, job_dir, state, lock, previous),
            name=f"scan-{job_id}",
            daemon=True,
        )
        thread.start()
    except BaseException:
        lock.release()
        raise
    return job_id


def _run_job(
    scanner: ScannerConfig,
    options: ScanOptions,
    source_caps: dict[str, Any],
    job_dir: Path,
    state: dict[str, Any],
    lock: FileLock,
    previous: Path | None,
) -> None:
    try:
        parts = _scan(scanner, options, source_caps, job_dir, state)
        if (job_dir / "cancel").exists():
            shutil.rmtree(job_dir, ignore_errors=True)
            return
        if not parts:
            raise ScanError(
                "The scanner returned no pages. Check that the pages are loaded.",
            )
        try:
            pages = _merge(parts, job_dir / "scan.pdf", previous)
        except FileNotFoundError as e:
            raise ScanError("The earlier scan is no longer available.") from e
        state.update(status="done", pages=pages)
        _write_state(job_dir, state)
        if previous is not None:
            shutil.rmtree(previous.parent, ignore_errors=True)
    except ScanError as e:
        _fail(job_dir, state, str(e))
    except Exception:
        logger.exception("Scan on %s failed", scanner.url)
        _fail(job_dir, state, "The scan failed. See the server log for details.")
    finally:
        lock.release()


def _fail(job_dir: Path, state: dict[str, Any], message: str) -> None:
    if not job_dir.exists():
        return
    if (job_dir / "cancel").exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        return
    state.update(status="failed", error=message)
    _write_state(job_dir, state)


def _scan(
    scanner: ScannerConfig,
    options: ScanOptions,
    source_caps: dict[str, Any],
    job_dir: Path,
    state: dict[str, Any],
) -> list[tuple[str, bytes]]:
    parts: list[tuple[str, bytes]] = []
    page_limit = options.page_limit()
    with _client() as client:
        try:
            response = client.post(
                urljoin(scanner.url, "ScanJobs"),
                content=build_scan_settings(options, source_caps),
                headers={"Content-Type": "text/xml"},
            )
        except httpx.HTTPError as e:
            raise ScanError("The scanner is not reachable.") from e
        if response.status_code in (409, 503):
            raise ScannerBusy("The scanner is busy.")
        if response.status_code != 201 or "Location" not in response.headers:
            raise ScanError(
                f"The scanner refused the scan (HTTP {response.status_code}).",
            )
        job_url = urljoin(scanner.url, response.headers["Location"]).rstrip("/") + "/"

        not_ready = 0
        while True:
            if (job_dir / "cancel").exists():
                _cancel_remote(client, job_url)
                return parts
            try:
                response = client.get(urljoin(job_url, "NextDocument"))
            except httpx.HTTPError as e:
                _cancel_remote(client, job_url)
                raise ScanError("The connection to the scanner was lost.") from e
            if response.status_code == 404:
                break
            if response.status_code == 503 and not_ready < NOT_READY_LIMIT:
                not_ready += 1
                time.sleep(1)
                continue
            if response.status_code != 200:
                _cancel_remote(client, job_url)
                if not parts and response.status_code in (409, 503):
                    raise ScanError("Load the pages into the scanner and try again.")
                raise ScanError(
                    f"The scanner stopped with an error (HTTP {response.status_code}).",
                )
            not_ready = 0
            content_type = response.headers.get("Content-Type", "").split(";")[0]
            parts.append((content_type.strip().lower(), response.content))
            state["pages"] = len(parts)
            _write_state(job_dir, state)
            if page_limit is not None and len(parts) >= page_limit:
                # Cancelling stops the feeder; the remaining sheets stay put.
                _cancel_remote(client, job_url)
                break
    return parts


def _cancel_remote(client: httpx.Client, job_url: str) -> None:
    try:
        client.delete(job_url.rstrip("/"), timeout=10.0)
    except httpx.HTTPError:
        logger.debug("Could not cancel scanner job %s", job_url)


def _merge(
    parts: list[tuple[str, bytes]],
    output: Path,
    previous: Path | None = None,
) -> int:
    with pikepdf.Pdf.new() as merged:
        if previous is not None:
            merged.pages.extend(pikepdf.Pdf.open(previous).pages)
        for content_type, data in parts:
            if content_type == "application/pdf" or data.startswith(b"%PDF"):
                source = pikepdf.Pdf.open(io.BytesIO(data))
            else:
                source = pikepdf.Pdf.open(io.BytesIO(img2pdf.convert(data)))
            merged.pages.extend(source.pages)
        merged.save(output)
        return len(merged.pages)
