from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields
from tortoise.migrations.constraints import UniqueConstraint

class Migration(migrations.Migration):
    dependencies = [('models', '0002_taskhistory_started_at')]

    initial = False

    operations = [
        ops.AddField(
            model_name='TaskHistory',
            name='idempotency_key',
            field=fields.CharField(null=True, max_length=255),
        ),
        ops.AddConstraint(
            model_name='TaskHistory',
            constraint=UniqueConstraint(fields=('user', 'idempotency_key'), name=None),
        ),
    ]
