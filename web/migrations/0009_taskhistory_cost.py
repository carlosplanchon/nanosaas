from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0008_taskhistory_history_index')]

    initial = False

    operations = [
        ops.AddField(
            model_name='TaskHistory',
            name='cost',
            field=fields.IntField(default=1, db_default=1),
        ),
    ]
