"""Scraper API views."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ScrapeJob, ScraperSource, Vehicle
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


class HealthView(APIView):
    """Simple liveness endpoint."""

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

    @extend_schema(request=VinLookupSerializer, responses=VehicleSerializer)
    def post(self, request):
        payload = VinLookupSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        vin = payload.validated_data["vin"]
        webhook_url = payload.validated_data.get("webhook_url", "")

        try:
            vehicle, state = resolve_vin(vin, webhook_url=webhook_url)
        except VinDecodeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        data = VehicleSerializer(vehicle).data
        if state == STATUS_READY:
            return Response({"status": STATUS_READY, "vehicle": data})

        return Response(
            {
                "status": state,
                "vehicle": data,
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

    @extend_schema(request=VinBatchSerializer, responses=None)
    def post(self, request):
        payload = VinBatchSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        webhook_url = payload.validated_data.get("webhook_url", "")

        results = []
        for vin in payload.validated_data["vins"]:
            try:
                _, state = resolve_vin(vin, webhook_url=webhook_url)
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

    @extend_schema(request=ModelLookupSerializer, responses=VehicleModelSerializer)
    def post(self, request):
        payload = ModelLookupSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        make = payload.validated_data["make"]
        model = payload.validated_data["model"]
        year = payload.validated_data.get("year")
        webhook_url = payload.validated_data.get("webhook_url", "")

        vm, state = resolve_model(make, model, year, webhook_url=webhook_url)
        data = VehicleModelSerializer(vm).data if vm else None
        if state == STATUS_READY:
            return Response({"status": STATUS_READY, "model_data": data})
        return Response(
            {
                "status": state,
                "model_data": data,
                "detail": "Market data in progress. You will be notified via webhook when ready.",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class VehicleStatusView(APIView):
    """Resolution state of a VIN. GET /api/vehicles/<vin>/status/

    Returns whether the vehicle is resolved and the state of the latest scrape
    job for it (useful for the frontend while it waits for the webhook).
    """

    throttle_classes = []

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, vin):
        vin = vin.strip().upper()
        vehicle = Vehicle.objects.filter(vin=vin).first()
        job = ScrapeJob.objects.filter(vin=vin).order_by("-created_at").first()
        return Response({
            "vin": vin,
            "vehicle": VehicleSerializer(vehicle).data if vehicle else None,
            "job": ScrapeJobSerializer(job).data if job else None,
        })


class VehicleListView(ListAPIView):
    """List resolved vehicles. GET /api/vehicles/"""

    throttle_classes = []
    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer


class VehicleDetailView(RetrieveAPIView):
    """Vehicle detail by VIN. GET /api/vehicles/<vin>/"""

    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer
    lookup_field = "vin"

    def get_object(self):
        self.kwargs[self.lookup_field] = self.kwargs[self.lookup_field].strip().upper()
        return super().get_object()


class SourceListView(ListAPIView):
    """List configured scraper sources. GET /api/sources/"""

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

    throttle_classes = []

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        from .worker import controller
        return Response(controller.status())

    @extend_schema(request=None, responses=None)
    def post(self, request, action=None):
        from .worker import controller

        if action == "start":
            started = controller.start()
            detail = "Worker started." if started else "The worker was already running."
            return Response({"ok": True, "detail": detail, **controller.status()})

        if action == "stop":
            stopped = controller.stop()
            detail = "Worker stopped." if stopped else "The worker was not running."
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
