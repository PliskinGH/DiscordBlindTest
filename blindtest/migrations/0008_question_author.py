from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('blindtest', '0007_alter_guess_unique_guess_per_round'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='question',
            name='author',
            field=models.ForeignKey(blank=True, help_text='Only the author sees this question in the host panels, unless they asked to see every question of the server.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='authored_questions', to=settings.AUTH_USER_MODEL, verbose_name='author'),
        ),
    ]
