import logging
import os
import random
from dataclasses import dataclass
from typing import List
import base64

import aiocache
import aiofiles
import pyz3r
import yaml
from aiohttp.client_exceptions import ClientResponseError
from alttprbot import models
from alttprbot.alttprgen.randomizer import ctjets, mysterydoors
from alttprbot.exceptions import SahasrahBotException
from alttprbot_discord.util.alttpr_discord import ALTTPRDiscord
from alttprbot_discord.util.alttprdoors_discord import AlttprDoorDiscord
from alttprbot_discord.util.sm_discord import SMDiscord, SMZ3Discord
from slugify import slugify
from tenacity import (AsyncRetrying, RetryError, retry_if_exception_type,
                      stop_after_attempt)
from tortoise.exceptions import DoesNotExist


class PresetNotFoundException(SahasrahBotException):
    pass


class AttemptToSaveGlobalPreset(SahasrahBotException):
    pass


class NamespaceNotFound(SahasrahBotException):
    pass


class NoPresetSpecified(SahasrahBotException):
    pass


class WeightsetNotFoundException(SahasrahBotException):
    pass


@aiocache.cached(ttl=60, cache=aiocache.SimpleMemoryCache)
async def get_full_preset_list(path) -> List[str]:
    return [f for f in os.listdir(path) if f.endswith(".yaml")]


@dataclass
class PresetData:
    randomizer: str
    preset_name: str
    preset_data: dict
    namespace: str = None


class SahasrahBotPresetCore():
    randomizer: str = "default"
    preset_data: dict = None

    @property
    def global_preset_path(self) -> str:
        return os.path.join("presets", self.randomizer)

    def __init__(self, preset: str = None):
        self.namespace = None
        self.preset = None
        self.preset_data: dict = None

        if preset:
            parsed = preset.lower().split('/')
            if len(parsed) == 1:
                # we have a global preset
                self.namespace = None
                self.preset = preset
            else:
                self.namespace = parsed[0]
                self.preset = parsed[1]

    @classmethod
    async def custom(cls, content, preset_name=None):
        preset = cls(preset_name)
        preset.preset_data = yaml.safe_load(content)

        return preset

    @classmethod
    async def custom_from_dict(cls, preset_dict, preset_name=None):
        preset = cls(preset_name)
        preset.preset_data = preset_dict

        return preset

    async def search(self, value: str) -> List[str]:
        preset_files = await get_full_preset_list(self.global_preset_path)
        return sorted([os.path.splitext(a)[0] for a in preset_files if a.startswith(value.lower())][:25])

    async def fetch(self) -> PresetData:
        if self.preset is None:
            raise NoPresetSpecified("No preset was specified.")

        if self.namespace is None:
            await self._fetch_global()
        else:
            await self._fetch_namespaced()

        return PresetData(
            randomizer=self.randomizer,
            preset_data=self.preset_data,
            preset_name=self.preset,
            namespace=self.namespace
        )

    async def save(self) -> None:
        if self.preset is None:
            raise NoPresetSpecified("No preset was specified.")

        if self.namespace is None:
            raise AttemptToSaveGlobalPreset("You cannot save a global preset.")

        body = yaml.dump(self.preset_data)
        namespace_data = await models.PresetNamespaces.get(name=self.namespace)

        await models.Presets.update_or_create(randomizer=self.randomizer, preset_name=self.preset, namespace=namespace_data, defaults={'content': body})

    async def _fetch_global(self):
        basename = os.path.basename(f'{self.preset}.yaml')
        try:
            async with aiofiles.open(os.path.join(self.global_preset_path, basename)) as f:
                self.preset_data = yaml.safe_load(await f.read())
        except FileNotFoundError as err:
            raise PresetNotFoundException(
                f'Could not find preset {self.preset}.  See a list of available presets at https://sahasrahbot.synack.live/presets.html') from err

    async def _fetch_namespaced(self):
        data = await models.Presets.get_or_none(preset_name=self.preset, randomizer=self.randomizer, namespace__name=self.namespace)

        if data is None:
            raise PresetNotFoundException(f'Could not find preset {self.preset} in namespace {self.namespace}.')

        self.preset_data = yaml.safe_load(data.content)


