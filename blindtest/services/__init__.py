"""Game logic, kept synchronous so every caller shares it.

The Discord bot calls these helpers through ``discordbot.db.run_db``;
the admin, tests and the web front end call them directly.
"""
