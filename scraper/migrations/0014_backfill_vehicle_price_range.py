"""Backfill Vehicle.price_low/price_high/price_kind from the linked VehicleModel.

0013 added these columns to Vehicle as NULL, and the API serializer now reads the
price range from the Vehicle's OWN fields (not the linked VehicleModel). So Vehicle
rows resolved BEFORE 0013 would report a null range until re-resolved. This copies
the range from each linked VehicleModel onto those rows, EXCEPT VinAudit-priced VINs
(whose per-VIN valuation is authoritative and set separately).
"""
from django.db import migrations


def backfill_price_range(apps, schema_editor):
    Vehicle = apps.get_model("scraper", "Vehicle")
    qs = (
        Vehicle.objects.filter(
            vehicle_model__isnull=False, vinaudit_priced_at__isnull=True
        )
        .select_related("vehicle_model")
    )
    batch, buf = [], 500
    for v in qs.iterator():
        vm = v.vehicle_model
        v.price_low = vm.price_low
        v.price_high = vm.price_high
        v.price_kind = vm.price_kind
        batch.append(v)
        if len(batch) >= buf:
            Vehicle.objects.bulk_update(batch, ["price_low", "price_high", "price_kind"])
            batch = []
    if batch:
        Vehicle.objects.bulk_update(batch, ["price_low", "price_high", "price_kind"])


class Migration(migrations.Migration):
    dependencies = [
        ("scraper", "0013_vehicle_price_high_vehicle_price_kind_and_more"),
    ]
    operations = [
        migrations.RunPython(backfill_price_range, migrations.RunPython.noop),
    ]
