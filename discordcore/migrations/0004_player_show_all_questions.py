from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('discordcore', '0003_guild_default_ping_role_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='show_all_questions',
            field=models.BooleanField(default=False, help_text='See every question of a server in the host panels, not only the ones authored here.', verbose_name='show all questions'),
        ),
    ]
