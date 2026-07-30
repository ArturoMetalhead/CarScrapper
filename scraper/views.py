"""Scraper API views."""
import logging

from django.contrib.auth import get_user

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView


class RecentPagination(PageNumberPagination):
    """Small page for the recent-vehicles list (panel polls it every 8s)."""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

from .models import ScrapeJob, ScrapeSubscriber, ScraperSource, Vehicle
from .serializers import (
    ModelLookupSerializer,
    ScraperSourceSerializer,
    ScrapeJobSerializer,
    VehicleModelSerializer,
    VehicleSerializer,
    VinBatchSerializer,
    VinLookupSerializer,
)
from .services import STATUS_READY, VinDecodeError, resolve_model, resolve_vin

logger = logging.getLogger("scraper.worker")


def _request_is_staff(request) -> bool:
    """True only for a logged-in staff session.

    The customer-facing lookup endpoints run with authentication_classes=[] (so no
    CSRF gate hits anonymous customers). DRF then sets request.user AND clobbers the
    underlying request.user to AnonymousUser, so we read the logged-in user straight
    from the session (django.contrib.auth.get_user) — the ops panel is
    staff_member_required and its fetch sends the session cookie. `force` is honored
    ONLY for such users, so an unauthenticated caller cannot use force to bypass
    caches (e.g. burn paid VinAudit quota by re-forcing the same VIN). For an
    anonymous request (no session) get_user returns AnonymousUser without a DB hit.
    """
    django_request = getattr(request, "_request", request)
    user = get_user(django_request)
    return bool(user is not None and user.is_authenticated and user.is_staff)


class HealthView(APIView):
    """Simple liveness endpoint."""

    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response({"status": "ok"})


