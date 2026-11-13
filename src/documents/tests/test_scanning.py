import io
import time
from typing import Any

import pikepdf
import pytest
from django.contrib.auth.models import Permission
from django.contrib.auth.models import User
from PIL import Image
from pytest_django.fixtures import Settings
from pytest_httpx import HTTPXMock
from rest_framework import status
from rest_framework.test import APIClient

from documents import scanning

SERVER = "http://airsane.test:8090/"
DEVICE_UUID = "61819d17-e017-5252-8bbc-2cc241adaf59"
DEVICE = f"{SERVER}{DEVICE_UUID}/"
JOB = f"{DEVICE}ScanJobs/1234"

CAPABILITIES = f"""<?xml version='1.0' encoding='UTF-8'?>
<scan:ScannerCapabilities xmlns:pwg='http://www.pwg.org/schemas/2010/12/sm'
    xmlns:scan='http://schemas.hp.com/imaging/escl/2011/05/03'>
<pwg:Version>2.0</pwg:Version>
<pwg:MakeAndModel>EPSON DS-571W:5838514B3030333542</pwg:MakeAndModel>
<scan:UUID>{DEVICE_UUID}</scan:UUID>
<scan:Adf>
<scan:AdfSimplexInputCaps>
<scan:MaxWidth>2550</scan:MaxWidth><scan:MaxHeight>4650</scan:MaxHeight>
<scan:SettingProfiles><scan:SettingProfile>
<scan:ColorModes><scan:ColorMode>Grayscale8</scan:ColorMode>
<scan:ColorMode>RGB24</scan:ColorMode></scan:ColorModes>
<scan:SupportedResolutions><scan:DiscreteResolutions>
<scan:DiscreteResolution><scan:XResolution>150</scan:XResolution>
<scan:YResolution>150</scan:YResolution></scan:DiscreteResolution>
<scan:DiscreteResolution><scan:XResolution>300</scan:XResolution>
<scan:YResolution>300</scan:YResolution></scan:DiscreteResolution>
</scan:DiscreteResolutions></scan:SupportedResolutions>
<scan:DocumentFormats><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>
</scan:DocumentFormats>
</scan:SettingProfile></scan:SettingProfiles>
</scan:AdfSimplexInputCaps>
<scan:AdfDuplexInputCaps>
<scan:MaxWidth>2550</scan:MaxWidth><scan:MaxHeight>4650</scan:MaxHeight>
<scan:SettingProfiles><scan:SettingProfile>
<scan:ColorModes><scan:ColorMode>RGB24</scan:ColorMode></scan:ColorModes>
<scan:SupportedResolutions><scan:DiscreteResolutions>
<scan:DiscreteResolution><scan:XResolution>300</scan:XResolution>
<scan:YResolution>300</scan:YResolution></scan:DiscreteResolution>
</scan:DiscreteResolutions></scan:SupportedResolutions>
<scan:DocumentFormats><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>
</scan:DocumentFormats>
</scan:SettingProfile></scan:SettingProfiles>
</scan:AdfDuplexInputCaps>
</scan:Adf>
</scan:ScannerCapabilities>
""".encode()

FLATBED_CAPABILITIES = b"""<?xml version='1.0' encoding='UTF-8'?>
<scan:ScannerCapabilities xmlns:pwg='http://www.pwg.org/schemas/2010/12/sm'
    xmlns:scan='http://schemas.hp.com/imaging/escl/2011/05/03'>
<pwg:MakeAndModel>EPSON GT-S660</pwg:MakeAndModel>
<scan:Platen><scan:PlatenInputCaps>
<scan:MaxWidth>2550</scan:MaxWidth><scan:MaxHeight>3508</scan:MaxHeight>
<scan:SettingProfiles><scan:SettingProfile>
<scan:ColorModes><scan:ColorMode>RGB24</scan:ColorMode></scan:ColorModes>
<scan:SupportedResolutions><scan:DiscreteResolutions>
<scan:DiscreteResolution><scan:XResolution>300</scan:XResolution>
<scan:YResolution>300</scan:YResolution></scan:DiscreteResolution>
</scan:DiscreteResolutions></scan:SupportedResolutions>
<scan:DocumentFormats><pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
</scan:DocumentFormats>
</scan:SettingProfile></scan:SettingProfiles>
</scan:PlatenInputCaps></scan:Platen>
</scan:ScannerCapabilities>
"""


def _status(adf_state: str = "ScannerAdfLoaded", state: str = "Idle") -> bytes:
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<scan:ScannerStatus xmlns:pwg='http://www.pwg.org/schemas/2010/12/sm'
    xmlns:scan='http://schemas.hp.com/imaging/escl/2011/05/03'>
