# Generated by Django 4.0.4 on 2022-04-13 02:07

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bom', '0005_rename_notifying_next_user_partclassworkflowcompletedtransition_notifying_next_users'),
    ]

    operations = [
        migrations.AlterField(
            model_name='partclassworkflowcompletedtransition',
            name='notifying_next_users',
            field=models.BooleanField(default=True, verbose_name='Notifying next users'),
        ),
    ]
