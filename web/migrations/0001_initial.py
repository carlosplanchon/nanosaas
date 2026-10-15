from tortoise import migrations
from tortoise.migrations import operations as ops
import functools
from json import dumps, loads
from tortoise.fields.base import OnDelete
from tortoise import fields

class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name='Account',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('google_id', fields.CharField(unique=True, max_length=255)),
                ('email', fields.CharField(unique=True, max_length=255)),
                ('first_name', fields.CharField(null=True, max_length=50)),
                ('last_name', fields.CharField(null=True, max_length=50)),
                ('display_name', fields.CharField(null=True, max_length=100)),
                ('picture', fields.CharField(max_length=255)),
                ('provider', fields.CharField(max_length=50)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('credits', fields.IntField(default=25)),
                ('api_key_hash', fields.CharField(source_field='api_key', null=True, unique=True, max_length=255)),
            ],
            options={'table': 'account', 'app': 'models', 'pk_attr': 'id'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='TaskHistory',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('user', fields.ForeignKeyField('models.Account', source_field='user_id', db_constraint=True, to_field='id', related_name='task_history', on_delete=OnDelete.CASCADE)),
                ('task_id', fields.CharField(unique=True, max_length=255)),
                ('task_type', fields.CharField(max_length=50)),
                ('parameters', fields.JSONField(encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads)),
                ('status', fields.CharField(default='PENDING', max_length=50)),
                ('result', fields.JSONField(null=True, encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'taskhistory', 'app': 'models', 'pk_attr': 'id'},
            bases=['Model'],
        ),
    ]
