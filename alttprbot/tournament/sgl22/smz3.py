import os

from alttprbot.alttprgen.preset import get_preset
from alttprbot.tournament.core import TournamentConfig
from alttprbot_discord.bot import discordbot
from .sglcore import SGLRandomizerTournamentRace


class SMZ3(SGLRandomizerTournamentRace):
    async def configuration(self):
        guild = discordbot.get_guild(590331405624410116)
        return TournamentConfig(
            guild=guild,
            racetime_category='sgl',
            racetime_goal="Super Metroid Link to the Past Combo Randomizer",
            event_slug="sgl22smz3",
            audit_channel=discordbot.get_channel(772351829022474260),
            commentary_channel=discordbot.get_channel(631564559018098698),
            coop=False,
            gsheet_id=os.environ.get("SGL_RESULTS_SHEET"),
            auto_record=True,
            stream_delay=10,
        )

    async def roll(self):
        self.seed, self.preset_dict = await get_preset('hardfast', tournament=True, randomizer='smz3')

    @property
    def seed_info(self):
        return f"{self.seed.url} - {self.seed.code}"
