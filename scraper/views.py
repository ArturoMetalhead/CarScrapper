"""Vistas de la API del scraper."""
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ScrapeJob, ScraperSource, Vehicle
from .serializers import (
    ScraperSourceSerializer,
    ScrapeJobSerializer,
    VehicleSerializer,
    VinBatchSerializer,
    VinLookupSerializer,
)
from .services import STATUS_READY, VinDecodeError, resolve_vin


class HealthView(APIView):
    """Endpoint simple para verificar que la API está viva."""

    def get(self, request):
        return Response({"status": "ok"})


class VehicleLookupView(APIView):
    """Busca un vehículo por VIN de forma rápida (caché + scraping en fondo).

    POST /api/vehicles/lookup/
    Body: {"vin": "1HGCM82633A004352", "webhook_url": "https://... (opcional)"}

    - Si el dato de mercado está en caché y fresco -> 200 con el vehículo.
    - Si no -> decodifica el VIN (NHTSA), encola el scraping por modelo y responde
      202 "processing". Cuando el worker termine, avisa por webhook; mientras
      tanto el frontend puede consultar GET /api/vehicles/<vin>/.
    """

    def post(self, request):
        entrada = VinLookupSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        vin = entrada.validated_data["vin"]
        webhook_url = entrada.validated_data.get("webhook_url", "")

        try:
            vehiculo, estado = resolve_vin(vin, webhook_url=webhook_url)
        except VinDecodeError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )

        datos = VehicleSerializer(vehiculo).data
        if estado == STATUS_READY:
            return Response({"status": STATUS_READY, "vehicle": datos})

        return Response(
            {
                "status": estado,
                "vehicle": datos,
                "detail": (
                    "Datos de mercado en proceso. Se notificará por webhook al "
                    "terminar; también puedes consultar el VIN más tarde."
                ),
                "poll_url": request.build_absolute_uri(f"/api/vehicles/{vin}/"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class VehiclePrewarmView(APIView):
    """Precarga (pre-scraping proactivo) de una lista de VINs.

    POST /api/vehicles/prewarm/
    Body: {"vins": ["...", "..."], "webhook_url": "https://... (opcional)"}

    Decodifica cada VIN y encola su modelo. Devuelve el estado por VIN (ready si
    ya estaba en caché, processing si se encoló, o error de decodificación).
    """

    def post(self, request):
        entrada = VinBatchSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        webhook_url = entrada.validated_data.get("webhook_url", "")

        resultados = []
        for vin in entrada.validated_data["vins"]:
            try:
                _, estado = resolve_vin(vin, webhook_url=webhook_url)
                resultados.append({"vin": vin, "status": estado})
            except VinDecodeError as exc:
                resultados.append({"vin": vin, "status": "error", "detail": str(exc)})

        return Response({"results": resultados}, status=status.HTTP_202_ACCEPTED)


class VehicleStatusView(APIView):
    """Estado de resolución de un VIN. GET /api/vehicles/<vin>/status/

    Devuelve si el vehículo ya está resuelto y el estado del último trabajo de
    scraping asociado (útil para el frontend mientras espera el webhook).
    """

    def get(self, request, vin):
        vin = vin.strip().upper()
        vehiculo = Vehicle.objects.filter(vin=vin).first()
        job = (
            ScrapeJob.objects.filter(vin=vin).order_by("-created_at").first()
        )
        return Response({
            "vin": vin,
            "vehicle": VehicleSerializer(vehiculo).data if vehiculo else None,
            "job": ScrapeJobSerializer(job).data if job else None,
        })


class VehicleListView(ListAPIView):
    """Lista los vehículos ya resueltos. GET /api/vehicles/"""

    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer


class VehicleDetailView(RetrieveAPIView):
    """Detalle de un vehículo por VIN. GET /api/vehicles/<vin>/"""

    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer
    lookup_field = "vin"

    def get_object(self):
        self.kwargs[self.lookup_field] = self.kwargs[self.lookup_field].strip().upper()
        return super().get_object()


class SourceListView(ListAPIView):
    """Lista las fuentes de scraping configuradas. GET /api/sources/"""

    queryset = ScraperSource.objects.all()
    serializer_class = ScraperSourceSerializer


class WorkerControlView(APIView):
    """Control del worker de scraping en segundo plano.

    GET  /api/worker/         -> estado (corriendo + resumen de la cola)
    POST /api/worker/start/   -> arranca el worker
    POST /api/worker/stop/    -> lo detiene (termina el trabajo en curso)

    El worker arranca solo con la API (SCRAPER_WORKER_AUTOSTART); estos endpoints
    permiten pararlo y volver a arrancarlo en caliente.
    """

    def get(self, request):
        from .worker import controller
        return Response(controller.status())

    def post(self, request, accion=None):
        from .worker import controller

        if accion == "start":
            arrancado = controller.start()
            detalle = "Worker arrancado." if arrancado else "El worker ya estaba corriendo."
            return Response({"ok": True, "detail": detalle, **controller.status()})

        if accion == "stop":
            detenido = controller.stop()
            detalle = "Worker detenido." if detenido else "El worker no estaba corriendo."
            return Response({"ok": True, "detail": detalle, **controller.status()})

        return Response(
            {"detail": "Acción no válida. Usa /api/worker/start/ o /api/worker/stop/."},
            status=status.HTTP_400_BAD_REQUEST,
        )
