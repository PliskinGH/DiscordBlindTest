"""Rename the broadcast kind of a published game from ANNOUNCE to PUBLISH."""


from django.db import migrations, models


def published_games(apps, schema_editor):
    """Move the rows of a publication to the kind that now names it."""
    Broadcast = apps.get_model('blindtest', 'Broadcast')
    Broadcast.objects.filter(kind='ANNOUNCE').update(kind='PUBLISH')


class Migration(migrations.Migration):

    dependencies = [
        ('blindtest', '0005_broadcast_attempts_broadcast_next_attempt_at_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='broadcast',
            name='kind',
            field=models.CharField(choices=[('PUBLISH', 'game published'), ('ROUND', 'round opened'), ('REVEAL', 'round answer and scores'), ('RECAP', 'final scores')], max_length=20, verbose_name='kind'),
        ),
        migrations.RunPython(published_games, migrations.RunPython.noop),
    ]