class ALTTPRPreset(SahasrahBotPresetCore):
    randomizer = 'alttpr'

    # TODO: Make this so it isn't an absolute dumpster fire
    # this code really sucks
    async def generate(self, hints=False, nohints=False, spoilers="off", tournament=True, allow_quickswap=False, endpoint_prefix="") -> ALTTPRDiscord:
        if self.preset_data is None:
            await self.fetch()

        settings = self.preset_data['settings']  # pylint: disable=E1136
        doors = self.preset_data.get('doors', False)
        if doors:
            if hints:
                settings['hints'] = 'on'
            elif nohints:
                settings['hints'] = 'off'

            if allow_quickswap:
                settings['quickswap'] = True

            seed = await AlttprDoorDiscord.create(
                settings=settings,
                spoilers=spoilers == "on"
            )
            hash_id = seed.hash
        else:
            if self.preset_data.get('customizer', False):
                if 'l' not in settings:
                    settings['l'] = {}
                for i in self.preset_data.get('forced_locations', {}):
                    quantity = i.get('quantity', 1)
                    if quantity > len(i['locations']):
                        quantity = len(i['locations'])
                    for q in range(i.get('quantity', 1)):
                        logging.info("Placing item %s (#%s)", i['item'], q)
                        if random.randint(1, 100) <= i.get('chance', 100):
                            location = random.choice([l for l in i['locations'] if l not in settings['l']])
                            if not settings['l'].get(location, None) and i.get('override', True):
                                settings['l'][location] = i['item']
                                try:
                                    i['locations'].remove(location)
                                except ValueError:
                                    continue
                            else:
                                logging.info("Skipping \"%s\" because it is already populated with \"%s\" and override is true.", base64.b64decode(location).decode("utf8"), settings['l'][location])

            if hints:
                settings['hints'] = 'on'
            elif nohints:
                settings['hints'] = 'off'

            settings['tournament'] = tournament
            settings['spoilers'] = spoilers

            if not 'allow_quickswap' in settings:
                settings['allow_quickswap'] = allow_quickswap

            if self.preset_data.get('customizer', False):
                endpoint = endpoint_prefix + "/api/customizer"
            else:
                endpoint = endpoint_prefix + "/api/randomizer"

            seed = await ALTTPRDiscord.generate(
                settings=settings,
                endpoint=endpoint
            )
            hash_id = seed.hash

        await models.AuditGeneratedGames.create(
            randomizer=self.randomizer,
            hash_id=hash_id,
            permalink=seed.url,
            settings=settings,
            gentype='preset',
            genoption=self.preset,
            customizer=1 if self.preset_data.get('customizer', False) else 0,
            doors=doors
        )
        return seed


class ALTTPRMystery(SahasrahBotPresetCore):
    randomizer = 'alttprmystery'

    @property
    def global_preset_path(self) -> str:
        return "weights"

    async def generate(self, spoilers="off", tournament=True, allow_quickswap=True):
        if self.preset_data is None:
            await self.fetch()

        try:
            async for attempt in AsyncRetrying(stop=stop_after_attempt(5), retry=retry_if_exception_type(ClientResponseError)):
                with attempt:
                    try:
                        mystery = await mystery_generate(self.preset_data, spoilers=spoilers)

                        if mystery.doors:
                            seed = await AlttprDoorDiscord.create(
                                settings=mystery.settings,
                                spoilers=spoilers != "mystery",
                            )
                        else:
                            if mystery.customizer:
                                endpoint = "/api/customizer"
                            else:
                                endpoint = "/api/randomizer"

                            mystery.settings['tournament'] = tournament
                            mystery.settings['allow_quickswap'] = allow_quickswap
                            seed = await ALTTPRDiscord.generate(settings=mystery.settings, endpoint=endpoint)
                    except:
                        # await models.AuditGeneratedGames.create(
                        #     randomizer='alttpr',
                        #     settings=mystery.settings,
                        #     gentype='mystery failure',
                        #     genoption=self.preset,
                        #     customizer=1 if mystery.customizer else 0,
                        #     doors=mystery.doors
                        # )
                        logging.exception("Failed to generate game, retrying...")
                        raise
        except RetryError as e:
            raise e.last_attempt._exception from e

        await models.AuditGeneratedGames.create(
            randomizer='alttpr',
            hash_id=seed.hash,
            permalink=seed.url,
            settings=mystery.settings,
            gentype='mystery',
            genoption=self.preset,
            customizer=1 if mystery.customizer else 0,
            doors=mystery.doors
        )

        mystery.seed = seed
        return mystery

    async def generate_test_game(self):
        if self.preset_data is None:
            await self.fetch()

        mystery = await mystery_generate(weights=self.preset_data)

        return mystery


