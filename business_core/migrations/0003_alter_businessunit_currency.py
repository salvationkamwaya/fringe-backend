from django.db import migrations, models

def update_existing_currency(apps, schema_editor):
    BusinessUnit = apps.get_model('business_core', 'BusinessUnit')
    BusinessUnit.objects.all().update(currency='TSH')

class Migration(migrations.Migration):

    dependencies = [
        ('business_core', '0002_alter_auditlog_after_value_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='businessunit',
            name='currency',
            field=models.CharField(default='TSH', max_length=10),
        ),
        migrations.RunPython(update_existing_currency, reverse_code=migrations.RunPython.noop),
    ]

