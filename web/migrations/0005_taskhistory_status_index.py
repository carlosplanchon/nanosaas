from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0004_taskhistory_leases')]

    initial = False

    operations = [
        ops.AlterField(
            model_name='TaskHistory',
            name='status',
            field=fields.CharField(default='PENDING', db_index=True, max_length=50),
        ),
    ]
