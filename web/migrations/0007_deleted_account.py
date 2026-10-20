from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0006_account_session_version')]

    initial = False

    operations = [
        ops.CreateModel(
            name='DeletedAccount',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('google_id_hash', fields.CharField(unique=True, max_length=64)),
                ('deleted_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'deletedaccount', 'app': 'models', 'pk_attr': 'id', 'table_description': "What's kept of a deleted account: the hash of its Google ID."},
            bases=['Model'],
        ),
    ]
