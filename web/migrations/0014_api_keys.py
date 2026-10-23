from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0013_webhook_throttling')]

    initial = False

    operations = [
        ops.CreateModel(
            name='ApiKey',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('account', fields.ForeignKeyField('models.Account', source_field='account_id', db_constraint=True, to_field='id', related_name='api_keys', on_delete=OnDelete.CASCADE)),
                ('name', fields.CharField(max_length=50)),
                ('key_hash', fields.CharField(unique=True, max_length=255)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('last_used_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
            ],
            options={'table': 'apikey', 'app': 'models', 'pk_attr': 'id', 'table_description': 'A key of an account to the API.'},
            bases=['Model'],
        ),
        # Each account's key becomes its first one, named Default, and keeps working.
        # The ones stored before NanoSaaS hashed them, without a "$", are hashed on
        # the way, as ApiKey.hash_key() does.
        ops.RunSQL(
            "INSERT INTO apikey (account_id, name, key_hash, created_at) "
            "SELECT id, 'Default', CASE WHEN position('$' IN api_key) = 0 "
            "THEN left(api_key, 8) || '$' || encode(sha256(convert_to(api_key, 'UTF8')), 'hex') "
            "ELSE api_key END, now() FROM account WHERE api_key IS NOT NULL"
        ),
        ops.RemoveField(model_name='Account', name='api_key_hash'),
    ]
