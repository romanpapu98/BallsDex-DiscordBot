from __future__ import annotations

import discord
import logging
import yarl
import math

from rich import print
from typing import cast
from datetime import datetime

from discord import app_commands
from discord.ext import commands
from discord.gateway import DiscordWebSocket

from ballsdex.settings import settings
from ballsdex.core.dev import Dev
from ballsdex.core.metrics import PrometheusServer
from ballsdex.core.models import BlacklistedID, Special, Ball, balls
from ballsdex.core.commands import Core

log = logging.getLogger("ballsdex.core.bot")

PACKAGES = ["config", "players", "countryballs", "info", "admin", "trade"]


def owner_check(ctx: commands.Context[BallsDexBot]):
    return ctx.bot.is_owner(ctx.author)


class CommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        bot = cast(BallsDexBot, interaction.client)
        if not bot.is_ready():
            if interaction.type != discord.InteractionType.autocomplete:
                await interaction.response.send_message(
                    "The bot is currently starting, please wait for a few minutes... "
                    f"({round((len(bot.shards)/bot.shard_count)*100)}%)",
                    ephemeral=True,
                )
            return False  # wait for all shards to be connected
        return await bot.blacklist_check(interaction)


class BallsDexBot(commands.AutoShardedBot):
    """
    BallsDex Discord bot
    """

    def __init__(
        self,
        command_prefix: str,
        dev: bool = False,
        **options,
    ):
        # An explaination for the used intents
        # guilds: needed for basically anything, the bot needs to know what guilds it has
        # and accordingly enable automatic spawning in the enabled ones
        # guild_messages: spawning is based on messages sent, content is not necessary
        # emojis_and_stickers: DB holds emoji IDs for the balls which are fetched from 3 servers
        intents = discord.Intents(
            guilds=True, guild_messages=True, emojis_and_stickers=True, message_content=True
        )

        super().__init__(command_prefix, intents=intents, tree_cls=CommandTree, **options)

        self.dev = dev
        self.prometheus_server: PrometheusServer | None = None

        self.tree.error(self.on_application_command_error)
        self.add_check(owner_check)  # Only owners are able to use text commands

        self._shutdown = 0
        self.blacklist: list[int] = []
        self.special_cache: list[Special] = []

    async def start_prometheus_server(self):
        self.prometheus_server = PrometheusServer(
            self, settings.prometheus_host, settings.prometheus_port
        )
        await self.prometheus_server.run()

    def assign_ids_to_app_groups(
        self, group: app_commands.Group, synced_commands: list[app_commands.AppCommandGroup]
    ):
        for synced_command in synced_commands:
            bot_command = group.get_command(synced_command.name)
            if not bot_command:
                continue
            bot_command.extras["mention"] = synced_command.mention
            if isinstance(bot_command, app_commands.Group) and bot_command.commands:
                self.assign_ids_to_app_groups(
                    bot_command, cast(list[app_commands.AppCommandGroup], synced_command.options)
                )

    def assign_ids_to_app_commands(self, synced_commands: list[app_commands.AppCommand]):
        for synced_command in synced_commands:
            bot_command = self.tree.get_command(synced_command.name, type=synced_command.type)
            if not bot_command:
                continue
            bot_command.extras["mention"] = synced_command.mention
            if isinstance(bot_command, app_commands.Group) and bot_command.commands:
                self.assign_ids_to_app_groups(
                    bot_command, cast(list[app_commands.AppCommandGroup], synced_command.options)
                )

    async def load_blacklist(self):
        self.blacklist = (
            await BlacklistedID.all().only("discord_id").values_list("discord_id", flat=True)
        )  # type: ignore

    async def load_special_cache(self):
        now = datetime.now()
        self.special_cache = await Special.filter(start_date__lte=now, end_date__gt=now)

    async def load_balls(self):
        balls.clear()
        for ball in await Ball.all():
            balls.append(ball)
        log.info(f"Loaded {len(balls)} balls")

    async def launch_shards(self) -> None:
        # override to add a log call on the number of shards that needs connecting
        if self.is_closed():
            return

        if self.shard_count is None:
            self.shard_count: int
            self.shard_count, gateway_url = await self.http.get_bot_gateway()
            log.info(
                f"Logged in to Discord, initiating connection. {self.shard_count} shards needed"
            )
            gateway = yarl.URL(gateway_url)
        else:
            gateway = DiscordWebSocket.DEFAULT_GATEWAY

        self._connection.shard_count = self.shard_count

        shard_ids = self.shard_ids or range(self.shard_count)
        self._connection.shard_ids = shard_ids

        for shard_id in shard_ids:
            initial = shard_id == shard_ids[0]
            await self.launch_shard(gateway, shard_id, initial=initial)

    async def on_ready(self):
        assert self.user
        log.info(f"Successfully logged in as {self.user} ({self.user.id})!")

        # set bot owners
        assert self.application
        if self.application.team:
            self.owner_id = self.application.team.owner_id
        else:
            self.owner_id = self.application.owner.id
        log.info(f"{self.owner_id} is set as the bot owner.")

        await self.load_balls()
        await self.load_blacklist()
        await self.load_special_cache()
        if self.blacklist:
            log.info(f"{len(self.blacklist)} blacklisted users.")

        log.info("Loading packages...")
        await self.add_cog(Core(self))
        if self.dev:
            await self.add_cog(Dev())

        loaded_packages = []
        for package in PACKAGES:
            try:
                await self.load_extension("ballsdex.packages." + package)
            except Exception:
                log.error(f"Failed to load package {package}", exc_info=True)
            else:
                loaded_packages.append(package)
        if loaded_packages:
            log.info(f"Packages loaded: {', '.join(loaded_packages)}")
        else:
            log.info("No package loaded.")

        synced_commands = await self.tree.sync()
        if synced_commands:
            log.info(f"Synced {len(synced_commands)} commands.")
            try:
                self.assign_ids_to_app_commands(synced_commands)
            except Exception:
                log.error("Failed to assign IDs to app commands", exc_info=True)
        else:
            log.info("No command to sync.")

        if "admin" in PACKAGES:
            for guild_id in settings.admin_guild_ids:
                guild = self.get_guild(guild_id)
                if not guild:
                    continue
                synced_commands = await self.tree.sync(guild=guild)
                log.info(f"Synced {len(synced_commands)} admin commands for guild {guild.id}.")

        if settings.prometheus_enabled:
            try:
                await self.start_prometheus_server()
            except Exception:
                log.exception("Failed to start Prometheus server, stats will be unavailable.")

        print(
            f"\n    [bold][red]{settings.bot_name} bot[/red] [green]"
            "is now operational![/green][/bold]\n"
        )

    async def blacklist_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.blacklist:
            await interaction.response.send_message(
                "You are blacklisted from the bot.", ephemeral=True
            )
            return False
        return True

    async def on_command_error(
        self, context: commands.Context, exception: commands.errors.CommandError
    ):
        if isinstance(
            exception, (commands.CommandNotFound, commands.CheckFailure, commands.DisabledCommand)
        ):
            return

        assert context.command
        if isinstance(exception, (commands.ConversionError, commands.UserInputError)):
            # in case we need to know what happened
            log.debug("Silenced command exception", exc_info=exception)
            await context.send_help(context.command)
            return

        if isinstance(exception, commands.MissingRequiredAttachment):
            await context.send("An attachment is missing.")
            return

        if isinstance(exception, commands.CommandInvokeError):
            if isinstance(exception.original, discord.Forbidden):
                await context.send("The bot does not have the permission to do something.")
                # log to know where permissions are lacking
                log.warning(
                    f"Missing permissions for text command {context.command.name}",
                    exc_info=exception.original,
                )
                return

            log.error(f"Error in text command {context.command.name}", exc_info=exception.original)
            await context.send(
                "An error occured when running the command. Contact support if this persists."
            )
            return

        await context.send(
            "An error occured when running the command. Contact support if this persists."
        )
        log.error(f"Unknown error in text command {context.command.name}", exc_info=exception)

    async def on_application_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        async def send(content: str):
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)

        if isinstance(error, app_commands.CheckFailure):
            if isinstance(error, app_commands.CommandOnCooldown):
                await send(
                    "This command is on cooldown. Please retry "
                    f"in {math.ceil(error.retry_after)} seconds."
                )
                return
            await send("You are not allowed to use that command.")
            return

        if isinstance(error, app_commands.CommandInvokeError):
            assert interaction.command

            if isinstance(error.original, discord.Forbidden):
                await send("The bot does not have the permission to do something.")
                # log to know where permissions are lacking
                log.warning(
                    f"Missing permissions for app command {interaction.command.name}",
                    exc_info=error.original,
                )
                return

            if isinstance(error.original, discord.InteractionResponded):
                # most likely an interaction received twice (happens sometimes),
                # or two instances are running on the same token.
                log.warning(
                    f"Tried invoking command {interaction.command.name}, but the "
                    "interaction was already responded to.",
                    exc_info=error.original,
                )
                # still including traceback because it may be a programming error

            log.error(
                f"Error in slash command {interaction.command.name}", exc_info=error.original
            )
            await send(
                "An error occured when running the command. Contact support if this persists."
            )
            return

        await send("An error occured when running the command. Contact support if this persists.")
        log.error("Unknown error in interaction", exc_info=error)

    async def on_error(self, event_method: str, /, *args, **kwargs):
        formatted_args = ", ".join(args)
        formatted_kwargs = " ".join(f"{x}={y}" for x, y in kwargs.items())
        log.error(
            f"Error in event {event_method}. Args: {formatted_args}. Kwargs: {formatted_kwargs}",
            exc_info=True,
        )
        self.tree.interaction_check
