from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.indexes import Index

class Migration(migrations.Migration):
    dependencies = [('models', '0007_deleted_account')]

    initial = False

    operations = [
        ops.AddIndex(
            model_name='TaskHistory',
            index=Index(fields=['user_id', 'created_at', 'id']),
        ),
    ]
