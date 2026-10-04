"""URLs of the web admin."""

from django.contrib.auth import views as auth_views
from django.urls import path

from .views import auth, dashboard, guilds, library, settings

app_name = 'webadmin'

urlpatterns = [
    path('', dashboard.DashboardView.as_view(), name='dashboard'),
    path('login/', auth.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('discord/connect/', auth.DiscordConnectView.as_view(),
         name='discord_connect'),
    path('discord/callback/', auth.DiscordCallbackView.as_view(),
         name='discord_callback'),
    path('g/<int:discord_guild_id>/', guilds.GuildView.as_view(), name='guild'),
    path('g/<int:discord_guild_id>/add/', guilds.AddGuildView.as_view(),
         name='guild_add'),
    # The library of a server is filled by its hosts.
    path('g/<int:discord_guild_id>/library/', library.LibraryView.as_view(),
         name='library'),
    path('g/<int:discord_guild_id>/library/questions/',
         library.AddQuestionView.as_view(), name='question_add'),
    path('g/<int:discord_guild_id>/library/questions/<int:question_pk>/',
         library.EditQuestionView.as_view(), name='question_edit'),
    path('g/<int:discord_guild_id>/library/questions/<int:question_pk>/drop/',
         library.RemoveQuestionView.as_view(), name='question_drop'),
    path('g/<int:discord_guild_id>/library/answers/<int:answer_pk>/drop/',
         library.RemoveAnswerView.as_view(), name='answer_drop'),
    # The settings of a server are managed by its administrators.
    path('g/<int:discord_guild_id>/settings/', settings.SettingsView.as_view(),
         name='settings'),
    path('g/<int:discord_guild_id>/settings/channel/',
         settings.ChannelView.as_view(), name='setting_channel'),
    path('g/<int:discord_guild_id>/settings/ping/',
         settings.PingRoleView.as_view(), name='setting_ping'),
    path('g/<int:discord_guild_id>/settings/hosts/',
         settings.HostsView.as_view(), name='setting_hosts'),
]
