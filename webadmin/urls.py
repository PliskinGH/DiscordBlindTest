"""URLs of the web admin."""

from django.contrib.auth import views as auth_views
from django.urls import path

from .views import auth, dashboard, games, guilds, library, settings

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
    path('g/<int:discord_guild_id>/posts/<int:broadcast_pk>/retry/',
         guilds.RetryBroadcastView.as_view(), name='broadcast_retry'),
    # The library of a server is filled by its hosts.
    path('g/<int:discord_guild_id>/library/', library.LibraryView.as_view(),
         name='library'),
    path('g/<int:discord_guild_id>/library/questions/',
         library.AddQuestionView.as_view(), name='question_add'),
    path('g/<int:discord_guild_id>/library/questions/<int:question_pk>/',
         library.EditQuestionView.as_view(), name='question_edit'),
    path('g/<int:discord_guild_id>/library/questions/<int:question_pk>/remove/',
         library.RemoveQuestionView.as_view(), name='question_remove'),
    path('g/<int:discord_guild_id>/library/answers/<int:answer_pk>/remove/',
         library.RemoveAnswerView.as_view(), name='answer_remove'),
    # The control room: a host runs a game from the browser, and every member
    # of the server may guess the round in play.
    path('g/<int:discord_guild_id>/games/', games.GamesView.as_view(),
         name='games'),
    path('g/<int:discord_guild_id>/games/setup/', games.SetupGameView.as_view(),
         name='game_setup'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/',
         games.GameView.as_view(), name='game'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/state/',
         games.GameStateView.as_view(), name='game_state'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/live/',
         games.GameLiveView.as_view(), name='game_live'),
    # The pickers ask the server for their options rather than carrying them.
    path('g/<int:discord_guild_id>/games/<int:game_pk>/search/',
         games.SearchView.as_view(), name='game_search'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/publish/',
         games.PublishGameView.as_view(), name='game_publish'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/queue/',
         games.QueueView.as_view(), name='game_queue'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/unqueue/',
         games.UnqueueView.as_view(), name='game_unqueue'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/copy/',
         games.CopyQuestionsView.as_view(), name='game_copy'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/next/',
         games.NextRoundView.as_view(), name='game_next'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/reveal/',
         games.RevealRoundView.as_view(), name='game_reveal'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/end/',
         games.EndGameView.as_view(), name='game_end'),
    # The teams of a game, which its hosts fill before and during the game.
    path('g/<int:discord_guild_id>/games/<int:game_pk>/teams/',
         games.TeamView.as_view(), name='team_add'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/teams/copy/',
         games.CopyTeamView.as_view(), name='team_copy'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/teams/<int:team_pk>/',
         games.TeamMemberView.as_view(), name='team_member'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/teams/<int:team_pk>/name/',
         games.RenameTeamView.as_view(), name='team_rename'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/teams/<int:team_pk>/remove/',
         games.RemoveTeamView.as_view(), name='team_remove'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/guess/',
         games.GuessView.as_view(), name='game_guess'),
    path('g/<int:discord_guild_id>/games/<int:game_pk>/guesses/<int:guess_pk>/correct/',
         games.GuessCorrectView.as_view(), name='guess_correct'),
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
