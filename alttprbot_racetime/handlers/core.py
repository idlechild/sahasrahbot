from itertools import groupby

import tortoise.exceptions

from alttprbot_discord.bot import discordbot
from alttprbot import models
from alttprbot import tournaments
from alttprbot_racetime.misc.konot import KONOT
from racetime_bot import RaceHandler, can_monitor, monitor_cmd


class SahasrahBotCoreHandler(RaceHandler):
    """
    SahasrahBot race handler. Generates seeds, presets, and frustration.
    """
    stop_at = ['cancelled', 'finished']
    tournament = None
    konot: KONOT = None
    status = None
    unlisted = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.seed_rolled = False

    async def begin(self):
        self.state['locked'] = False

        if self.data.get('status', {}).get('value') in ['open', 'invitational']:
            await self.intro()

        await self.setup_tournament()
        await self.setup_konot()

    async def setup_tournament(self):
        if self.tournament:
            return

        race = await models.TournamentResults.get_or_none(srl_id=self.data.get('name'))
        if not race:
            return

        try:
            self.tournament = await tournaments.fetch_tournament_handler(
                event=race.event,
                episodeid=race.episode_id,
                rtgg_handler=self
            )
            await self.tournament.on_room_resume()
        except:
            self.logger.exception("Error while association tournament race to handler.")

    async def setup_konot(self):
        if self.konot:
            return

        try:
            self.konot = await KONOT.resume(self)
        except tortoise.exceptions.DoesNotExist:
            self.konot = None

    async def race_data(self, data):
        self.data = data.get('race')

        await self.race_data_hook()

        if self.tournament:
            pending_entrants = [e for e in self.data['entrants'] if e.get('status', {}).get('value', {}) == 'requested']
            for entrant in pending_entrants:
                entrant_id = entrant['user']['id']

                if entrant_id in self.tournament.player_racetime_ids:
                    await self.accept_request(entrant_id)

                elif await self.tournament.can_gatekeep(entrant_id):
                    await self.accept_request(entrant_id)
                    await self.add_monitor(entrant_id)

        fire_event = False

        status = self.data.get('status', {}).get('value')
        if status != self.status and self.status is not None:
            fire_event = True

        self.status = status
        if fire_event:
            self.status = status
            method = f'status_{status}'

            discordbot.dispatch(f"racetime_{status}", self, data)

            if hasattr(self, method):
                self.logger.debug('[%(race)s] Calling status handler for %(status)s' % {
                    'race': self.data.get('name'),
                    'status': status,
                })
                await getattr(self, method)()

        unlisted = self.data.get('unlisted', False)
        if unlisted != self.unlisted:
            if unlisted:
                await models.RTGGUnlistedRooms.update_or_create(room_name=self.data.get('name'), defaults={'category': self.bot.category_slug})
            else:
                await models.RTGGUnlistedRooms.filter(room_name=self.data.get('name')).delete()
        self.unlisted = unlisted

    async def error(self, data):
        await self.send_message(f"Command raised exception: {','.join(data.get('errors'))}")
        # raise Exception(data.get('errors'))

    async def status_in_progress(self):
        if self.tournament:
            await self.tournament.on_race_start()

    async def status_pending(self):
        if self.tournament:
            await self.tournament.on_race_pending()

    async def status_open(self):
        pass

    async def status_invitational(self):
        pass

    async def status_finished(self):
        if self.konot:
            await self.konot.create_next_room()

    async def race_data_hook(self):
        pass

    async def intro(self):
        """
        Send introduction messages.
        """
        if not self.state.get('intro_sent'):
            await self.send_message(
                f"Hi!  I'm SahasrahBot, your friendly robotic elder and randomizer seed roller! Use {self.command_prefix}help to see what I can do!   Check out https://sahasrahbot.synack.live/rtgg.html for more info."
            )
            self.state['intro_sent'] = True

    async def end(self):
        # await self.send_message(f"SahasrahBot is now leaving this race room.  Have a great day!")
        self.logger.info(f"Leaving race room {self.data.get('name')}")

    @property
    def teams(self):
        if self.data.get('team_race', False) is False:
            raise Exception('This is not a team race.')

        entrants = [(e['user']['name'], e['team']['name']) for e in self.data['entrants']]
        return {key: [v[0] for v in val] for key, val in groupby(sorted(entrants, key=lambda ele: ele[1]), key=lambda ele: ele[1])}

    @property
    def is_equal_teams(self):
        def all_equal(iterable):
            g = groupby(iterable)
            return next(g, True) and not next(g, False)

        teams = self.teams
        return all_equal([len(teams[t]) for t in teams])

    async def ex_cancel(self, args, message):
        self.seed_rolled = False
        await self.set_bot_raceinfo("New Race")
        await self.send_message("Reseting bot state.  You may now roll a new game.")

    async def ex_tournamentrace(self, args, message):
        if await self.is_locked(message):
            return

        if self.tournament:
            await self.tournament.process_tournament_race()

    async def ex_konot(self, args, message):
        await self.send_message("Setting up new KONOT race series!  The last player(s) to finish will be eliminated.  Once this race finishes, a new race will be created and the players advancing will be invited to the new room.")
        await self.set_bot_raceinfo("KONOT Series, Segment #1")
        self.konot = await KONOT.create_new(self.data['category']['slug'], self)

    @monitor_cmd
    async def ex_lock(self, args, message):
        """
        Handle !lock commands.
        Prevent seed rolling unless user is a race monitor.
        """
        self.state['locked'] = True
        await self.send_message(
            'Lock initiated. I will now only roll seeds for race monitors.'
        )

    @monitor_cmd
    async def ex_unlock(self, args, message):
        """
        Handle !unlock commands.
        Remove lock preventing seed rolling unless user is a race monitor.
        """
        self.state['locked'] = False
        await self.send_message(
            'Lock released. Anyone may now roll a seed.'
        )

    async def is_locked(self, message):
        """
        Check if room is locked or seed already exists.
        Post in chat if that is the case.
        """
        if self.seed_rolled:
            await self.send_message(
                'I already rolled a seed!  Use !cancel to clear the currently rolled game.'
            )
            return True
        if self.state.get('locked') and not can_monitor(message):
            await self.send_message(
                'Seed rolling is locked in this room.  Only the creator of this room, a race monitor, or a moderator can roll.'
            )
            return True

        return False

    async def ex_promote(self, args, message):
        if self.tournament:
            racetime_id = message.get('user', {}).get('id', None)
            if await self.tournament.can_gatekeep(racetime_id):
                await self.add_monitor(racetime_id)
