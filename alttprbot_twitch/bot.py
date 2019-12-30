from twitchio.ext import commands

from config import Config as c

twitchbot = commands.Bot(
    irc_token=c.SB_TWITCH_TOKEN,
    client_id=c.SB_TWITCH_CLIENT_ID,
    nick=c.SB_TWITCH_NICK,
    prefix=c.SB_TWITCH_PREFIX,
    initial_channels=c.SB_TWITCH_CHANNELS
)

twitchbot.load_module('alttprbot_twitch.cogs.gtbk')

@twitchbot.event
async def event_command_error(ctx, error):
    if isinstance(error, commands.errors.CheckFailure):
        raise error
    else:
        await ctx.send(error)
        raise error

