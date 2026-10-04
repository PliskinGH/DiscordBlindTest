"""URLs of the web admin."""

from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = 'webadmin'

urlpatterns = [
    path('', views.DashboardView.as_view(), name='dashboard'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('discord/connect/', views.DiscordConnectView.as_view(),
         name='discord_connect'),
    path('discord/callback/', views.DiscordCallbackView.as_view(),
         name='discord_callback'),
    path('g/<int:discord_guild_id>/', views.GuildView.as_view(), name='guild'),
    path('g/<int:discord_guild_id>/add/', views.AddGuildView.as_view(),
         name='guild_add'),
]
