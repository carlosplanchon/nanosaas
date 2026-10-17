from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0005_taskhistory_status_index')]

    initial = False

    operations = [
        ops.AddField(
            model_name='Account',
            name='session_version',
            field=fields.IntField(default=0, db_default=0),
        ),
    ]
