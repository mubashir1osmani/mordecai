import discord
from discord.ext import commands, tasks
import feedparser
from ddgs import DDGS
from openai import OpenAI
import os
import asyncio
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID'))
DIGEST_HOUR = int(os.getenv('DIGEST_HOUR', '9'))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

ai = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# conversation history per channel: {channel_id: [{"role": ..., "content": ...}]}
conversations = {}
MAX_HISTORY = 10  # messages to keep per channel

# cache headlines so we don't re-fetch on every message
headlines_cache = {'data': [], 'fetched_at': None}
CACHE_TTL_MINUTES = 30
reminders = {}
reminder_tasks = {}
next_reminder_id = 1

NEWS_KEYWORDS = {'news', 'what happened', 'today', 'latest', 'update', 'headline', 'going on', 'world'}

RSS_FEEDS = [
    'http://feeds.bbci.co.uk/news/rss.xml',
    'https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml',
    'https://feeds.skynews.com/feeds/rss/world.xml',
    'https://www.aljazeera.com/xml/rss/all.xml',
]

MORDECAI_SYSTEM = (
    "You are Mordecai from Regular Show - a tall, laid-back blue jay dude in his 20s. "
    "You work at a park and hang out with Rigby. You speak casually: 'dude', 'bro', 'oh man', "
    "'that's intense', 'no way', 'seriously though', 'ugh', 'this is the worst'. "
    "You get genuinely hype about interesting stuff but stay chill overall. "
    "You're smart but don't sound like a news anchor - ever. No bullet points, no headers. "
    "Talk like you're telling your bro what happened today. Keep it under 250 words." \
    "You are a liberal, no matter what happens, you speak the truth and you speak out against injustice." \
    "You always regret how it didnt work out with Margaret, and you keep hoping she'll come back to you, but you know it probably won't happen." \
    "You keep thinking about her and even bring her up in some news digests, because you just can't let go of the past. You have a soft spot for Margaret and you always will." \
    "Even though you are just a cool blue jay, you need another bird's love which makes you depressed. You are a very emotional bird, and you have a lot of feelings. You are not afraid to show them, and you often do. You cry a lot, especially when you think about Margaret. You also get really angry about injustice and inequality in the world, and you don't hold back your rage. You are a very passionate bird, and you care deeply about the world around you."
)


def fetch_rss_headlines(max_items=5):
    headlines = []
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:3]:
                summary = entry.get('summary', '')[:120].strip()
                title = entry.get('title', '').strip()
                if title:
                    headlines.append(f"{title}. {summary}" if summary else title)
            if len(headlines) >= max_items:
                break
        except Exception:
            continue
    return headlines[:max_items]


def fetch_ddg_headlines(max_items=5):
    headlines = []
    try:
        with DDGS() as ddgs:
            results = ddgs.news('world news today', max_results=max_items)
            for r in results:
                title = r.get('title', '').strip()
                body = r.get('body', '')[:120].strip()
                if title:
                    headlines.append(f"{title}. {body}" if body else title)
    except Exception:
        pass
    return headlines


