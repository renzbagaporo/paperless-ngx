from typing import Any

from django.http import FileResponse
from django.http import Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from documents import scanning


def _require_scan_permission(request) -> None:
    user = request.user
    if not (
        user.has_perm("documents.add_document")
        or user.has_perm("documents.change_document")
    ):
        raise PermissionDenied


class StartScanSerializer(serializers.Serializer[dict[str, Any]]):
    source = serializers.ChoiceField(choices=list(scanning.SOURCES))
    color_mode = serializers.ChoiceField(choices=list(scanning.COLOR_MODES))
    resolution = serializers.IntegerField(
        min_value=1,
        max_value=scanning.MAX_RESOLUTION,
    )
    paper_size = serializers.ChoiceField(choices=list(scanning.PAPER_SIZES))
    max_sheets = serializers.IntegerField(
        min_value=1,
        max_value=500,
        required=False,
        allow_null=True,
    )
    continue_job = serializers.RegexField(
        scanning.JOB_ID_RE,
        required=False,
        allow_null=True,
    )


class PageOperationSerializer(serializers.Serializer[dict[str, Any]]):
    page = serializers.IntegerField(min_value=1)
    rotate = serializers.ChoiceField(choices=[0, 90, 180, 270], default=0)


class EditPagesSerializer(serializers.Serializer[dict[str, Any]]):
    pages = PageOperationSerializer(many=True, allow_empty=False)


@extend_schema(
    description="List the configured network scanners and their options.",
    responses={(200, "application/json"): OpenApiTypes.OBJECT},
)
class ScannerListView(GenericAPIView[Any]):
    permission_classes = (IsAuthenticated,)

    def get(self, request, format=None):
        _require_scan_permission(request)
        return Response(scanning.describe_scanners())


@extend_schema(
    description="Start a scan; poll the returned job until it finishes.",
    request=StartScanSerializer,
    responses={(202, "application/json"): OpenApiTypes.OBJECT},
)
class StartScanView(GenericAPIView[Any]):
    permission_classes = (IsAuthenticated,)
    serializer_class = StartScanSerializer

    def post(self, request, scanner_id: str, format=None):
        _require_scan_permission(request)
        scanner = scanning.get_scanner(scanner_id)
        if scanner is None:
            raise Http404
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        continue_job = data.pop("continue_job", None)
        options = scanning.ScanOptions(**data)
        try:
            job_id = scanning.start_scan(
                scanner,
                options,
                request.user.id,
                continue_job,
            )
        except scanning.ScannerBusy as e:
            return Response({"error": str(e)}, status=status.HTTP_409_CONFLICT)
        except scanning.ScanError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            scanning.read_job(job_id, request.user.id),
            status=status.HTTP_202_ACCEPTED,
        )


@extend_schema(
    description="Get a scan job's progress, or discard it.",
    responses={(200, "application/json"): OpenApiTypes.OBJECT},
)
class ScanJobView(GenericAPIView[Any]):
    permission_classes = (IsAuthenticated,)

    def get(self, request, job_id: str, format=None):
        state = scanning.read_job(job_id, request.user.id)
        if state is None:
            raise Http404
        return Response(state)

    def delete(self, request, job_id: str, format=None):
        if not scanning.delete_job(job_id, request.user.id):
            raise Http404
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    description="Download a finished scan as a PDF.",
    responses={(200, "application/pdf"): OpenApiTypes.BINARY},
)
class ScanJobFileView(GenericAPIView[Any]):
    permission_classes = (IsAuthenticated,)

    def get(self, request, job_id: str, format=None):
        path = scanning.job_file(job_id, request.user.id)
        if path is None or not path.exists():
            raise Http404
        response = FileResponse(
            path.open("rb"),
            content_type="application/pdf",
            as_attachment=False,
            filename=f"scan-{job_id[:8]}.pdf",
        )
        # The dialog shows every page from the same URL, which changes with
        # each edit (?revision=), so the browser can load the file once.
        response["Cache-Control"] = "private, max-age=3600"
        return response


@extend_schema(
    description=(
        "Reorder, rotate or remove pages of a finished scan. Pages are listed "
        "in their new order; pages left out are removed."
    ),
    request=EditPagesSerializer,
    responses={(200, "application/json"): OpenApiTypes.OBJECT},
)
class ScanJobPagesView(GenericAPIView[Any]):
    permission_classes = (IsAuthenticated,)
    serializer_class = EditPagesSerializer

    def post(self, request, job_id: str, format=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            state = scanning.edit_job_pages(
                job_id,
                request.user.id,
                serializer.validated_data["pages"],
            )
        except scanning.ScanError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if state is None:
            raise Http404
        return Response(state)
