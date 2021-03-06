from xenon import *
from xenon.cmd import *
import dc_interactions as dc
from motor.motor_asyncio import AsyncIOMotorClient
import aioredis
import json
from os import environ as env
import random
import asyncio
import traceback
import sys
from datetime import datetime


class Xenon(dc.InteractionBot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, ctx_klass=CustomContext)
        self.mongo = AsyncIOMotorClient(env.get("MONGO_URL", "mongodb://localhost"))
        self.db = self.mongo.xenon
        self.redis = None
        self.http = None
        self.relay = None

        self._receiver = aioredis.pubsub.Receiver()

    async def on_command_error(self, ctx, e):
        if isinstance(e, asyncio.CancelledError):
            raise e

        else:
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            print("Command Error:\n", tb, file=sys.stderr)

            error_id = unique_id()
            await self.redis.setex(f"cmd:errors:{error_id}", 60 * 60 * 24, json.dumps({
                "command": ctx.command.full_name,
                "timestamp": datetime.utcnow().timestamp(),
                "author": ctx.author.id,
                "traceback": tb
            }))
            await ctx.respond_with_source(**create_message(
                "An unexpected error occurred. Please report this on the "
                "[Support Server](https://xenon.bot/discord).\n\n"
                f"**Error Code**: `{error_id.upper()}`",
                f=Format.ERROR
            ))

    async def execute_command(self, command, payload, remaining_options):
        await self.redis.hincrby("cmd:commands", command.full_name, 1)

        # Global rate limits to prevent abuse
        block_bucket = payload.guild_id or payload.member["user"]["id"]
        is_blacklisted = await self.redis.exists(f"cmd:blacklist:{block_bucket}")
        if is_blacklisted:
            await self.redis.incr("cmd:commands:blocked")
            await self.redis.setex(f"cmd:blacklist:{block_bucket}", random.randint(60 * 15, 60 * 60), 1)
            return dc.InteractionResponse.message(**create_message(
                "You are being **blocked from using Xenon commands** due to exceeding internal rate limits. "
                "These rate limits are in place to protect our infrastructure. Please be patient and wait a few hours "
                "before trying to run another command.",
                embed=False,
                f=Format.ERROR
            ), ephemeral=True)

        cmd_count = int(await self.redis.get(f"cmd:commands:{block_bucket}") or 0)
        if cmd_count > 5:
            await self.redis.setex(f"cmd:blacklist:{block_bucket}", random.randint(60 * 15, 60 * 60), 1)

        else:
            await self.redis.setex(f"cmd:commands:{block_bucket}", 2, cmd_count + 1)

        # Apply morph
        morph_target = await self.redis.get(f"cmd:morph:{payload.member['user']['id']}")
        if morph_target is not None:
            try:
                member = await self.http.get_guild_member(payload.guild_id, morph_target.decode())
            except rest.HTTPNotFound:
                pass
            else:
                guild = await self.http.get_guild(payload.guild_id)
                if guild.owner_id == member.id:
                    perms = Permissions.all()

                else:
                    perms = Permissions.none()
                    roles = sorted(guild.roles, key=lambda r: r.position)
                    for role in roles:
                        if role.id in member.roles:
                            perms.value |= role.permissions.value

                payload.morph_source = payload.member['user']['id']
                payload.member = member.to_dict()
                payload.member["permissions"] = perms.value

        return await super().execute_command(command, payload, remaining_options)

    async def gateway_subscriber(self):
        await self.redis.subscribe(
            self._receiver.channel("gateway:events:message_reaction_add")
        )
        async for channel, msg in self._receiver.iter():
            event_name = channel.name.decode().replace("gateway:events:", "")
            self.dispatch(event_name, json.loads(msg))

    async def prepare(self):
        self.redis = await aioredis.create_redis_pool(env.get("REDIS_URL", "redis://localhost"))

        self.relay = com.Relay(self.redis)
        self.relay.start_reader()

        ratelimits = rest.RedisRatelimitHandler(self.redis)
        self.http = rest.HTTPClient(self.token, ratelimits)

        self.loop.create_task(self.gateway_subscriber())

        await super().prepare()
        # await self.flush_commands()
        self.loop.create_task(self.push_commands())

    def make_request(self, method, path, data=None, **params):
        req = rest.Request(method, path, **params)
        self.http.start_request(req, json=data)
        return req