class SMPreset(SahasrahBotPresetCore):
    randomizer = 'sm'

    async def generate(self, tournament=True):
        if self.preset_data is None:
            await self.fetch()

        settings = self.preset_data['settings']  # pylint: disable=E1136
        settings['race'] = "true" if tournament else "false"
        seed = await SMDiscord.create(
            settings=settings,
        )
        hash_id = seed.slug_id

        await models.AuditGeneratedGames.create(
            randomizer=self.randomizer,
            hash_id=hash_id,
            permalink=seed.url,
            settings=settings,
            gentype='preset',
            genoption=self.preset,
            customizer=0
        )
        return seed


class SMZ3Preset(SahasrahBotPresetCore):
    randomizer = 'smz3'

    async def generate(self, tournament=True):
        if self.preset_data is None:
            await self.fetch()

        settings = self.preset_data['settings']  # pylint: disable=E1136
        settings['race'] = "true" if tournament else "false"
        seed = await SMZ3Discord.create(
            settings=settings,
        )
        hash_id = seed.slug_id

        await models.AuditGeneratedGames.create(
            randomizer=self.randomizer,
            hash_id=hash_id,
            permalink=seed.url,
            settings=settings,
            gentype='preset',
            genoption=self.preset,
            customizer=0
        )
        return seed


class CTJetsPreset(SahasrahBotPresetCore):
    randomizer = 'ctjets'

    async def generate(self):
        if self.preset_data is None:
            await self.fetch()

        settings = self.preset_data['settings']  # pylint: disable=E1136
        seed_uri = await ctjets.roll_ctjets(settings, version=self.preset_data.get('version', '3_1_0'))

        await models.AuditGeneratedGames.create(
            randomizer=self.randomizer,
            hash_id=None,
            permalink=seed_uri,
            settings=settings,
            gentype='preset',
            genoption=self.preset,
            customizer=0
        )
        return seed_uri


async def mystery_generate(weights, spoilers="mystery"):
    if 'preset' in weights:
        rolledpreset = pyz3r.mystery.get_random_option(weights['preset'])
        if rolledpreset == 'none':
            return mysterydoors.generate_doors_mystery(weights=weights, spoilers=spoilers)  # pylint: disable=unbalanced-tuple-unpacking
        else:
            data = ALTTPRPreset(rolledpreset)
            await data.fetch()
            # preset_dict = await fetch_preset(rolledpreset, randomizer='alttpr')
            settings = data.preset_data['settings']
            customizer = data.preset_data.get('customizer', False)
            doors = data.preset_data.get('doors', False)
            settings.pop('name', None)
            settings.pop('notes', None)
            settings['spoilers'] = spoilers
            custom_instructions = pyz3r.mystery.get_random_option(weights.get('custom_instructions', None))

            return mysterydoors.AlttprMystery(
                weights=weights,
                settings=settings,
                customizer=customizer,
                doors=doors,
                custom_instructions=custom_instructions
            )
    else:
        return mysterydoors.generate_doors_mystery(weights=weights, spoilers=spoilers)  # pylint: disable=unbalanced-tuple-unpacking


async def create_or_retrieve_namespace(discord_user_id, discord_user_name):
    tempnamespaceslug = slugify(discord_user_name, max_length=20)
    try:
        namespace, _ = await models.PresetNamespaces.get_or_create(discord_user_id=discord_user_id, defaults={'name': tempnamespaceslug})
    except DoesNotExist:
        tempnamespaceslug = tempnamespaceslug + str(random.randint(0, 99))
        namespace, _ = await models.PresetNamespaces.get_or_create(discord_user_id=discord_user_id, defaults={'name': tempnamespaceslug})

    return namespace


PRESET_CLASS_MAPPING = {
    'alttpr': ALTTPRPreset,
    'alttprmystery': ALTTPRMystery,
    'ctjets': CTJetsPreset,
    'smz3': SMZ3Preset,
    'sm': SMPreset
}
