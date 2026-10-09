from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.translation import gettext_lazy as _

from .models import Guild, Host, Player


@admin.register(Player)
class PlayerAdmin(UserAdmin):
    list_display = ('username', 'discord_name', 'is_staff', 'show_all_questions')
    list_filter = ('is_active', 'is_staff', 'is_superuser')
    search_fields = ('username', 'discord_name', 'discord_user_id', 'email')
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        (_('Personal info'), {'fields': ('email', 'discord_user_id',
                                         'discord_name',
                                         'show_all_questions')}),
        (_('Permissions'), {'fields': ('is_active', 'is_staff', 'is_superuser',
                                       'groups', 'user_permissions')}),
        (_('Important dates'), {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {'classes': ('wide',),
                'fields': ('username', 'email', 'discord_user_id',
                           'discord_name', 'usable_password',
                           'password1', 'password2')}),
    )


class HostInline(admin.TabularInline):
    model = Host
    extra = 0
    fields = ('mention', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(Guild)
class GuildAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'discord_id', 'default_channel_id',
                    'default_ping_role_id', 'created_at')
    search_fields = ('name', 'discord_id', 'default_channel_id',
                     'default_ping_role_id')
    inlines = (HostInline,)
    readonly_fields = ('created_at',)


@admin.register(Host)
class HostAdmin(admin.ModelAdmin):
    list_display = ('id', 'guild', 'mention', 'is_role', 'created_at')
    list_filter = ('guild',)
    search_fields = ('mention', 'guild__name')
    readonly_fields = ('created_at',)

    @admin.display(boolean=True, description=_('role'))
    def is_role(self, host):
        return host.is_role