<pwg:State>{state}</pwg:State><scan:AdfState>{adf_state}</scan:AdfState>
</scan:ScannerStatus>""".encode()


def _pdf(pages: int = 1) -> bytes:
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page()
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(out, format="JPEG")
    return out.getvalue()


def _index(*hrefs: str) -> str:
    links = "".join(f"<li><a href='{href}'>scanner</a></li>" for href in hrefs)
    return f"<html><body><h1>AirSane</h1><ul>{links}</ul></body></html>"


def _wait_for(client: APIClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        data = client.get(f"/api/scan_jobs/{job_id}/").data
        if data["status"] != "scanning":
            return data
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


def _scanner_id(url: str = DEVICE) -> str:
    return scanning._scanner(url).id


SCAN_REQUEST = {
    "source": "feeder",
    "color_mode": "grayscale",
    "resolution": 300,
    "paper_size": "a4",
}


@pytest.fixture
def scanner_settings(settings: Settings, paperless_dirs) -> Settings:
    settings.SCANNERS = [SERVER.rstrip("/")]
    return settings


@pytest.fixture
def airsane(httpx_mock: HTTPXMock) -> HTTPXMock:
    httpx_mock.add_response(
        url=SERVER,
        html=_index(f"/{DEVICE_UUID}"),
        is_optional=True,
    )
    httpx_mock.add_response(
        url=f"{DEVICE}ScannerCapabilities",
        content=CAPABILITIES,
        is_optional=True,
    )
    return httpx_mock


@pytest.mark.django_db
class TestScannerList:
    def test_lists_devices_from_airsane_index(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        response = admin_client.get("/api/scanners/")

        assert response.status_code == status.HTTP_200_OK
        [scanner] = response.data
        assert scanner["id"] == _scanner_id()
        assert scanner["name"] == "EPSON DS-571W"
        assert scanner["available"] is True
        assert [s["id"] for s in scanner["sources"]] == ["feeder", "feeder_duplex"]
        feeder = scanner["sources"][0]
        assert feeder["color_modes"] == ["color", "grayscale"]
        assert feeder["resolutions"] == [150, 300]
        assert feeder["pdf"] is True

    def test_newly_published_device_appears(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        httpx_mock: HTTPXMock,
    ) -> None:
        other_uuid = "0a1b2c3d-0000-4000-8000-123456789abc"
        httpx_mock.add_response(
            url=SERVER,
            html=_index(f"/{DEVICE_UUID}", f"/{other_uuid}", "/style.css"),
        )
        httpx_mock.add_response(
            url=f"{DEVICE}ScannerCapabilities",
            content=CAPABILITIES,
        )
        httpx_mock.add_response(
            url=f"{SERVER}{other_uuid}/ScannerCapabilities",
            content=FLATBED_CAPABILITIES,
        )

        response = admin_client.get("/api/scanners/")

        names = {s["name"]: s for s in response.data}
        assert set(names) == {"EPSON DS-571W", "EPSON GT-S660"}
        flatbed = names["EPSON GT-S660"]["sources"]
        assert [s["id"] for s in flatbed] == ["flatbed"]
        assert flatbed[0]["pdf"] is False

    def test_direct_device_url(
        self,
        admin_client: APIClient,
        settings: Settings,
        httpx_mock: HTTPXMock,
    ) -> None:
        settings.SCANNERS = ["http://printer.test/eSCL"]
        httpx_mock.add_response(url="http://printer.test/eSCL/", status_code=404)
        httpx_mock.add_response(
            url="http://printer.test/eSCL/ScannerCapabilities",
            content=FLATBED_CAPABILITIES,
        )

        [scanner] = admin_client.get("/api/scanners/").data

        assert scanner["id"] == _scanner_id("http://printer.test/eSCL/")
        assert scanner["name"] == "EPSON GT-S660"

    def test_unreachable_server(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        httpx_mock: HTTPXMock,
    ) -> None:
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("refused"), url=SERVER)

        [scanner] = admin_client.get("/api/scanners/").data

        assert scanner["available"] is False
        assert scanner["name"] == "airsane.test"
        assert scanner["error"] == "The scanner is not reachable."

    def test_requires_document_permission(
        self,
        user_client: APIClient,
        scanner_settings: Settings,
    ) -> None:
        assert user_client.get("/api/scanners/").status_code == 403

    def test_change_permission_is_enough(
        self,
        user_client: APIClient,
        regular_user: User,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        regular_user.user_permissions.add(
            Permission.objects.get(codename="change_document"),
        )

        assert user_client.get("/api/scanners/").status_code == 200


@pytest.mark.django_db
class TestScanJobs:
    def test_feeder_scan_merges_pages(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        airsane.add_response(url=f"{DEVICE}ScannerStatus", content=_status())
        airsane.add_response(
            url=f"{DEVICE}ScanJobs",
            method="POST",
            status_code=201,
            headers={"Location": "/61819d17-e017-5252-8bbc-2cc241adaf59/ScanJobs/1234"},
        )
        airsane.add_response(url=f"{JOB}/NextDocument", status_code=503)
        airsane.add_response(
            url=f"{JOB}/NextDocument",
            content=_pdf(),
            headers={"Content-Type": "application/pdf"},
        )
        airsane.add_response(
            url=f"{JOB}/NextDocument",
            content=_pdf(2),
            headers={"Content-Type": "application/pdf"},
        )
        airsane.add_response(url=f"{JOB}/NextDocument", status_code=404)

        response = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            SCAN_REQUEST,
            format="json",
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        job = _wait_for(admin_client, response.data["id"])
        assert job["status"] == "done"
        assert job["pages"] == 3
        settings_sent = airsane.get_request(url=f"{DEVICE}ScanJobs").content.decode()
        assert "<pwg:InputSource>Feeder</pwg:InputSource>" in settings_sent
        assert "<scan:Duplex>false</scan:Duplex>" in settings_sent
        assert "<scan:ColorMode>Grayscale8</scan:ColorMode>" in settings_sent
        assert "<pwg:Width>2480</pwg:Width>" in settings_sent
        assert (
            "<pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>" in settings_sent
        )

        file_response = admin_client.get(f"/api/scan_jobs/{job['id']}/file/")
        assert file_response.status_code == 200
        assert file_response["Content-Type"] == "application/pdf"
        pdf = pikepdf.Pdf.open(io.BytesIO(b"".join(file_response.streaming_content)))
        assert len(pdf.pages) == 3

    def test_duplex_and_paper_clamped_to_scanner(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        airsane.add_response(url=f"{DEVICE}ScannerStatus", content=_status())
        airsane.add_response(
            url=f"{DEVICE}ScanJobs",
            method="POST",
            status_code=201,
            headers={"Location": JOB},
        )
        airsane.add_response(
            url=f"{JOB}/NextDocument",
            content=_jpeg(),
            headers={"Content-Type": "image/jpeg"},
        )
        airsane.add_response(url=f"{JOB}/NextDocument", status_code=404)

        response = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            {
                **SCAN_REQUEST,
                "source": "feeder_duplex",
                "color_mode": "color",
                "paper_size": "legal",
            },
            format="json",
        )

        job = _wait_for(admin_client, response.data["id"])
        assert job["status"] == "done"
        assert job["pages"] == 1
        settings_sent = airsane.get_request(url=f"{DEVICE}ScanJobs").content.decode()
        assert "<scan:Duplex>true</scan:Duplex>" in settings_sent
        assert "<pwg:Height>4200</pwg:Height>" in settings_sent

    def test_unsupported_option_is_rejected(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        response = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            {**SCAN_REQUEST, "source": "flatbed"},
            format="json",
        )

        assert response.status_code == 400
        assert response.data["error"] == "The scanner does not offer this source."

    @pytest.mark.parametrize(
        ("source", "pages_kept"),
        [("feeder", 2), ("feeder_duplex", 4)],
    )
    def test_feeder_stops_after_sheet_limit(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
        source: str,
        pages_kept: int,
    ) -> None:
        airsane.add_response(url=f"{DEVICE}ScannerStatus", content=_status())
        airsane.add_response(
            url=f"{DEVICE}ScanJobs",
            method="POST",
            status_code=201,
            headers={"Location": JOB},
        )
        for _ in range(pages_kept):
            airsane.add_response(url=f"{JOB}/NextDocument", content=_pdf())
        airsane.add_response(url=JOB, method="DELETE")

        response = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            {
                **SCAN_REQUEST,
                "source": source,
                "color_mode": "color",
                "max_sheets": 2,
            },
            format="json",
        )

        job = _wait_for(admin_client, response.data["id"])
        assert job["status"] == "done"
        assert job["pages"] == pages_kept
        assert airsane.get_request(url=JOB, method="DELETE") is not None

    def test_sheet_limit_ignored_for_flatbed(self) -> None:
        options = scanning.ScanOptions("flatbed", "color", 300, "a4", max_sheets=1)
        assert options.page_limit() is None

    def test_feeder_sensor_is_not_trusted(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        airsane.add_response(
            url=f"{DEVICE}ScannerStatus",
            content=_status("ScannerAdfEmpty"),
        )
        airsane.add_response(
            url=f"{DEVICE}ScanJobs",
            method="POST",
            status_code=201,
            headers={"Location": JOB},
        )
        airsane.add_response(url=f"{JOB}/NextDocument", content=_pdf())
        airsane.add_response(url=f"{JOB}/NextDocument", status_code=404)

        response = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            SCAN_REQUEST,
            format="json",
        )

        assert _wait_for(admin_client, response.data["id"])["status"] == "done"

    def test_no_pages_returned(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        airsane.add_response(url=f"{DEVICE}ScannerStatus", content=_status())
        airsane.add_response(
            url=f"{DEVICE}ScanJobs",
            method="POST",
            status_code=201,
            headers={"Location": JOB},
        )
        airsane.add_response(url=f"{JOB}/NextDocument", status_code=404)

        response = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            SCAN_REQUEST,
            format="json",
        )

        job = _wait_for(admin_client, response.data["id"])
        assert job["status"] == "failed"
        assert job["error"] == (
            "The scanner returned no pages. Check that the pages are loaded."
        )

    def test_busy_scanner(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        airsane.add_response(
            url=f"{DEVICE}ScannerStatus",
            content=_status(state="Processing"),
        )

        response = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            SCAN_REQUEST,
            format="json",
        )

        assert response.status_code == 409

    def test_scan_in_progress_holds_scanner(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        lock = scanning.FileLock(
            scanning._jobs_dir() / f"scanner-{_scanner_id()}.lock",
            thread_local=False,
        )
        with lock:
            response = admin_client.post(
                f"/api/scanners/{_scanner_id()}/scan/",
                SCAN_REQUEST,
                format="json",
            )

        assert response.status_code == 409
        assert response.data["error"] == "Another scan is in progress on this scanner."

    def test_scanner_error_mid_scan(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        airsane.add_response(url=f"{DEVICE}ScannerStatus", content=_status())
        airsane.add_response(
            url=f"{DEVICE}ScanJobs",
            method="POST",
            status_code=201,
            headers={"Location": JOB},
        )
        airsane.add_response(url=f"{JOB}/NextDocument", status_code=500)
        airsane.add_response(url=JOB, method="DELETE")

        response = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            SCAN_REQUEST,
            format="json",
        )

        job = _wait_for(admin_client, response.data["id"])
        assert job["status"] == "failed"
        assert job["error"] == "The scanner stopped with an error (HTTP 500)."
        assert admin_client.get(f"/api/scan_jobs/{job['id']}/file/").status_code == 404

    def test_jobs_are_private_and_deletable(
        self,
        admin_client: APIClient,
        regular_user: User,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        user_client = APIClient()
        user_client.force_authenticate(user=regular_user)
        airsane.add_response(url=f"{DEVICE}ScannerStatus", content=_status())
        airsane.add_response(
            url=f"{DEVICE}ScanJobs",
            method="POST",
            status_code=201,
            headers={"Location": JOB},
        )
        airsane.add_response(url=f"{JOB}/NextDocument", content=_pdf())
        airsane.add_response(url=f"{JOB}/NextDocument", status_code=404)
        job_id = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            SCAN_REQUEST,
            format="json",
        ).data["id"]
        _wait_for(admin_client, job_id)

        assert user_client.get(f"/api/scan_jobs/{job_id}/").status_code == 404
        assert user_client.get(f"/api/scan_jobs/{job_id}/file/").status_code == 404
        assert user_client.delete(f"/api/scan_jobs/{job_id}/").status_code == 404

        assert admin_client.delete(f"/api/scan_jobs/{job_id}/").status_code == 204
        assert admin_client.get(f"/api/scan_jobs/{job_id}/").status_code == 404

    def test_continue_scan_appends_pages(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        airsane.add_response(
            url=f"{DEVICE}ScannerStatus",
            content=_status(),
            is_reusable=True,
        )
        airsane.add_response(
            url=f"{DEVICE}ScanJobs",
            method="POST",
            status_code=201,
            headers={"Location": JOB},
            is_reusable=True,
        )
        for pages in (1, 2):
            airsane.add_response(url=f"{JOB}/NextDocument", content=_pdf(pages))
            airsane.add_response(url=f"{JOB}/NextDocument", status_code=404)
        url = f"/api/scanners/{_scanner_id()}/scan/"
        first = _wait_for(
            admin_client,
            admin_client.post(url, SCAN_REQUEST, format="json").data["id"],
        )

        second = _wait_for(
            admin_client,
            admin_client.post(
                url,
                {**SCAN_REQUEST, "continue_job": first["id"]},
                format="json",
            ).data["id"],
        )

        assert second["status"] == "done"
        assert second["pages"] == 3
        assert admin_client.get(f"/api/scan_jobs/{first['id']}/").status_code == 404

    def test_continue_missing_scan(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        response = admin_client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            {**SCAN_REQUEST, "continue_job": "c" * 32},
            format="json",
        )

        assert response.status_code == 400
        assert response.data["error"] == "The earlier scan is no longer available."

    def _finished_scan(self, client: APIClient, airsane: HTTPXMock, pages: int) -> str:
        airsane.add_response(url=f"{DEVICE}ScannerStatus", content=_status())
        airsane.add_response(
            url=f"{DEVICE}ScanJobs",
            method="POST",
            status_code=201,
            headers={"Location": JOB},
        )
        for _ in range(pages):
            airsane.add_response(url=f"{JOB}/NextDocument", content=_pdf())
        airsane.add_response(url=f"{JOB}/NextDocument", status_code=404)
        job_id = client.post(
            f"/api/scanners/{_scanner_id()}/scan/",
            SCAN_REQUEST,
            format="json",
        ).data["id"]
        assert _wait_for(client, job_id)["status"] == "done"
        return job_id

    def test_edit_pages(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        job_id = self._finished_scan(admin_client, airsane, 3)
        original = pikepdf.Pdf.open(scanning._job_dir(job_id) / "scan.pdf")
        widths = [p.mediabox[2] for p in original.pages]
        original.close()

        response = admin_client.post(
            f"/api/scan_jobs/{job_id}/pages/",
            {"pages": [{"page": 3, "rotate": 90}, {"page": 1}]},
            format="json",
        )

        assert response.status_code == 200
        assert response.data["pages"] == 2
        assert response.data["revision"] == 1
        with pikepdf.Pdf.open(scanning._job_dir(job_id) / "scan.pdf") as edited:
            assert len(edited.pages) == 2
            assert edited.pages[0].obj.get("/Rotate", 0) == 90
            assert edited.pages[1].obj.get("/Rotate", 0) == 0
            assert edited.pages[0].mediabox[2] == widths[2]

    @pytest.mark.parametrize(
        ("pages", "error"),
        [
            ([{"page": 4}], "The page list does not match the scan."),
            ([{"page": 1}, {"page": 1}], "The page list does not match the scan."),
        ],
    )
    def test_edit_pages_rejects_bad_lists(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
        pages: list[dict[str, int]],
        error: str,
    ) -> None:
        job_id = self._finished_scan(admin_client, airsane, 3)

        response = admin_client.post(
            f"/api/scan_jobs/{job_id}/pages/",
            {"pages": pages},
            format="json",
        )

        assert response.status_code == 400
        assert response.data["error"] == error

    def test_edit_pages_validation_and_privacy(
        self,
        admin_client: APIClient,
        regular_user: User,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        job_id = self._finished_scan(admin_client, airsane, 1)
        url = f"/api/scan_jobs/{job_id}/pages/"

        assert admin_client.post(url, {"pages": []}, format="json").status_code == 400
        assert (
            admin_client.post(
                url,
                {"pages": [{"page": 1, "rotate": 45}]},
                format="json",
            ).status_code
            == 400
        )
        other = APIClient()
        other.force_authenticate(user=regular_user)
        assert (
            other.post(url, {"pages": [{"page": 1}]}, format="json").status_code == 404
        )

    def test_unknown_scanner(
        self,
        admin_client: APIClient,
        scanner_settings: Settings,
        airsane: HTTPXMock,
    ) -> None:
        response = admin_client.post(
            "/api/scanners/000000000000/scan/",
            SCAN_REQUEST,
            format="json",
        )

        assert response.status_code == 404


@pytest.mark.django_db
def test_expired_jobs_are_purged(scanner_settings: Settings) -> None:
    import os

    old = scanning._jobs_dir() / ("a" * 32)
    old.mkdir()
    (old / "state.json").write_text("{}")
    os.utime(old, (0, 0))
    fresh = scanning._jobs_dir() / ("b" * 32)
    fresh.mkdir()

    scanning.purge_expired_jobs()

    assert not old.exists()
    assert fresh.exists()
