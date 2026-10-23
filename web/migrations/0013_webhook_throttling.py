from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0012_webhook_disabling')]

    initial = False

    operations = [
        ops.AddField(
            model_name='Webhook',
            name='throttled_until',
            field=fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
        ),
    ]
