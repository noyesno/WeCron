# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-01-11 10:03
from __future__ import unicode_literals

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('remind', '0008_remind_create_time'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='remind',
            name='subscribers',
        ),
        migrations.AddField(
            model_name='remind',
            name='participants',
            field=models.ManyToManyField(related_name='time_reminds_participate', to=settings.AUTH_USER_MODEL, verbose_name='\u8ba2\u9605\u8005'),
        ),
    ]
