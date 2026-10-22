from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from tortoise import fields
from tortoise.indexes import Index

class Migration(migrations.Migration):
    dependencies = [('models', '0010_webhooks')]

    initial = False

    operations = [
        ops.CreateModel(
            name='CreditMovement',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('account', fields.ForeignKeyField('models.Account', source_field='account_id', db_constraint=True, to_field='id', related_name='credit_movements', on_delete=OnDelete.CASCADE)),
                ('amount', fields.IntField()),
                ('balance', fields.IntField()),
                ('reason', fields.CharField(max_length=20)),
                ('task_id', fields.CharField(null=True, max_length=255)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'creditmovement', 'app': 'models', 'indexes': [Index(fields=['account_id', 'created_at', 'id'])], 'pk_attr': 'id', 'table_description': "A change of an account's credits, with the balance it left."},
            bases=['Model'],
        ),
        # The history of the existing accounts starts with their balance.
        ops.RunSQL(
            "INSERT INTO creditmovement (account_id, amount, balance, reason, created_at) "
            "SELECT id, credits, credits, 'opening', now() FROM account WHERE credits <> 0"
        ),
    ]
