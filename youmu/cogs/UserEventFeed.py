import asyncio
import time
import discord
from discord.ext import commands
from youmu.modules import permissions
from youmu.reusables import send_large_message
from youmu.reusables import list_helpers
from youmu.embeds import oldembeds


class UserEventFeed(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.background_tasks.append(
            self.bot.loop.create_task(self.usereventfeed_background_loop())
        )

    @commands.command(name="uef_track", brief="Track mapping activity of a specified user")
    @commands.check(permissions.is_admin)
    @commands.check(permissions.is_not_ignored)
    async def track(self, ctx, user_id):
        """
        This will keep track of mapping activity for a specified user and send it to the current channel.

        user_id: osu! account ID
        """

        user = await self.bot.osu.get_user(u=user_id)
        if not user:
            await ctx.send("can't find a user with that id. maybe they are restricted.")
            return

        async with await self.bot.db.execute("SELECT osu_id FROM usereventfeed_tracklist WHERE osu_id = ?",
                                             [int(user.id)]) as cursor:
            check_is_already_tracked = await cursor.fetchone()
        if not check_is_already_tracked:
            await self.bot.db.execute("INSERT INTO usereventfeed_tracklist VALUES (?)", [int(user.id)])

        for event in user.events:
            async with await self.bot.db.execute("SELECT event_id FROM usereventfeed_history WHERE event_id = ?",
                                                 [int(event.id)]) as cursor:
                check_is_already_in_history = await cursor.fetchone()
            if not check_is_already_in_history:
                await self.bot.db.execute("INSERT INTO usereventfeed_history VALUES (?, ?, ?)",
                                          [int(user.id), int(event.id), int(time.time())])

        async with await self.bot.db.execute("SELECT channel_id FROM usereventfeed_channels "
                                             "WHERE channel_id = ? AND osu_id = ?",
                                             [int(ctx.channel.id), int(user.id)]) as cursor:
            check_is_channel_already_tracked = await cursor.fetchone()
        if check_is_channel_already_tracked:
            await ctx.send(f"User `{user.name}` is already tracked in this channel")
            return

        await self.bot.db.execute("INSERT INTO usereventfeed_channels VALUES (?, ?)",
                                  [int(user.id), int(ctx.channel.id)])
        await ctx.send(f"Tracked `{user.name}` in this channel")

        await self.bot.db.commit()

    @commands.command(name="uef_untrack", brief="Stop tracking the mapping activity of the specified user")
    @commands.check(permissions.is_admin)
    @commands.check(permissions.is_not_ignored)
    async def untrack(self, ctx, user_id):
        """
        Stop tracking mapping activity of a specified user in the current channel
        
        user_id: osu! account ID
        """

        user = await self.bot.osu.get_user(u=user_id)
        if user:
            user_id = user.id
            user_name = user.name
        else:
            user_name = user_id

        await self.bot.db.execute("DELETE FROM usereventfeed_channels WHERE osu_id = ? AND channel_id = ? ",
                                  [int(user_id), int(ctx.channel.id)])
        await self.bot.db.commit()

        await ctx.send(f"`{user_name}` is no longer tracked in this channel")

    @commands.command(name="uef_tracklist", brief="Show a list of all users' mapping activity being tracked and where")
    @commands.check(permissions.is_admin)
    @commands.check(permissions.is_not_ignored)
    async def tracklist(self, ctx, *args):
        """
        Show a list of all users who are being tracked and where

        optional parameter: 'everywhere' - it will show all channels, not just this one.
        """

        channel = ctx.channel
        async with await self.bot.db.execute("SELECT osu_id FROM usereventfeed_tracklist") as cursor:
            tracklist = await cursor.fetchall()
        if not tracklist:
            await ctx.send("user event feed tracklist is empty")
            return

        buffer = ":notepad_spiral: **Track list**\n\n"
        for one_entry in tracklist:
            async with await self.bot.db.execute("SELECT channel_id FROM usereventfeed_channels WHERE osu_id = ?",
                                                 [int(one_entry[0])]) as cursor:
                destination_list = await cursor.fetchall()
            destination_list_str = ""
            for destination_id in destination_list:
                destination_list_str += f"<#{destination_id[0]}> "
            if (str(channel.id) in destination_list_str) or "everywhere" in args:
                buffer += f"osu_id: `{one_entry[0]}` | channels: {destination_list_str}\n"
        embed = discord.Embed(color=0xff6781)

        await send_large_message.send_large_embed(channel, embed, buffer)

    async def usereventfeed_background_loop(self):
        print("UEF Loop launched!")
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(10)

                async with await self.bot.db.execute("SELECT osu_id FROM usereventfeed_tracklist") as cursor:
                    tracklist = await cursor.fetchall()
                if not tracklist:
                    # UEF tracklist is empty
                    await asyncio.sleep(3600)
                    continue

                print(time.strftime("%X %x %Z") + " | performing user event check")
                for user_id in tracklist:
                    await self.prepare_to_check(user_id[0])
                print(time.strftime("%X %x %Z") + " | finished user event check")
                await asyncio.sleep(3600)
            except Exception as e:
                print(time.strftime("%X %x %Z"))
                print("in usereventfeed_background_loop")
                print(e)
                await asyncio.sleep(7200)

    async def prepare_to_check(self, user_id):
        user = await self.bot.osu.get_user(u=user_id, event_days="2")
        if not user:
            print(f"{user_id} is restricted, untracking everywhere")
            await self.bot.db.execute("DELETE FROM usereventfeed_tracklist WHERE osu_id = ?", [int(user.id)])
            await self.bot.db.execute("DELETE FROM usereventfeed_channels WHERE osu_id = ?", [int(user.id)])
            await self.bot.db.commit()
            return

        async with await self.bot.db.execute("SELECT channel_id FROM usereventfeed_channels WHERE osu_id = ?",
                                             [int(user.id)]) as cursor:
            channel_list = await cursor.fetchall()
        if not channel_list:
            await self.bot.db.execute("DELETE FROM usereventfeed_tracklist WHERE osu_id = ?", [int(user.id)])
            await self.bot.db.commit()
            print(f"{user.id} is not tracked in any channel so I am untracking them")
            return

        channel_list = list_helpers.unnest_list(channel_list)
        await self.check_events(channel_list, user)

    async def check_events(self, channel_list, user):
        print(time.strftime("%X %x %Z") + f" | currently checking {user.name}")
        for event in user.events:
            async with await self.bot.db.execute("SELECT event_id FROM usereventfeed_history WHERE event_id = ?",
                                                 [int(event.id)]) as cursor:
                check_is_entry_in_history = await cursor.fetchone()
            if check_is_entry_in_history:
                continue

            await self.bot.db.execute("INSERT INTO usereventfeed_history VALUES (?, ?, ?)",
                                      [int(user.id), int(event.id), int(time.time())])
            await self.bot.db.commit()

            event_color = await self.get_event_color(event.display_text)
            if not event_color:
                # this is not the kind of event we are interested in posting
                continue

            result = await self.bot.osu.get_beatmapset(s=event.beatmapset_id)
            embed = await oldembeds.beatmapset(result, event_color)
            if not embed:
                print("uef track embed didn't return anything, this should not happen")
                continue

            display_text = event.display_text.replace("@", "")
            for channel_id in channel_list:
                channel = self.bot.get_channel(int(channel_id))
                if not channel:
                    await self.bot.db.execute("DELETE FROM usereventfeed_channels WHERE channel_id = ?",
                                              [int(channel_id)])
                    await self.bot.db.commit()
                    print(f"channel with id {channel_id} no longer exists "
                          "so I am removing it from the list")
                    continue

                await channel.send(display_text, embed=embed)

    async def get_event_color(self, string):
        if "has submitted" in string:
            return 0x2a52b2
        elif "has updated" in string:
            # return 0xb2532a
            return 0x0
        elif "qualified" in string:
            return 0x2ecc71
        elif "has been revived" in string:
            return 0xff93c9
        elif "has been deleted" in string:
            return 0xf2d7d5
        else:
            return 0x0


def setup(bot):
    bot.add_cog(UserEventFeed(bot))