class VehicleLookupView(APIView):
    """Fast VIN lookup (cache + background scraping).

    POST /api/vehicles/lookup/
    Body: {"vin": "1HGCM82633A004352", "webhook_url": "https://... (optional)"}

    - If the market data is cached and fresh -> 200 with the vehicle.
    - Otherwise -> decode the VIN (NHTSA), enqueue the per-model scraping and
      respond 202 "processing". When the worker finishes it notifies via webhook;
      meanwhile the frontend can poll GET /api/vehicles/<vin>/.
    """

    authentication_classes = []  # public: no session → no CSRF gate for customers
    permission_classes = [AllowAny]  # customer-facing search

    @extend_schema(request=VinLookupSerializer, responses=VehicleSerializer)
    def post(self, request):
        payload = VinLookupSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        vin = payload.validated_data["vin"]
        webhook_url = payload.validated_data.get("webhook_url", "")
        # force triggers a paid VinAudit re-query bypassing the cache — staff only.
        force = payload.validated_data.get("force", False) and _request_is_staff(request)

        try:
            vehicle, state, job = resolve_vin(vin, webhook_url=webhook_url, force=force)
        except VinDecodeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        data = VehicleSerializer(vehicle).data
        if state == STATUS_READY:
            return Response({"status": STATUS_READY, "vehicle": data})

        return Response(
            {
                "status": state,
                "vehicle": data,
                "job_id": job.id if job else None,
                "detail": (
                    "Market data in progress. You will be notified via webhook when "
                    "ready; you can also look up the VIN later."
                ),
                "poll_url": request.build_absolute_uri(f"/api/vehicles/{vin}/"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class VehiclePrewarmView(APIView):
    """Prewarm (proactive scraping) of a list of VINs.

    POST /api/vehicles/prewarm/
    Body: {"vins": ["...", "..."], "webhook_url": "https://... (optional)"}

    Decodes each VIN and enqueues its model. Returns the per-VIN state (ready if
    already cached, processing if enqueued, or a decode error).
    """

    permission_classes = [IsAdminUser]  # bulk/ops endpoint

    @extend_schema(request=VinBatchSerializer, responses=None)
    def post(self, request):
        payload = VinBatchSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        webhook_url = payload.validated_data.get("webhook_url", "")

        results = []
        for vin in payload.validated_data["vins"]:
            try:
                # Prewarm is PROACTIVE (not a user request), so it must not spend
                # VinAudit quota: only warm the model cache (Edmunds/CarGurus).
                _, state, _ = resolve_vin(vin, webhook_url=webhook_url, allow_vinaudit=False)
                results.append({"vin": vin, "status": state})
            except VinDecodeError as exc:
                results.append({"vin": vin, "status": "error", "detail": str(exc)})

        return Response({"results": results}, status=status.HTTP_202_ACCEPTED)


class ModelLookupView(APIView):
    """Fast lookup by MODEL (no VIN needed) — handy for new cars.

    POST /api/models/lookup/
    Body: {"make": "Mazda", "model": "CX-5", "year": 2026, "webhook_url": "..."}

    - Cached and fresh -> 200 with the model data (suggested price + range).
    - Otherwise -> enqueue the scraping and respond 202 "processing"; the worker
      notifies via webhook when done.
    """

    authentication_classes = []  # public: no session → no CSRF gate for customers
    permission_classes = [AllowAny]  # customer-facing search

    @extend_schema(request=ModelLookupSerializer, responses=VehicleModelSerializer)
    def post(self, request):
        payload = ModelLookupSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        make = payload.validated_data["make"]
        model = payload.validated_data["model"]
        year = payload.validated_data.get("year")
        webhook_url = payload.validated_data.get("webhook_url", "")
        # force re-scrapes bypassing the cache — staff only (see _request_is_staff).
        force = payload.validated_data.get("force", False) and _request_is_staff(request)

        vm, state, job = resolve_model(make, model, year, webhook_url=webhook_url, force=force)
        data = VehicleModelSerializer(vm).data if vm else None
        if state == STATUS_READY:
            return Response({"status": STATUS_READY, "model_data": data})
        return Response(
            {
                "status": state,
                "model_data": data,
                "job_id": job.id if job else None,
                "detail": "Market data in progress. You will be notified via webhook when ready.",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class VehicleStatusView(APIView):
    """Resolution state of a VIN. GET /api/vehicles/<vin>/status/

    Returns whether the vehicle is resolved and the state of the latest scrape
    job for it (useful for the frontend while it waits for the webhook).
    """

    permission_classes = [AllowAny]  # frontend polls this while waiting
    throttle_classes = []

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, vin):
        vin = vin.strip().upper()
        vehicle = Vehicle.objects.filter(vin=vin).first()
        job = ScrapeJob.objects.filter(vin=vin).order_by("-created_at").first()
        if job is None:
            # Deduped requests share ONE job; a second requester's VIN lives only in
            # ScrapeSubscriber, so fall back to it to still report the job status
            # (keeps the frontend's fast-fail if the shared job fails).
            sub = (
                ScrapeSubscriber.objects.filter(vin=vin)
                .select_related("job").order_by("-created_at").first()
            )
            job = sub.job if sub else None
        return Response({
            "vin": vin,
            "vehicle": VehicleSerializer(vehicle).data if vehicle else None,
            "job": ScrapeJobSerializer(job).data if job else None,
        })


class JobStatusView(APIView):
    """State of a scrape job by id. GET /api/jobs/<id>/

    Used by the admin "re-scrape" button to wait for a FORCED scrape to finish
    (so it shows the freshly scraped result instead of the previously cached one).
    """

    permission_classes = [AllowAny]  # frontend polls this while waiting
    throttle_classes = []

    @extend_schema(responses=ScrapeJobSerializer)
    def get(self, request, job_id):
        job = ScrapeJob.objects.filter(pk=job_id).first()
        if job is None:
            return Response({"detail": "Job not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(ScrapeJobSerializer(job).data)


class VehicleListView(ListAPIView):
    """List resolved vehicles, most-recent first. GET /api/vehicles/?page_size=10"""

    permission_classes = [IsAdminUser]  # enumerates all VINs/prices — ops only
    throttle_classes = []
    queryset = Vehicle.objects.order_by("-updated_at")
    serializer_class = VehicleSerializer
    pagination_class = RecentPagination


class VehicleDetailView(RetrieveAPIView):
    """Vehicle detail by VIN. GET /api/vehicles/<vin>/"""

    permission_classes = [IsAdminUser]  # exposes raw_data — ops only
    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer
    lookup_field = "vin"

    def get_object(self):
        self.kwargs[self.lookup_field] = self.kwargs[self.lookup_field].strip().upper()
        return super().get_object()


class SourceListView(ListAPIView):
    """List configured scraper sources. GET /api/sources/"""

    permission_classes = [IsAdminUser]  # exposes selectors/config — ops only
    queryset = ScraperSource.objects.all()
    serializer_class = ScraperSourceSerializer


class WorkerControlView(APIView):
    """Control the background scraping worker.

    GET  /api/worker/         -> state (running + queue summary)
    POST /api/worker/start/   -> start the worker
    POST /api/worker/stop/    -> stop it (finishes the in-flight job)

    The worker autostarts with the API (SCRAPER_WORKER_AUTOSTART); these
    endpoints let you stop and start it again at runtime.
    """

    permission_classes = [IsAdminUser]  # control plane — ops only
    throttle_classes = []

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        from .worker import controller
        return Response(controller.status())

    @extend_schema(request=None, responses=None)
    def post(self, request, action=None):
        from .worker import controller

        if action == "start":
            started = controller.start(log=logger.info)
            detail = "Worker started." if started else "The worker was already running."
            return Response({"ok": True, "detail": detail, **controller.status()})

        if action == "stop":
            # Short join so the HTTP request returns fast; the stop flag is set
            # either way and the worker exits after the in-flight scrape.
            stopped = controller.stop(timeout=3)
            if stopped:
                detail = "Worker stopped."
            elif controller.is_running():
                detail = "Stop requested — the worker will stop after the current scrape finishes."
            else:
                detail = "The worker was not running."
            return Response({"ok": True, "detail": detail, **controller.status()})

        return Response(
            {"detail": "Invalid action. Use /api/worker/start/ or /api/worker/stop/."},
            status=status.HTTP_400_BAD_REQUEST,
        )


class CrawlerControlView(APIView):
    """Control the background crawler (proactive discovery + refresh).

    GET  /api/crawler/         -> state (running, frontier size, pending by origin)
    POST /api/crawler/start/   -> start the crawl planner
    POST /api/crawler/stop/    -> stop it
    """

    permission_classes = [IsAdminUser]  # control plane — ops only
    throttle_classes = []

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        from .crawler import planner
        return Response(planner.status())

    @extend_schema(request=None, responses=None)
    def post(self, request, action=None):
        from .crawler import planner

        if action == "start":
            started = planner.start()
            detail = "Crawler started." if started else "The crawler was already running."
            return Response({"ok": True, "detail": detail, **planner.status()})

        if action == "stop":
            stopped = planner.stop()
            detail = "Crawler stopped." if stopped else "The crawler was not running."
            return Response({"ok": True, "detail": detail, **planner.status()})

        return Response(
            {"detail": "Invalid action. Use /api/crawler/start/ or /api/crawler/stop/."},
            status=status.HTTP_400_BAD_REQUEST,
        )
