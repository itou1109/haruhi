import os
import sqlite3
import datetime
from contextlib import closing

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 任意。開発中に即時反映させたい場合はサーバーIDを設定

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "todos.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                task TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


intents = discord.Intents.default()


class TodoBot(commands.Bot):
    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = TodoBot(command_prefix="!", intents=intents)

todo_group = app_commands.Group(name="todo", description="このチャンネルのTodoリストを管理します")


def get_channel_todos(channel_id: int, include_done: bool = True):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM todos WHERE channel_id = ?"
        params = [channel_id]
        if not include_done:
            query += " AND done = 0"
        query += " ORDER BY id ASC"
        rows = conn.execute(query, params).fetchall()
        return rows


@todo_group.command(name="add", description="Todoを追加します")
@app_commands.describe(task="追加するタスクの内容")
async def todo_add(interaction: discord.Interaction, task: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO todos (guild_id, channel_id, author_id, task, done, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                task,
                datetime.datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    await interaction.response.send_message(f"✅ タスクを追加しました: **{task}**")


@todo_group.command(name="list", description="このチャンネルのTodo一覧を表示します")
async def todo_list(interaction: discord.Interaction):
    rows = get_channel_todos(interaction.channel_id)
    if not rows:
        await interaction.response.send_message(
            "このチャンネルにはまだTodoがありません。`/todo add` で追加してください。"
        )
        return

    embed = discord.Embed(title="📋 Todoリスト", color=discord.Color.blurple())
    lines = []
    for idx, row in enumerate(rows, start=1):
        checkbox = "✅" if row["done"] else "⬜"
        lines.append(f"{checkbox} **{idx}.** {row['task']}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="/todo done <番号> で完了 / /todo remove <番号> で削除")
    await interaction.response.send_message(embed=embed)


@todo_group.command(name="done", description="指定した番号のTodoを完了にします")
@app_commands.describe(number="/todo list に表示された番号")
async def todo_done(interaction: discord.Interaction, number: int):
    rows = get_channel_todos(interaction.channel_id)
    if number < 1 or number > len(rows):
        await interaction.response.send_message("その番号のタスクは見つかりませんでした。", ephemeral=True)
        return
    target = rows[number - 1]
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE todos SET done = 1 WHERE id = ?", (target["id"],))
        conn.commit()
    await interaction.response.send_message(f"🎉 完了にしました: **{target['task']}**")


@todo_group.command(name="remove", description="指定した番号のTodoを削除します")
@app_commands.describe(number="/todo list に表示された番号")
async def todo_remove(interaction: discord.Interaction, number: int):
    rows = get_channel_todos(interaction.channel_id)
    if number < 1 or number > len(rows):
        await interaction.response.send_message("その番号のタスクは見つかりませんでした。", ephemeral=True)
        return
    target = rows[number - 1]
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (target["id"],))
        conn.commit()
    await interaction.response.send_message(f"🗑️ 削除しました: **{target['task']}**")


@todo_group.command(name="clear", description="このチャンネルの完了済みTodoを一括削除します")
async def todo_clear(interaction: discord.Interaction):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "DELETE FROM todos WHERE channel_id = ? AND done = 1",
            (interaction.channel_id,),
        )
        conn.commit()
        deleted = cur.rowcount
    await interaction.response.send_message(f"🧹 完了済みのタスクを {deleted} 件削除しました。")


bot.tree.add_command(todo_group)


@bot.event
async def on_ready():
    init_db()
    print(f"✅ ログインしました: {bot.user} (ID: {bot.user.id})")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。")
    init_db()
    bot.run(TOKEN)
