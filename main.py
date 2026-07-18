import os
import sqlite3
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import disnake
from disnake.ext import commands

TESTER_ROLE_ID = 1505930643597430846

def is_admin_or_tester():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        role_ids = [role.id for role in ctx.author.roles]
        return TESTER_ROLE_ID in role_ids
    return commands.check(predicate)

# ==============================================================================
# Config
# ==============================================================================
BOT_TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "leaderboard.db")
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID", "1514906931494518864"))
CLAN_ADMIN_CHANNEL_ID = int(os.getenv("CLAN_ADMIN_CHANNEL_ID", "1514906931494518864"))

intents = disnake.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

testing_queue = []
queue_status = "ЗАКРЫТА"
player_tiers = {}
queue_message = None

# ==============================================================================
# Database
# ==============================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leaderboard (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            points INTEGER,
            kits TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            channel_id INTEGER,
            message_id INTEGER
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ==============================================================================
# Queue helpers
# ==============================================================================
async def update_queue_message():
    global queue_message, queue_status, testing_queue
    if not queue_message:
        return

    queue_text = "\n".join([f"{i+1}. <@{user_id}>" for i, user_id in enumerate(testing_queue)]) if testing_queue else "пусто"
    status_block = f"```diff\n+ ОТКРЫТА\n```" if queue_status == "ОТКРЫТА" else f"```diff\n- ЗАКРЫТА\n```"

    embed = disnake.Embed(title="🏆 ECL Tiers", color=0x1e90ff)
    embed.add_field(name="┃ Очередь на тестирование", value=" ", inline=False)
    embed.add_field(name="📊 Статус", value=status_block, inline=True)
    embed.add_field(name="👥 В очереди", value=f"{len(testing_queue)} чел.", inline=True)
    embed.add_field(name="🧪 Тестеров", value="1", inline=True)
    embed.add_field(name="📋 Очередь", value=f"```\n{queue_text}\n```", inline=False)
    embed.set_footer(text="ECL Tiers")
    try:
        await queue_message.edit(embed=embed)
    except Exception:
        pass

async def create_queue_ticket(guild):
    if not testing_queue:
        return

    user_id = testing_queue[0]
    tester_role = guild.get_role(1505930643597430846)

    try:
        member = await guild.fetch_member(user_id)
    except:
        member = None

    overwrites = {
        guild.default_role: disnake.PermissionOverwrite(read_messages=False),
        tester_role: disnake.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True)
    }
    if member:
        overwrites[member] = disnake.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True)

    ch_name = f"очередь-{member.name if member else user_id}"
    ticket_channel = await guild.create_text_channel(name=ch_name, overwrites=overwrites)

    embed = disnake.Embed(
        title="🎯 Ваша очередь подошла!",
        description=f"Приветствуем, <@{user_id}>!\n\nВы заняли 1-е место в очереди. Комната тестирования автоматически открыта.\nПожалуйста, ожидайте тестеров <@&1505930643597430846>.",
        color=0x1e90ff
    )
    embed.set_footer(text="ECL Tiers • Очередь")
    await ticket_channel.send(content=f"<@{user_id}> <@&1505930643597430846>", embed=embed, view=TicketCloseView())

# ==============================================================================
# Leaderboard helpers
# ==============================================================================
def generate_top_embed():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, name, points, kits FROM leaderboard ORDER BY points DESC LIMIT 10")
    players = cursor.fetchall()
    conn.close()

    embed = disnake.Embed(
        title="🏆 Официальный Топ Игроков • ECL Tiers",
        description="Актуальный рейтинг сильнейших игроков сервера.\n\n",
        color=0x7b2fbe
    )

    if not players:
        embed.description += "*Таблица лидеров пока пуста. Добавьте игроков через /топ_игрок!*"
    else:
        for index, (user_id, name, points, kits) in enumerate(players):
            place = "🥇" if index == 0 else "🥈" if index == 1 else "🥉" if index == 2 else f"`#{index + 1}`"
            embed.add_field(
                name=f"{place} Игрок: {name}",
                value=f"┃ **Очки:** `{points}`\n┃ **Киты:** {kits}\n┗ **Упоминание:** <@{user_id}>",
                inline=False
            )

    embed.set_footer(text="ECL Tiers • Сезон 1")
    return embed

