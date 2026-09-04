from django.db import migrations, models


def migrate_driver_ids(apps, schema_editor):
    ScannerEndpoint = apps.get_model("manager", "ScannerEndpoint")
    ScannerEndpoint.objects.filter(protocol="escl").update(protocol="airscan-escl")
    ScannerEndpoint.objects.filter(protocol="wsd").update(protocol="airscan-wsd")


def restore_legacy_ids(apps, schema_editor):
    ScannerEndpoint = apps.get_model("manager", "ScannerEndpoint")
    ScannerEndpoint.objects.filter(protocol="airscan-escl").update(protocol="escl")
    ScannerEndpoint.objects.filter(protocol="airscan-wsd").update(protocol="wsd")


class Migration(migrations.Migration):
    dependencies = [("manager", "0002_alter_printerendpoint_uri")]

    operations = [
        migrations.AlterField(
            model_name="scannerendpoint",
            name="uri",
            field=models.CharField(max_length=500, unique=True),
        ),
        migrations.AlterField(
            model_name="scannerendpoint",
            name="protocol",
            field=models.CharField(
                choices=[
                    ("airscan-escl", "AirScan / eSCL"),
                    ("airscan-wsd", "AirScan / WSD"),
                    ("hpaio", "HP HPLIP / HPAIO"),
                ],
                default="airscan-escl",
                max_length=24,
            ),
        ),
        migrations.RunPython(migrate_driver_ids, restore_legacy_ids),
        migrations.AddField(
            model_name="scannerendpoint",
            name="validation_state",
            field=models.CharField(
                choices=[("unknown", "Unknown"), ("pending", "Pending"), ("ready", "Ready"), ("failed", "Failed")],
                default="unknown",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="scannerendpoint",
            name="validation_message",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="scannerendpoint",
            name="validated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
