import discord
from discord.ext import commands, tasks
import requests
import os
from dotenv import load_dotenv
import urllib.parse  # For query encoding
import re

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

cache = {}  # Simple in-memory cache

INSIGHT_API_KEY = os.getenv('INSIGHT_API_KEY')
EXOPLANET_API_KEY = os.getenv('EXOPLANET_API_KEY')
CHANNEL_ID = int(os.getenv('CHANNEL_ID'))

async def _fetch_weather(send_to):
    cache_key = 'weather'
    if cache_key in cache:
        data = cache[cache_key]
    else:
        url = f'https://api.nasa.gov/insight_weather/?api_key={INSIGHT_API_KEY}&feedtype=json&ver=1.0'
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            cache[cache_key] = data
        else:
            await send_to.send('Failed to fetch weather data.')
            return
    sol = data['sol_keys'][-1] if data['sol_keys'] else None
    if sol:
        weather = data[sol]
        temp = weather['AT']['mx']
        pressure = weather.get('PRE', {}).get('av', 'N/A')
        wind_speed = weather.get('HWS', {}).get('av', 'N/A')
        embed = discord.Embed(title=f'Mars Weather - Sol {sol}', color=0xff4500)
        embed.add_field(name='Max Temp (K)', value=temp, inline=True)
        embed.add_field(name='Avg Pressure (Pa)', value=pressure, inline=True)
        embed.add_field(name='Avg Wind Speed (m/s)', value=wind_speed, inline=True)
        await send_to.send(embed=embed)
    else:
        await send_to.send('No recent weather data available.')

async def _fetch_photo(send_to, rover='perseverance', sol=1000):
    cache_key = f'photo_{rover}_{sol}'
    if cache_key in cache:
        photo_url = cache[cache_key]
    else:
        url = f'https://api.nasa.gov/mars-photos/api/v1/rovers/{rover}/photos?sol={sol}&api_key=DEMO_KEY'
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            if data['photos']:
                photo_url = data['photos'][0]['img_src']
                cache[cache_key] = photo_url
            else:
                await send_to.send(f'No photos found for {rover} on Sol {sol}.')
                return
        else:
            await send_to.send('Failed to fetch photo.')
            return
    embed = discord.Embed(title=f'Mars Photo from {rover} - Sol {sol}', color=0xff4500)
    embed.set_image(url=photo_url)
    await send_to.send(embed=embed)

async def _fetch_exoplanets(send_to, count=5):
    cache_key = f'exoplanets_{count}'
    if cache_key in cache:
        data = cache[cache_key]
    else:
        query = f"select top {count} pl_name, discoverymethod, pl_orbsmax from pscomppars order by pl_disc desc"
        encoded_query = urllib.parse.quote(query)
        url = f'https://exoplanetarchive.ipac.caltech.edu/TAP/sync?query={encoded_query}&format=json'
        headers = {'X-API-Key': EXOPLANET_API_KEY} if EXOPLANET_API_KEY else {}
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            cache[cache_key] = data
        else:
            await send_to.send('Failed to fetch exoplanet data.')
            return
    if data:
        embed = discord.Embed(title='Recent Exoplanets', color=0x8a2be2)
        for i, planet in enumerate(data, 1):
            name = planet.get('pl_name', 'Unknown')
            method = planet.get('discoverymethod', 'N/A')
            dist = planet.get('pl_orbsmax', 'N/A')
            embed.add_field(name=f'{i}. {name}', value=f'Method: {method}\nSemi-major axis (AU): {dist}', inline=False)
        await send_to.send(embed=embed)
    else:
        await send_to.send('No exoplanet data available.')

@bot.event
async def on_ready():
    print(f'{bot.user} running on discord!')
    mars_updates.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if bot.user.mentioned_in(message):
        text = message.content.lower().replace(f'<@{bot.user.id}>', '').strip()
        if any(word in text for word in ['weather', 'temp', 'mars weather']):
            await _fetch_weather(message.channel)
        elif any(word in text for word in ['photo', 'picture', 'image', 'rover']):
            # Simple parse for rover/sol
            rover_match = re.search(r'(curiosity|perseverance|opportunity)', text)
            sol_match = re.search(r'sol (\d+)', text)
            rover = rover_match.group(1) if rover_match else 'perseverance'
            sol = int(sol_match.group(1)) if sol_match else 1000
            await _fetch_photo(message.channel, rover, sol)
        elif any(word in text for word in ['exoplanet', 'planet', 'exoplanets']):
            count_match = re.search(r'(\d+)', text)
            count = int(count_match.group(1)) if count_match else 5
            await _fetch_exoplanets(message.channel, count)
        else:
            await message.reply("Hey! Ask me about Mars weather, rover photos, or exoplanets (e.g., 'how's the weather?', 'show perseverance photo', 'top 3 exoplanets').")

    await bot.process_commands(message)

@bot.command(name='mars_weather')
async def get_weather(ctx):
    await _fetch_weather(ctx)

@bot.command(name='mars_photo')
async def get_photo(ctx, rover='perseverance', sol=1000):
    await _fetch_photo(ctx, rover, sol)

@bot.command(name='exoplanets')
async def get_exoplanets(ctx, count=5):
    await _fetch_exoplanets(ctx, count)

@tasks.loop(hours=6)  # Update every 6 hours
async def mars_updates():
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        cache_key = 'weather'
        if cache_key not in cache:
            url = f'https://api.nasa.gov/insight_weather/?api_key={INSIGHT_API_KEY}&feedtype=json&ver=1.0'
            response = requests.get(url)
            if response.status_code == 200:
                data = response.json()
                cache[cache_key] = data
        sol = cache['weather']['sol_keys'][-1] if 'weather' in cache and cache['weather']['sol_keys'] else None
        if sol:
            weather = cache['weather'][sol]
            temp = weather['AT']['mx']
            pressure = weather.get('PRE', {}).get('av', 'N/A')
            embed = discord.Embed(title='Mars Live Update', color=0xff4500)
            embed.add_field(name=f'Sol {sol}', value=f'Max Temp: {temp}K | Pressure: {pressure}Pa', inline=False)
            await channel.send(embed=embed)

bot.run(os.getenv('DISCORD_TOKEN'))