from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0003_taskhistory_idempotency_key')]

    initial = False

    operations = [
        ops.AddField(
            model_name='TaskHistory',
            name='attempts',
            field=fields.IntField(default=0, db_default=0),
        ),
        ops.AddField(
            model_name='TaskHistory',
            name='lease_until',
            field=fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
        ),
    ]
