from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0011_credit_movements')]

    initial = False

    operations = [
        ops.AddField(
            model_name='Webhook',
            name='disabled_at',
            field=fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
        ),
        ops.AddField(
            model_name='Webhook',
            name='disabled_reason',
            field=fields.CharField(null=True, max_length=300),
        ),
        ops.AddField(
            model_name='Webhook',
            name='failing_since',
            field=fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
        ),
    ]
