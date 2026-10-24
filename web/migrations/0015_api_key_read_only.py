from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0014_api_keys')]

    initial = False

    operations = [
        ops.AddField(
            model_name='ApiKey',
            name='read_only',
            field=fields.BooleanField(default=False, db_default=False),
        ),
    ]
