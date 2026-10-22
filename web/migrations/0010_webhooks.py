from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from tortoise import fields
from tortoise.indexes import Index

class Migration(migrations.Migration):
    dependencies = [('models', '0009_taskhistory_cost')]

    initial = False

    operations = [
        ops.CreateModel(
            name='Webhook',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('account', fields.OneToOneField('models.Account', source_field='account_id', db_constraint=True, to_field='id', related_name='webhook', on_delete=OnDelete.CASCADE)),
                ('url', fields.CharField(max_length=2000)),
                ('secret', fields.CharField(max_length=100)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'webhook', 'app': 'models', 'pk_attr': 'id', 'table_description': 'The URL where an account gets an event when each of its tasks ends.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='WebhookDelivery',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('webhook', fields.ForeignKeyField('models.Webhook', source_field='webhook_id', db_constraint=True, to_field='id', related_name='deliveries', on_delete=OnDelete.CASCADE)),
                ('message_id', fields.CharField(unique=True, max_length=50)),
                ('type', fields.CharField(max_length=50)),
                ('task_id', fields.CharField(null=True, max_length=255)),
                ('payload', fields.TextField(unique=False)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('status', fields.CharField(default='PENDING', max_length=20)),
                ('attempts', fields.IntField(default=0)),
                ('next_attempt_at', fields.DatetimeField(auto_now=False, auto_now_add=False)),
                ('last_attempt_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
                ('last_response', fields.CharField(null=True, max_length=200)),
            ],
            options={'table': 'webhookdelivery', 'app': 'models', 'indexes': [Index(fields=['status', 'next_attempt_at']), Index(fields=['webhook_id', 'created_at'])], 'pk_attr': 'id', 'table_description': "An event for a webhook, which the worker sends until it's delivered."},
            bases=['Model'],
        ),
    ]