async def auto_refresh_top(bot):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, message_id FROM config WHERE key = 'last_top_msg'")
    row = cursor.fetchone()
    conn.close()

    if row:
        ch_id, msg_id = row
        try:
            channel = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            if channel:
                message = await channel.fetch_message(msg_id)
                if message:
                    embed = generate_top_embed()
                    await message.edit(embed=embed)
                    return True
        except:
            pass
    return False

# ==============================================================================
# Views
# ==============================================================================
class StaffDecisionView(disnake.ui.View):
    def __init__(self, applicant_id: int, role_name: str):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.role_name = role_name

    @disnake.ui.button(label="Принять", style=disnake.ButtonStyle.green, emoji="✅")
    async def accept_staff(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.defer()
        try:
            member = await inter.guild.fetch_member(self.applicant_id)
            await member.send(
                f"🎉 **Поздравляем!** Ваша заявка на должность **{self.role_name}** была успешно **принята** "
                f"администратором {inter.author.mention}!"
            )
            ls_status = "✅ Уведомление в ЛС отправлено успешно."
        except Exception:
            ls_status = "❌ Не удалось отправить ЛС (у пользователя закрыты личные сообщения)."

        embed = inter.message.embeds[0]
        embed.color = 0x2ecc71
        embed.title = f"🛡️ Анкета: {self.role_name} [ОДОБРЕНО]"
        embed.add_field(
            name="📢 Вердикт администрации",
            value=f"Принят администратором {inter.author.mention}\n*{ls_status}*",
            inline=False
        )
        for child in self.children:
            child.disabled = True
        await inter.message.edit(embed=embed, view=self)

    @disnake.ui.button(label="Отклонить", style=disnake.ButtonStyle.danger, emoji="❌")
    async def reject_staff(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.defer()
        try:
            member = await inter.guild.fetch_member(self.applicant_id)
            await member.send(
                f"😔 К сожалению, ваша заявка на должность **{self.role_name}** была **отклонена** администрацией."
            )
            ls_status = "✅ Уведомление в ЛС отправлено успешно."
        except Exception:
            ls_status = "❌ Не удалось отправить ЛС (у пользователя закрыты личные сообщения)."

        embed = inter.message.embeds[0]
        embed.color = 0xe74c3c
        embed.title = f"🛡️ Анкета: {self.role_name} [ОТКЛОНЕНО]"
        embed.add_field(
            name="📢 Вердикт администрации",
            value=f"Отклонен администратором {inter.author.mention}\n*{ls_status}*",
            inline=False
        )
        for child in self.children:
            child.disabled = True
        await inter.message.edit(embed=embed, view=self)


class TicketCloseView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Закрыть тест", style=disnake.ButtonStyle.danger, emoji="🔒")
    async def close_ticket(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        TESTER_ROLE_ID = 1505930643597430846
        user_role_ids = [role.id for role in inter.author.roles]

        if TESTER_ROLE_ID not in user_role_ids and not inter.author.guild_permissions.administrator:
            return await inter.response.send_message("❌ Ошибка: Только Тестеры могут закрывать этот тикет!", ephemeral=True)

        await inter.response.send_message("⚠️ Тест завершен. Канал будет удален через 3 секунды...")

        global testing_queue
        if "очередь" in inter.channel.name and testing_queue:
            testing_queue.pop(0)
            await update_queue_message()
            if testing_queue:
                bot.loop.create_task(create_queue_ticket(inter.guild))

        await inter.channel.delete(delay=3.0)


class QueueView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Встать в очередь!", style=disnake.ButtonStyle.green, emoji="✅")
    async def join_queue(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        global queue_status, testing_queue, queue_message
        if not queue_message:
            queue_message = inter.message

        if queue_status == "ЗАКРЫТА":
            return await inter.response.send_message("❌ Извините, очередь закрыта!", ephemeral=True)
        if inter.author.id in testing_queue:
            return await inter.response.send_message("⚠️ Вы уже в очереди!", ephemeral=True)

        testing_queue.append(inter.author.id)
        await inter.response.send_message("✅ Вы успешно встали в очередь!", ephemeral=True)
        await update_queue_message()

        if len(testing_queue) == 1:
            await create_queue_ticket(inter.guild)

    @disnake.ui.button(label="Выйти из очереди", style=disnake.ButtonStyle.red, emoji="❌")
    async def leave_queue(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        global testing_queue
        if inter.author.id not in testing_queue:
            return await inter.response.send_message("⚠️ Вас нет в очереди!", ephemeral=True)

        testing_queue.remove(inter.author.id)
        await inter.response.send_message("❌ Вы вышли из очереди.", ephemeral=True)
        await update_queue_message()


class HighTestView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Пройти высокий тест", style=disnake.ButtonStyle.green, emoji="🧪")
    async def high_test(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        ALLOWED_ROLES = [
            1514930990353748158, 1514931889327308900, 1514932139911544883,
            1514932328865206282, 1514932499992678472, 1514932675688005712
        ]

        user_role_ids = [role.id for role in inter.author.roles]
        has_access = any(role_id in ALLOWED_ROLES for role_id in user_role_ids)

        if not has_access:
            return await inter.response.send_message("❌ Доступ заблокирован! Требуется ранг LT3+ для прохождения высоких тестов.", ephemeral=True)

        guild = inter.guild
        tester_role = guild.get_role(1505930643597430846)

        overwrites = {
            guild.default_role: disnake.PermissionOverwrite(read_messages=False),
            inter.author: disnake.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            tester_role: disnake.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True)
        }

        ticket_channel = await guild.create_text_channel(
            name=f"тест-{inter.author.name}",
            overwrites=overwrites
        )

        await inter.response.send_message(f"✅ Комната для вашего теста создана: {ticket_channel.mention}", ephemeral=True)

        embed = disnake.Embed(
            title="⚔️ ECL Tiers • Высокий Тест",
            description=f"Приветствуем, {inter.author.mention}!\n\nВы успешно открыли комнату тестирования.\nПожалуйста, ожидайте, тестеры <@&1505930643597430846> скоро подключатся сюда.",
            color=0x2ecc71
        )
        embed.set_footer(text="ECL Tiers")

        await ticket_channel.send(content=f"{inter.author.mention} <@&1505930643597430846>", embed=embed, view=TicketCloseView())


class ApplicationView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Стать модератором", style=disnake.ButtonStyle.blurple, emoji="💙")
    async def moderator(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(modal=ModModal())

    @disnake.ui.button(label="Стать тестером", style=disnake.ButtonStyle.green, emoji="🧪")
    async def tester(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(modal=TesterModal())


class ClanDecisionView(disnake.ui.View):
    def __init__(self, applicant_id: int):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id

    @disnake.ui.button(label="Принять", style=disnake.ButtonStyle.green, emoji="✅")
    async def accept_applicant(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.defer()
        try:
            member = await inter.guild.fetch_member(self.applicant_id)
            await member.send(f"🎉 **Поздравляем!** Ваша заявка в клан **EclipseClan** была успешно **принята** администратором {inter.author.mention}!")
            ls_status = "✅ Сообщение в ЛС отправлено."
        except Exception:
            ls_status = "❌ Не удалось написать в ЛС (закрыт профиль)."

        embed = inter.message.embeds[0]
        embed.color = 0x2ecc71
        embed.title = "🌙 Заявка в клан • EclipseClan [ПРИНЯТА]"
        embed.add_field(name="📢 Вердикт", value=f"Принят администратором {inter.author.mention}\n*{ls_status}*", inline=False)

        for child in self.children:
            child.disabled = True
        await inter.message.edit(embed=embed, view=self)

    @disnake.ui.button(label="Отклонить", style=disnake.ButtonStyle.danger, emoji="❌")
    async def reject_applicant(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.defer()
        try:
            member = await inter.guild.fetch_member(self.applicant_id)
            await member.send(f"😔 К сожалению, ваша заявка в клан **EclipseClan** была **отклонена** администрацией.")
            ls_status = "✅ Сообщение в ЛС отправлено."
        except Exception:
            ls_status = "❌ Не удалось написать в ЛС (закрыт профиль)."

        embed = inter.message.embeds[0]
        embed.color = 0xe74c3c
        embed.title = "🌙 Заявка в клан • EclipseClan [ОТКЛОНЕНА]"
        embed.add_field(name="📢 Вердикт", value=f"Отклонен администратором {inter.author.mention}\n*{ls_status}*", inline=False)

        for child in self.children:
            child.disabled = True
        await inter.message.edit(embed=embed, view=self)


class ClanApplicationView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Подать заявку в клан", style=disnake.ButtonStyle.secondary, emoji="🌙")
    async def join_clan(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(modal=EclipseClanModal())

# ==============================================================================
# Modals
# ==============================================================================
class ModModal(disnake.ui.Modal):
    def __init__(self):
        components = [
            disnake.ui.TextInput(
                label="Ваш возраст",
                placeholder="Например: 14",
                custom_id="mod_age",
                style=disnake.TextInputStyle.short,
                max_length=3
            ),
            disnake.ui.TextInput(
                label="Сколько времени готовы уделять серверу?",
                placeholder="Например: 2-4 часа в день",
                custom_id="mod_time",
                style=disnake.TextInputStyle.short,
                max_length=50
            ),
            disnake.ui.TextInput(
                label="Почему именно вы должны стать модератором?",
                placeholder="Расскажите немного о себе и своем опыте...",
                custom_id="mod_about",
                style=disnake.TextInputStyle.paragraph,
                max_length=500
            ),
        ]
        super().__init__(title="Анкета на должность Модератора", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.send_message("✅ Ваша анкета модератора успешно отправлена администрации!", ephemeral=True)

        age = inter.text_values["mod_age"]
        time = inter.text_values["mod_time"]
        about = inter.text_values["mod_about"]

        channel = bot.get_channel(ADMIN_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(title="🛡️ Новая анкета: Модератор", color=0x3498db)
            embed.set_thumbnail(url=inter.author.display_avatar.url)
            embed.add_field(name="Отправитель", value=f"{inter.author.mention} (`{inter.author.name}`)", inline=False)
            embed.add_field(name="Возраст", value=f"`{age}`", inline=True)
            embed.add_field(name="Время", value=f"`{time}`", inline=True)
            embed.add_field(name="О себе / Почему он", value=f"```\n{about}\n```", inline=False)
            embed.set_footer(text=f"ID пользователя: {inter.author.id}")

            await channel.send(embed=embed, view=StaffDecisionView(applicant_id=inter.author.id, role_name="Модератор"))


class TesterModal(disnake.ui.Modal):
    def __init__(self):
        components = [
            disnake.ui.TextInput(
                label="Ваш ник в Minecraft и текущий ранг",
                placeholder="Например: qw1zzy9 | HT3",
                custom_id="test_nick",
                style=disnake.TextInputStyle.short,
                max_length=50
            ),
            disnake.ui.TextInput(
                label="Был ли опыт в тестировании игроков?",
                placeholder="Да/Нет (если да, то где)",
                custom_id="test_exp",
                style=disnake.TextInputStyle.short,
                max_length=100
            ),
            disnake.ui.TextInput(
                label="Оцените ваше знание ПВП механик (0/10)",
                placeholder="Например: 9/10, отлично знаю тиры",
                custom_id="test_skills",
                style=disnake.TextInputStyle.short,
                max_length=20
            ),
        ]
        super().__init__(title="Анкета на должность Тестера", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.send_message("✅ Ваша анкета тестера успешно отправлена администрации!", ephemeral=True)

        nick = inter.text_values["test_nick"]
        exp = inter.text_values["test_exp"]
        skills = inter.text_values["test_skills"]

        channel = bot.get_channel(ADMIN_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(title="🧪 Новая анкета: Тестер", color=0x2ecc71)
            embed.set_thumbnail(url=inter.author.display_avatar.url)
            embed.add_field(name="Отправитель", value=f"{inter.author.mention} (`{inter.author.name}`)", inline=False)
            embed.add_field(name="Ник и ранг", value=f"`{nick}`", inline=False)
            embed.add_field(name="Опыт", value=f"`{exp}`", inline=False)
            embed.add_field(name="Знание ПВП механик", value=f"`{skills}`", inline=False)
            embed.set_footer(text=f"ID пользователя: {inter.author.id}")

            await channel.send(embed=embed, view=StaffDecisionView(applicant_id=inter.author.id, role_name="Тестер"))


class EclipseClanModal(disnake.ui.Modal):
    def __init__(self):
        components = [
            disnake.ui.TextInput(
                label="Ник, обращение и возраст (11+)",
                placeholder="Пример: qw1zzy9 (квизи), 13 лет",
                custom_id="clan_base",
                style=disnake.TextInputStyle.short,
                max_length=100
            ),
            disnake.ui.TextInput(
                label="Активность и наличие микрофона",
                placeholder="Пример: Играю каждый день по 3 часа. Микрофон есть.",
                custom_id="clan_activity",
                style=disnake.TextInputStyle.short,
                max_length=100
            ),
            disnake.ui.TextInput(
                label="Умение строить и навыки ПвП (мин. 2)",
                placeholder="Пример: Строить умею 8/10. Хорош в ПвП: бисты, мейс, опка.",
                custom_id="clan_skills",
                style=disnake.TextInputStyle.paragraph,
                max_length=200
            ),
            disnake.ui.TextInput(
                label="Адекватность (готовы не токсичить?)",
                placeholder="Пример: Да, полностью адекватен, правила соблюдаю.",
                custom_id="clan_rules",
                style=disnake.TextInputStyle.short,
                max_length=100
            ),
            disnake.ui.TextInput(
                label="О себе и почему именно Мы?",
                placeholder="Расскажи пару слов о себе и почему выбрал EclipseClan...",
                custom_id="clan_about",
                style=disnake.TextInputStyle.paragraph,
                max_length=500
            ),
        ]
        super().__init__(title="Заявка в EclipseClan", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.send_message("✅ Ваша заявка в EclipseClan успешно отправлена! Ожидайте вердикта в ЛС.", ephemeral=True)

        base = inter.text_values["clan_base"]
        activity = inter.text_values["clan_activity"]
        skills = inter.text_values["clan_skills"]
        rules = inter.text_values["clan_rules"]
        about = inter.text_values["clan_about"]

        channel = bot.get_channel(CLAN_ADMIN_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(title="🌙 Новая заявка в клан • EclipseClan", color=0x7b2fbe)
            embed.set_thumbnail(url=inter.author.display_avatar.url)

            embed.add_field(name="👤 Отправитель", value=f"{inter.author.mention} (`{inter.author.name}`)", inline=False)
            embed.add_field(name="1) Ник / Обращение и 2) Возраст", value=f"```text\n{base}\n```", inline=False)
            embed.add_field(name="3) Активность и 6) Микрофон", value=f"```text\n{activity}\n```", inline=False)
            embed.add_field(name="4) Строительство и 5) Навыки ПвП", value=f"```text\n{skills}\n```", inline=False)
            embed.add_field(name="7) Адекватность / Правила", value=f"```text\n{rules}\n```", inline=False)
            embed.add_field(name="8) О себе и почему EclipseClan", value=f"```text\n{about}\n```", inline=False)

            embed.set_footer(text=f"ID: {inter.author.id}")

            await channel.send(embed=embed, view=ClanDecisionView(applicant_id=inter.author.id))

# ==============================================================================
# Bot Slash Commands
# ==============================================================================
@bot.slash_command(name="меню_заявок", description="Отправить меню заявок в команду")
@commands.has_permissions(administrator=True)
async def slash_меню_заявок(inter: disnake.ApplicationCommandInteraction):
    embed = disnake.Embed(
        title="📝 Заявки в команду",
        description="**Выберите роль**\n\n🛡️ **Модератор** 🧪 **Тестер**\nУправление сервером       Тестирование",
        color=0x2b2d31
    )
    await inter.response.send_message(embed=embed, view=ApplicationView())

@bot.slash_command(name="меню_клана", description="Отправить меню заявок в клан EclipseClan")
@commands.has_permissions(administrator=True)
async def slash_меню_клана(inter: disnake.ApplicationCommandInteraction):
    embed = disnake.Embed(
        title="🌙 EclipseClan • Набор в клан",
        description="Хочешь стать частью нашей команды? Нажимай на кнопку ниже и заполняй анкету!\n\n┃ **📌 Критерии для вступления:**\n┃ **Возраст** — 11+\n┃ **Онлайн** — Хорошая активность\n┃ **Микрофон** — Обязательно\n┃ **Навыки** — Строительство + 2+ вида ПвП\n┃ **Адекватность** — Полная\n",
        color=0x7b2fbe
    )
    embed.set_footer(text="EclipseClan")
    await inter.response.send_message(embed=embed, view=ClanApplicationView())

@bot.slash_command(name="меню_высоких_тестов", description="Отправить меню высоких тестов (LT3+)")
@is_admin_or_tester()
async def slash_меню_высоких_тестов(inter: disnake.ApplicationCommandInteraction):
    embed = disnake.Embed(
        title="🧪 Высокие тесты",
        description="Требуется ранг **LT3+**\n\nНажмите на кнопку ниже, чтобы открыть приватный тикет для прохождения высокого теста.",
        color=0xffd700
    )
    embed.set_footer(text="ECL Tiers")
    await inter.response.send_message(embed=embed, view=HighTestView())

@bot.slash_command(name="меню_очереди", description="Отправить меню очереди на тестирование")
@is_admin_or_tester()
async def slash_меню_очереди(inter: disnake.ApplicationCommandInteraction):
    global queue_message
    embed = disnake.Embed(title="🏆 ECL Tiers", color=0x1e90ff)
    embed.add_field(name="┃ Очередь на тестирование", value=" ", inline=False)
    embed.add_field(name="📊 Статус", value=f"```diff\n- {queue_status}\n```", inline=True)
    embed.add_field(name="👥 В очереди", value=f"{len(testing_queue)} чел.", inline=True)
    embed.add_field(name="🧪 Тестеров", value="1", inline=True)
    embed.add_field(name="📋 Очередь", value=f"```\nпусто\n```", inline=False)
    embed.set_footer(text="ECL Tiers")
    await inter.response.send_message(embed=embed, view=QueueView())
    queue_message = await inter.original_response()

@bot.slash_command(name="управление_очередью", description="Управление очередью на тестирование")
@is_admin_or_tester()
async def slash_управление_очередью(
    inter: disnake.ApplicationCommandInteraction,
    action: str = disnake.Option("action", "Действие", choices=["открыть", "закрыть", "очистить"])
):
    global queue_status, testing_queue
    if action == "открыть":
        queue_status = "ОТКРЫТА"
        await inter.response.send_message("🔓 Очередь открыта!", ephemeral=True)
    elif action == "закрыть":
        queue_status = "ЗАКРЫТА"
        await inter.response.send_message("🔒 Очередь закрыта!", ephemeral=True)
    elif action == "очистить":
        if testing_queue:
            testing_queue.pop(0)
            await inter.response.send_message("🧹 Первое место удалено из очереди!", ephemeral=True)
            if testing_queue:
                bot.loop.create_task(create_queue_ticket(inter.guild))
        else:
            await inter.response.send_message("⚠️ Очередь пуста!", ephemeral=True)

    await update_queue_message()

@bot.slash_command(name="очередь_пинг", description="Оповестить всех об открытой очереди")
@is_admin_or_tester()
async def slash_очередь_пинг(inter: disnake.ApplicationCommandInteraction):
    await inter.response.send_message("@everyone 📢 Очередь открыта! Вы можете встать в неё, нажав на кнопку «Встать в очередь✅»")

@bot.slash_command(name="результаты", description="Отправить результат тестирования игрока")
async def slash_результаты(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member = disnake.Option("member", "Игрок"),
    tester_nick: str = disnake.Option("tester_nick", "Ник тестера"),
    player_nick: str = disnake.Option("player_nick", "Ник игрока"),
    old_tier: str = disnake.Option("old_tier", "Предыдущий ранг"),
    new_tier: str = disnake.Option("new_tier", "Новый ранг"),
    score: str = disnake.Option("score", "Счет матча"),
    kit: str = disnake.Option("kit", "Кит / Набор")
):
    try:
        embed = disnake.Embed(
            title="🏆 Результат тестирования",
            description=f"Игрок: {member.mention} (**{player_nick}**)",
            color=0x1e90ff
        )
        embed.add_field(name="🧪 Тестер", value=f"`{tester_nick}`", inline=True)
        embed.add_field(name="🎮 Minecraft ник", value=f"`{player_nick}`", inline=True)
        embed.add_field(name="📉 Предыдущий ранг", value=f"`{old_tier}`", inline=True)
        embed.add_field(name="📈 Новый ранг", value=f"`{new_tier}`", inline=True)
        embed.add_field(name="⚔️ Счет матча", value=f"`{score}`", inline=True)
        embed.add_field(name="📦 Кит / Набор", value=f"`{kit}`", inline=True)

        embed.set_thumbnail(url=member.display_avatar.url)

        current_time = datetime.now().strftime("%d.%m.%Y %H:%M")
        embed.set_footer(text=f"ECL Tiers {current_time}")

        await inter.response.send_message(content=member.mention, embed=embed)

    except Exception as e:
        await inter.response.send_message(f"❌ Произошла ошибка при отправке результатов:\n```text\n{e}\n```")

@bot.slash_command(name="сервера", description="Показать список верифицированных серверов")
@is_admin_or_tester()
async def slash_сервера(inter: disnake.ApplicationCommandInteraction):
    embed = disnake.Embed(
        title="Верифицированные сервера",
        description="Ниже вы можете ознакомиться со списком верифицированных серверов.\n\n⠀",
        color=0x7b2fbe
    )

    servers_no_lic = (
        "┃ 🇷🇺 **HT-1** — HT-1.ru\n"
        "┃ 🇷🇺 **bworld** — bworld.pro\n"
        "┃ 🇷🇺 **pvpcult** — mc.pvpcult.ru\n"
        "┃ 🇩🇪 **RaidMine** — mc.raidmine.com\n"
        "┃ 🇷🇺 **tavix** — tavix.su\n"
        "┃ 🇷🇺 **astrummc** — astrummc.net\n"
        "┃ 🇷🇺 **aormio** — mc.aormio.ru / mc.aormio.net"
    )
    embed.add_field(name="Сервера без лицензии", value=servers_no_lic, inline=False)
    embed.add_field(name="⠀", value=" ", inline=False)

    servers_lic = (
        "┃ 🇩🇪 **CatPVP** — eu.catpvp.xyz\n"
        "┃ 🇩🇪 **MagicFFA** — magicffa.ru\n"
        "┃ 🇩🇪 **Minemen** — eu.minemen.club\n"
        "┃ 🇬🇧 **PVPClub** — eu.mcpvp.club\n"
        "┃ 🇩🇪 **Stray** — eu.stray.gg\n"
        "┃ 🇳🇱 **VexFFA** — vexaay.nl"
    )
    embed.add_field(name="Лицензионные сервера", value=servers_lic, inline=False)

    embed.set_image(url="https://cdn.discordapp.com/attachments/1498590027272159235/1514957729813364886/SpisokServerovECLtiers.png?ex=6a2d41d5&is=6a2bf055&hm=5e06d38939b3257c9f5e04aa9bcb55484f068a633342dbca95f07a1a958e3a54&")

    embed.set_footer(text="ECL Tiers")

    await inter.response.send_message(embed=embed)

@bot.slash_command(name="топ", description="Показать таблицу лидеров")
async def slash_топ(inter: disnake.ApplicationCommandInteraction):
    embed = generate_top_embed()
    await inter.response.send_message(embed=embed)
    new_msg = await inter.original_response()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO config (key, channel_id, message_id)
        VALUES ('last_top_msg', ?, ?)
        ON CONFLICT(key) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id
    """, (inter.channel.id, new_msg.id))
    conn.commit()
    conn.close()

@bot.slash_command(name="топ_игрок", description="Добавить или обновить игрока в таблице лидеров")
@commands.has_permissions(administrator=True)
async def slash_топ_игрок(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member = disnake.Option("member", "Игрок"),
    display_name: str = disnake.Option("display_name", "Отображаемое имя"),
    points: int = disnake.Option("points", "Очки"),
    kits: str = disnake.Option("kits", "Киты")
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO leaderboard (user_id, name, points, kits)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            name=excluded.name,
            points=excluded.points,
            kits=excluded.kits
    """, (member.id, display_name, points, kits))
    conn.commit()
    conn.close()

    refreshed = await auto_refresh_top(inter.bot)
    status = "Топ обновлен прямо в чате!" if refreshed else "Сохранено. Напишите /топ для создания карточки."
    await inter.response.send_message(f"✅ Данные игрока **{display_name}** изменены. {status}", ephemeral=True)

@bot.slash_command(name="топ_удалить", description="Удалить игрока из таблицы лидеров")
@commands.has_permissions(administrator=True)
async def slash_топ_удалить(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member = disnake.Option("member", "Игрок")
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM leaderboard WHERE user_id = ?", (member.id,))
    conn.commit()
    conn.close()

    refreshed = await auto_refresh_top(inter.bot)
    status = "Топ обновлен прямо в чате!" if refreshed else "Напишите /топ для создания карточки."
    await inter.response.send_message(f"🧹 Игрок удален. {status}", ephemeral=True)

# ==============================================================================
# Events
# ==============================================================================
@bot.event
async def on_ready():
    print(f"====================================")
    print(f"Бот {bot.user} успешно запущен!")
    print(f"Slash-команды: {len(bot.pending_application_commands)} зарегистрировано")
    print(f"Все системы ECL Bot работают стабильно.")
    print(f"====================================")

# ==============================================================================
# Health check server for Railway
# ==============================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

# ==============================================================================
# Run
# ==============================================================================
if __name__ == "__main__":
    thread = threading.Thread(target=run_health_server, daemon=True)
    thread.start()

    if not BOT_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable not set!")
    bot.run(BOT_TOKEN)
