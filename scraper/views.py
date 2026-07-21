"""Vistas de la API del scraper."""
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ScraperSource, Vehicle
from .serializers import (
    ScraperSourceSerializer,
    VehicleSerializer,
    VinLookupSerializer,
)
from .services import AllSourcesFailed, scrape_vehicle


class HealthView(APIView):
    """Endpoint simple para verificar que la API está viva."""

    def get(self, request):
        return Response({"status": "ok"})


class VehicleLookupView(APIView):
    """Recibe un VIN, hace scraping con fallback entre fuentes y devuelve el auto.

    POST /api/vehicles/lookup/
    Body: {"vin": "1HGCM82633A004352"}

    Prueba las fuentes activas por prioridad; si la principal falla, usa la
    siguiente automáticamente. Con `?refresh=true` fuerza un nuevo scraping
    aunque el vehículo ya exista en caché.
    """

    def post(self, request):
        entrada = VinLookupSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        vin = entrada.validated_data["vin"]

        refrescar = request.query_params.get("refresh", "").lower() == "true"

        vehiculo = Vehicle.objects.filter(vin=vin).first()
        if vehiculo and not refrescar:
            return Response(VehicleSerializer(vehiculo).data)

        try:
            scrapeado, fuente = scrape_vehicle(vin)
        except AllSourcesFailed as exc:
            return Response(
                {"detail": str(exc), "errores_por_fuente": exc.errores},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        defaults = scrapeado.as_model_kwargs()
        defaults["source"] = fuente
        vehiculo, creado = Vehicle.objects.update_or_create(vin=vin, defaults=defaults)
        codigo = status.HTTP_201_CREATED if creado else status.HTTP_200_OK
        return Response(VehicleSerializer(vehiculo).data, status=codigo)


class VehicleListView(ListAPIView):
    """Lista los vehículos ya scrapeados. GET /api/vehicles/"""

    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer


class VehicleDetailView(RetrieveAPIView):
    """Detalle de un vehículo por VIN. GET /api/vehicles/<vin>/"""

    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer
    lookup_field = "vin"


class SourceListView(ListAPIView):
    """Lista las fuentes de scraping configuradas. GET /api/sources/"""

    queryset = ScraperSource.objects.all()
    serializer_class = ScraperSourceSerializer