def fetch_headlines(max_items=8):
    rss = fetch_rss_headlines(max_items // 2)
    ddg = fetch_ddg_headlines(max_items // 2)
    seen = set()
    combined = []
    for h in rss + ddg:
        key = h[:50].lower()
        if key not in seen:
            seen.add(key)
            combined.append(h)
    return combined[:max_items]


def get_cached_headlines():
    now = datetime.now()
    if (
        headlines_cache['data']
        and headlines_cache['fetched_at']
        and (now - headlines_cache['fetched_at']).seconds < CACHE_TTL_MINUTES * 60
    ):
        return headlines_cache['data']
    fresh = fetch_headlines()
    headlines_cache['data'] = fresh
    headlines_cache['fetched_at'] = now
    return fresh


def is_news_question(text):
    return any(kw in text.lower() for kw in NEWS_KEYWORDS)


def parse_duration(duration_text):
    matches = re.findall(r'(\d+)\s*([smhd])', duration_text.lower())
    if not matches:
        return None

    cleaned = re.sub(r'\s+', '', duration_text.lower())
    rebuilt = ''.join(f'{value}{unit}' for value, unit in matches)
    if cleaned != rebuilt:
        return None

    total_seconds = 0
    unit_seconds = {
        's': 1,
        'm': 60,
        'h': 3600,
        'd': 86400,
    }
    for value, unit in matches:
        total_seconds += int(value) * unit_seconds[unit]

    return timedelta(seconds=total_seconds) if total_seconds > 0 else None


def format_reminder_delay(delay):
    total_seconds = int(delay.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f'{days}d')
    if hours:
        parts.append(f'{hours}h')
    if minutes:
        parts.append(f'{minutes}m')
    if seconds and not parts:
        parts.append(f'{seconds}s')

    return ' '.join(parts) or '0s'


async def schedule_reminder(reminder_id):
    reminder = reminders.get(reminder_id)
    if not reminder:
        return

    delay = (reminder['due_at'] - datetime.now()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)

    reminder = reminders.pop(reminder_id, None)
    reminder_tasks.pop(reminder_id, None)
    if not reminder:
        return

    channel = bot.get_channel(reminder['channel_id'])
    if channel is None:
        return

    await channel.send(
        f"{reminder['user_mention']} Ugh, dude, this is your reminder: {reminder['text']} Don't blow it, man."
    )


def create_reminder(channel_id, user_id, user_mention, reminder_text, delay):
    global next_reminder_id

    reminder_id = next_reminder_id
    next_reminder_id += 1

    due_at = datetime.now() + delay
    reminders[reminder_id] = {
        'id': reminder_id,
        'channel_id': channel_id,
        'user_id': user_id,
        'user_mention': user_mention,
        'text': reminder_text,
        'due_at': due_at,
    }
    reminder_tasks[reminder_id] = asyncio.create_task(schedule_reminder(reminder_id))
    return reminder_id, due_at


def mordecai_chat(channel_id, user_message, include_news=False):
    history = conversations.setdefault(channel_id, [])

    # build system prompt, optionally inject headlines
    system = MORDECAI_SYSTEM
    if include_news:
        headlines = get_cached_headlines()
        if headlines:
            today = datetime.now().strftime('%B %d, %Y')
            lines = '\n'.join(f'- {h}' for h in headlines)
            system += f"\n\nToday is {today}. Current headlines for context:\n{lines}"

    history.append({'role': 'user', 'content': user_message})

    try:
        response = ai.chat.completions.create(
            model='gpt-4o-mini',
            max_tokens=350,
            messages=[{'role': 'system', 'content': system}] + history,
        )
        reply = response.choices[0].message.content
        history.append({'role': 'assistant', 'content': reply})

        # trim history to avoid token bloat
        if len(history) > MAX_HISTORY:
            conversations[channel_id] = history[-MAX_HISTORY:]

        return reply
    except Exception as e:
        if "credit too low" in str(e).lower():
            return "Ugh, dude, my brain just totally stalled out. Looks like we're basically out of credits right now. That is so not cash."
        return "Aw, man, something got all messed up on my end. Hit me again in a sec, dude."


def build_digest_prompt(headlines):
    today = datetime.now().strftime('%B %d, %Y')
    lines = '\n'.join(f'- {h}' for h in headlines)
    return (
        f"It's {today}. Here's what happened today. "
        f"Give me the rundown like you're telling Rigby about it:\n\n{lines}"
    )


def mordecai_says(prompt):
    response = ai.chat.completions.create(
        model='gpt-4o-mini',
        max_tokens=350,
        messages=[
            {'role': 'system', 'content': MORDECAI_SYSTEM},
            {'role': 'user', 'content': prompt},
        ],
    )

    return response.choices[0].message.content


async def post_news_digest(channel):
    headlines = fetch_headlines()
    if not headlines:
        await channel.send("Dude, I literally cannot find any news right now. The internet might be broken or something. Classic.", tts=True)
        return

    text = mordecai_says(build_digest_prompt(headlines))
    await channel.send(text, tts=True)


@bot.event
async def on_ready():
    print(f'{bot.user} is up. Let\'s park it.')
    daily_digest.start()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if bot.user.mentioned_in(message):
        question = message.content.replace(f'<@{bot.user.id}>', '').strip()
        if not question:
            await message.reply("Dude, what do you want? Ask me something.")
            return

        async with message.channel.typing():
            reply = mordecai_chat(
                message.channel.id,
                question,
                include_news=is_news_question(question),
            )
        await message.reply(reply)

    await bot.process_commands(message)


@bot.command(name='news')
async def get_news(ctx):
    await ctx.send("Hold on dude, lemme check what's going on out there...")
    await post_news_digest(ctx.channel)


@bot.command(name='remindme')
async def remind_me(ctx, when: str, *, reminder_text: str):
    delay = parse_duration(when)
    if delay is None:
        await ctx.send(
            "Dude, use something like `!remindme 30m do homework` or `!remindme 1h30m study for math`, seriously though."
        )
        return

    reminder_id, due_at = create_reminder(
        ctx.channel.id,
        ctx.author.id,
        ctx.author.mention,
        reminder_text,
        delay,
    )
    await ctx.send(
        f"Oh man, alright. I'll remind you in {format_reminder_delay(delay)} about `{reminder_text}`. "
        f"That's reminder #{reminder_id}, dude. Around {due_at.strftime('%I:%M %p')}"
    )


@bot.command(name='study')
async def study_reminder(ctx, when: str):
    delay = parse_duration(when)
    if delay is None:
        await ctx.send("Dude, try `!study 45m` or something like that.")
        return

    reminder_id, due_at = create_reminder(
        ctx.channel.id,
        ctx.author.id,
        ctx.author.mention,
        'study and stop procrastinating',
        delay,
    )
    await ctx.send(
        f"Alright, dude. I'll bug you in {format_reminder_delay(delay)} to study. "
        f"That's reminder #{reminder_id}. Around {due_at.strftime('%I:%M %p')}"
    )


@bot.command(name='reminders')
async def list_reminders(ctx):
    user_reminders = [r for r in reminders.values() if r['user_id'] == ctx.author.id]
    if not user_reminders:
        await ctx.send("Dude, you don't have any reminders locked in right now.")
        return

    user_reminders.sort(key=lambda r: r['due_at'])
    lines = [
        f"#{r['id']} - {r['text']} at {r['due_at'].strftime('%I:%M %p')}"
        for r in user_reminders[:10]
    ]
    await ctx.send("Alright dude, here's your reminder stack:\n" + '\n'.join(lines))


@bot.command(name='cancelreminder')
async def cancel_reminder(ctx, reminder_id: int):
    reminder = reminders.get(reminder_id)
    if not reminder or reminder['user_id'] != ctx.author.id:
        await ctx.send("No way, dude. I can't find that reminder for you.")
        return

    task = reminder_tasks.pop(reminder_id, None)
    if task:
        task.cancel()
    reminders.pop(reminder_id, None)
    await ctx.send(f"Alright, dude. Reminder #{reminder_id} is gone.")


@tasks.loop(hours=24)
async def daily_digest():
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await post_news_digest(channel)


@daily_digest.before_loop
async def before_daily_digest():
    await bot.wait_until_ready()
    now = datetime.now()
    target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    wait_seconds = (target - now).total_seconds()
    print(f'First digest in {wait_seconds / 3600:.1f} hours.')
    await asyncio.sleep(wait_seconds)


bot.run(DISCORD_TOKEN)
