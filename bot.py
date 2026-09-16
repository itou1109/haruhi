import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 開発中だけ設定するとスラッシュコマンドが即時反映される

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "todos.db")

JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc

NO_MENTIONS = discord.AllowedMentions.none()
EVERYONE_ONLY = discord.AllowedMentions(everyone=True, roles=False, users=False)

MAX_PENDING_LINES = 15
MAX_DONE_LINES = 5
REMINDER_STALE_AFTER = timedelta(hours=24)

DEADLINE_EXAMPLE_TEXT = (
    "日時の形式を認識できませんでした。次のような形式で入力してください。\n"
    "・明日18時\n"
    "・9/30 23:59\n"
    "・2026-09-30 18:00"
)

CLEAR_KEYWORDS = {"none", "なし", "クリア", "削除"}

NOTIFY_CHOICES = [
    app_commands.Choice(name="scheduled: 指定時刻に1回だけ@everyone通知（既定）", value="scheduled"),
    app_commands.Choice(name="deadline: 締切の3日前/1日前/3時間前にリマインド", value="deadline"),
    app_commands.Choice(name="none: 通知しない（期限は表示のみ）", value="none"),
]


# ============================================================
# DB 初期化・マイグレーション
# ============================================================

def init_db():
    """既存のTodoを残したまま、必要なテーブル・列を用意する。"""
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

        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()}
        if "deadline_utc" not in existing_cols:
            conn.execute("ALTER TABLE todos ADD COLUMN deadline_utc TEXT")
        if "notify_type" not in existing_cols:
            conn.execute("ALTER TABLE todos ADD COLUMN notify_type TEXT NOT NULL DEFAULT 'none'")

        # guild_idを主キーにすることで、サーバーごとにボードを必ず1つにする。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todo_boards (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_log (
                todo_id INTEGER NOT NULL,
                milestone TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (todo_id, milestone)
            )
            """
        )

        # 旧バージョンのboardsテーブルがあれば、最初の1件を新しい形式へ引き継ぐ。
        legacy_board_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'boards'"
        ).fetchone()
        if legacy_board_table:
            legacy_rows = conn.execute(
                "SELECT guild_id, channel_id, message_id FROM boards ORDER BY rowid ASC"
            ).fetchall()
            for guild_id, channel_id, message_id in legacy_rows:
                conn.execute(
                    "INSERT OR IGNORE INTO todo_boards (guild_id, channel_id, message_id) VALUES (?, ?, ?)",
                    (guild_id, channel_id, message_id),
                )

        conn.commit()


# ============================================================
# 日時パース（JST）
# ============================================================

_RELATIVE_DAY = {"今日": 0, "明日": 1, "明後日": 2}


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def parse_deadline(text: str, reference: datetime) -> Optional[datetime]:
    """JSTで解釈した日時を返す。認識できない入力はNone。"""
    text = unicodedata.normalize("NFKC", text).strip().replace("　", " ")
    ref_jst = reference.astimezone(JST)

    # 明日18時 / 明日18時30分 / 今日9:00
    match = re.match(r"^(今日|明日|明後日)\s*(\d{1,2})時(?:\s*(\d{1,2})分)?$", text)
    if not match:
        match = re.match(r"^(今日|明日|明後日)\s*(\d{1,2}):(\d{2})$", text)
    if match:
        day_word, hour, minute = match.group(1), int(match.group(2)), int(match.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        target_date = (ref_jst + timedelta(days=_RELATIVE_DAY[day_word])).date()
        return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=JST)

    # 9/30 23:59（年省略。すでに過ぎていたら翌年）
    match = re.match(r"^(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})$", text)
    if match:
        month, day, hour, minute = map(int, match.groups())
        try:
            result = datetime(ref_jst.year, month, day, hour, minute, tzinfo=JST)
        except ValueError:
            return None
        if result < ref_jst - timedelta(minutes=1):
            try:
                result = result.replace(year=result.year + 1)
            except ValueError:
                return None
        return result

    # 9/30（時刻は23:59扱い）
    match = re.match(r"^(\d{1,2})/(\d{1,2})$", text)
    if match:
        month, day = map(int, match.groups())
        try:
            result = datetime(ref_jst.year, month, day, 23, 59, tzinfo=JST)
        except ValueError:
            return None
        if result < ref_jst - timedelta(minutes=1):
            try:
                result = result.replace(year=result.year + 1)
            except ValueError:
                return None
        return result

    # 2026-09-30 18:00 / 2026/09/30 18:00
    match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})$", text)
    if match:
        year, month, day, hour, minute = map(int, match.groups())
        try:
            return datetime(year, month, day, hour, minute, tzinfo=JST)
        except ValueError:
            return None

    # 2026-09-30（時刻は23:59扱い）
    match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", text)
    if match:
        year, month, day = map(int, match.groups())
        try:
            return datetime(year, month, day, 23, 59, tzinfo=JST)
        except ValueError:
            return None

    return None


def get_deadline_dt(row: sqlite3.Row) -> Optional[datetime]:
    value = row["deadline_utc"]
    if not value:
        return None
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def format_jst(dt_utc: datetime) -> str:
    dt = dt_utc.astimezone(JST)
    return f"{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}"


# ============================================================
# DBアクセス
# ============================================================

def get_guild_todos(guild_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM todos WHERE guild_id = ? ORDER BY id ASC", (guild_id,)
        ).fetchall()


def get_board_row(guild_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM todo_boards WHERE guild_id = ?", (guild_id,)).fetchone()


def save_board(guild_id: int, channel_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO todo_boards (guild_id, channel_id, message_id) VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id
            """,
            (guild_id, channel_id, message_id),
        )
        conn.commit()


def delete_reminder_log(todo_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM reminder_log WHERE todo_id = ?", (todo_id,))
        conn.commit()


# ============================================================
# Todoボード（guildに1つ）
# ============================================================

def format_pending_line(number: int, row: sqlite3.Row, now: datetime) -> str:
    deadline = get_deadline_dt(row)
    if deadline is None:
        return f"⬜ **{number}.** {row['task']}"

    if row["notify_type"] == "scheduled":
        icon = "🔴" if deadline <= now else "🕐"
        return f"{icon} **{number}.** {row['task']}　{format_jst(deadline)} に通知"

    if deadline <= now:
        icon = "🔴"
    elif deadline - now <= timedelta(hours=24):
        icon = "🟡"
    else:
        icon = "⬜"
    return f"{icon} **{number}.** {row['task']}　締切: {format_jst(deadline)}"


def build_board_embed(rows, now: datetime) -> discord.Embed:
    indexed_rows = list(enumerate(rows, start=1))
    pending = [(number, row) for number, row in indexed_rows if not row["done"]]
    done = [(number, row) for number, row in indexed_rows if row["done"]]

    def urgency(item):
        _, row = item
        deadline = get_deadline_dt(row)
        if deadline is None:
            return (3, datetime.max.replace(tzinfo=UTC))
        if row["notify_type"] == "scheduled":
            return (0 if deadline <= now else 2, deadline)
        if deadline <= now:
            return (0, deadline)
        if deadline - now <= timedelta(hours=24):
            return (1, deadline)
        return (2, deadline)

    pending.sort(key=urgency)
    lines = [format_pending_line(number, row, now) for number, row in pending[:MAX_PENDING_LINES]]
    if not lines:
        lines = ["（未完了のタスクはありません）"]
    if len(pending) > MAX_PENDING_LINES:
        lines.append(f"…ほか{len(pending) - MAX_PENDING_LINES}件")

    embed = discord.Embed(
        title="📋 Todoボード",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )

    if done:
        done_lines = [f"~~{number}. {row['task']}~~" for number, row in done[-MAX_DONE_LINES:]]
        if len(done) > MAX_DONE_LINES:
            done_lines.append(f"…ほか{len(done) - MAX_DONE_LINES}件")
        embed.add_field(name="✅ 完了済み", value="\n".join(done_lines), inline=False)

    updated = now.astimezone(JST).strftime("%Y/%m/%d %H:%M")
    embed.set_footer(text=f"最終更新: {updated} (JST) ／ /todo done・/todo remove の番号と対応")
    return embed


async def _resolve_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            return None
    return channel


async def _delete_board_message(board_row):
    if board_row is None or not board_row["message_id"]:
        return
    channel = await _resolve_channel(board_row["channel_id"])
    if channel is None:
        return
    try:
        message = await channel.fetch_message(board_row["message_id"])
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass


async def _create_board(guild_id: int, channel_id: int) -> Optional[discord.Message]:
    channel = await _resolve_channel(channel_id)
    if channel is None:
        return None
    try:
        message = await channel.send(
            embed=build_board_embed(get_guild_todos(guild_id), now_utc()),
            allowed_mentions=NO_MENTIONS,
        )
    except discord.Forbidden:
        return None
    save_board(guild_id, channel_id, message.id)
    return message


async def refresh_board_if_configured(guild_id: int):
    """登録済みの唯一のボードを編集する。削除済みなら同じチャンネルに作り直す。"""
    board_row = get_board_row(guild_id)
    if board_row is None:
        return
    channel = await _resolve_channel(board_row["channel_id"])
    if channel is None:
        return

    embed = build_board_embed(get_guild_todos(guild_id), now_utc())
    try:
        message = await channel.fetch_message(board_row["message_id"])
        await message.edit(embed=embed, allowed_mentions=NO_MENTIONS)
    except discord.NotFound:
        await _create_board(guild_id, board_row["channel_id"])
    except discord.Forbidden:
        print("[board] ボードを更新する権限がありません。")


# ============================================================
# Bot 本体・コマンド
# ============================================================

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
todo_group = app_commands.Group(name="todo", description="個人用Todoメモを管理します")
board_group = app_commands.Group(name="board", description="Todoボードを管理します")
todo_group.add_command(board_group)


async def reply(interaction: discord.Interaction, text: str, ephemeral: bool = True):
    if interaction.response.is_done():
        await interaction.followup.send(text, allowed_mentions=NO_MENTIONS, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(text, allowed_mentions=NO_MENTIONS, ephemeral=ephemeral)


def validate_number(rows, number: int) -> Optional[sqlite3.Row]:
    if number < 1 or number > len(rows):
        return None
    return rows[number - 1]


@todo_group.command(name="setup", description="このチャンネルに唯一のTodoボードを作成します")
@app_commands.guild_only()
async def todo_setup(interaction: discord.Interaction):
    board_row = get_board_row(interaction.guild_id)
    if board_row is not None:
        await reply(
            interaction,
            f"📋 Todoボードはすでに <#{board_row['channel_id']}> にあります。"
            "移動・作り直しは `/todo board reset` を使ってください。",
        )
        return

    await interaction.response.defer(ephemeral=True)
    message = await _create_board(interaction.guild_id, interaction.channel_id)
    if message is None:
        await reply(interaction, "⚠️ このチャンネルにTodoボードを送信できませんでした。Botの権限を確認してください。")
        return
    await reply(interaction, "📋 このチャンネルをTodoボードとして登録しました。以後どこから追加しても、ここだけが更新されます。")


@board_group.command(name="reset", description="このチャンネルへTodoボードを移動・作り直しします（Todoは消えません）")
@app_commands.guild_only()
async def todo_board_reset(interaction: discord.Interaction):
    old_board = get_board_row(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)

    # 新しいボード作成を先に試し、失敗した場合に古いボードを失わないようにする。
    message = await _create_board(interaction.guild_id, interaction.channel_id)
    if message is None:
        await reply(interaction, "⚠️ このチャンネルにTodoボードを送信できませんでした。古いボードはそのままです。")
        return
    if old_board is not None and old_board["message_id"] != message.id:
        await _delete_board_message(old_board)
    await reply(interaction, "📋 Todoボードをこのチャンネルへ移動・作り直ししました。Todoはそのまま残っています。")


@todo_group.command(name="add", description="Todoを追加します（期限・通知は任意）")
@app_commands.describe(
    task="追加するタスクの内容",
    deadline="任意。例: 明日18時 / 9/30 23:59 / 2026-09-30 18:00",
    notify="任意。期限を指定した場合のみ有効（既定: scheduled）",
)
@app_commands.choices(notify=NOTIFY_CHOICES)
@app_commands.guild_only()
async def todo_add(
    interaction: discord.Interaction,
    task: str,
    deadline: Optional[str] = None,
    notify: Optional[app_commands.Choice[str]] = None,
):
    deadline_dt = None
    notify_value = "none"
    if deadline is None:
        if notify is not None and notify.value != "none":
            await reply(interaction, "⚠️ 期限(deadline)が未指定のため通知は設定できません。", ephemeral=True)
            return
    else:
        deadline_dt = parse_deadline(deadline, now_utc())
        if deadline_dt is None:
            await reply(interaction, f"⚠️ {DEADLINE_EXAMPLE_TEXT}")
            return
        notify_value = notify.value if notify else "scheduled"

    await interaction.response.defer(ephemeral=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO todos (guild_id, channel_id, author_id, task, done, created_at, deadline_utc, notify_type)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                interaction.guild_id,
                interaction.channel_id,  # 履歴として保持するだけで、一覧の分類には使わない
                interaction.user.id,
                task,
                now_utc().isoformat(),
                deadline_dt.astimezone(UTC).isoformat() if deadline_dt else None,
                notify_value,
            ),
        )
        conn.commit()

    await refresh_board_if_configured(interaction.guild_id)
    suffix = f"（{format_jst(deadline_dt)}）" if deadline_dt else ""
    await reply(interaction, f"✅ タスクを追加しました: **{task}**{suffix}")


@todo_group.command(name="update", description="タスク名・期限・通知方式を変更します")
@app_commands.describe(
    number="Todoボードに表示された番号",
    task="新しいタスク名（変更する場合のみ）",
    deadline="新しい期限。'none'で期限を削除",
    notify="新しい通知方式（期限がある場合のみ指定可）",
)
@app_commands.choices(notify=NOTIFY_CHOICES)
@app_commands.guild_only()
async def todo_update(
    interaction: discord.Interaction,
    number: int,
    task: Optional[str] = None,
    deadline: Optional[str] = None,
    notify: Optional[app_commands.Choice[str]] = None,
):
    if task is None and deadline is None and notify is None:
        await reply(interaction, "⚠️ task / deadline / notify のいずれかを指定してください。")
        return

    rows = get_guild_todos(interaction.guild_id)
    target = validate_number(rows, number)
    if target is None:
        await reply(interaction, "その番号のタスクは見つかりませんでした。")
        return

    new_task = task if task is not None else target["task"]
    clear_deadline = deadline is not None and deadline.strip().lower() in CLEAR_KEYWORDS
    if deadline is None:
        new_deadline_dt = get_deadline_dt(target)
    elif clear_deadline:
        new_deadline_dt = None
    else:
        new_deadline_dt = parse_deadline(deadline, now_utc())
        if new_deadline_dt is None:
            await reply(interaction, f"⚠️ {DEADLINE_EXAMPLE_TEXT}")
            return

    new_notify = notify.value if notify is not None else (target["notify_type"] or "none")
    if clear_deadline:
        new_notify = "none"
    if new_deadline_dt is None and new_notify != "none":
        await reply(interaction, "⚠️ 期限が設定されていないため、通知(notify)は指定できません。")
        return

    await interaction.response.defer(ephemeral=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE todos SET task = ?, deadline_utc = ?, notify_type = ? WHERE id = ?",
            (new_task, new_deadline_dt.astimezone(UTC).isoformat() if new_deadline_dt else None, new_notify, target["id"]),
        )
        conn.commit()

    # 名前だけの更新では通知済み履歴を残し、期限または通知方式を変えた時だけリセットする。
    if deadline is not None or notify is not None:
        delete_reminder_log(target["id"])
    await refresh_board_if_configured(interaction.guild_id)
    await reply(interaction, f"✏️ タスクを更新しました: **{new_task}**")


@todo_group.command(name="list", description="サーバー全体のTodo一覧を一時表示します")
@app_commands.guild_only()
async def todo_list(interaction: discord.Interaction):
    rows = get_guild_todos(interaction.guild_id)
    if not rows:
        await reply(interaction, "Todoはまだありません。`/todo add` で追加してください。")
        return
    embed = build_board_embed(rows, now_utc())
    embed.title = "📋 Todoリスト（一時表示）"
    await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS, ephemeral=True)


@todo_group.command(name="done", description="指定した番号のTodoを完了にします")
@app_commands.describe(number="Todoボードに表示された番号")
@app_commands.guild_only()
async def todo_done(interaction: discord.Interaction, number: int):
    rows = get_guild_todos(interaction.guild_id)
    target = validate_number(rows, number)
    if target is None:
        await reply(interaction, "その番号のタスクは見つかりませんでした。")
        return

    await interaction.response.defer(ephemeral=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE todos SET done = 1 WHERE id = ?", (target["id"],))
        conn.commit()
    await refresh_board_if_configured(interaction.guild_id)
    await reply(interaction, f"🎉 完了にしました: **{target['task']}**")


@todo_group.command(name="remove", description="指定した番号のTodoを削除します")
@app_commands.describe(number="Todoボードに表示された番号")
@app_commands.guild_only()
async def todo_remove(interaction: discord.Interaction, number: int):
    rows = get_guild_todos(interaction.guild_id)
    target = validate_number(rows, number)
    if target is None:
        await reply(interaction, "その番号のタスクは見つかりませんでした。")
        return

    await interaction.response.defer(ephemeral=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (target["id"],))
        conn.execute("DELETE FROM reminder_log WHERE todo_id = ?", (target["id"],))
        conn.commit()
    await refresh_board_if_configured(interaction.guild_id)
    await reply(interaction, f"🗑️ 削除しました: **{target['task']}**")


@todo_group.command(name="clear", description="完了済みTodoを一括削除します")
@app_commands.guild_only()
async def todo_clear(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        done_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM todos WHERE guild_id = ? AND done = 1", (interaction.guild_id,)
            ).fetchall()
        ]
        if done_ids:
            conn.executemany("DELETE FROM reminder_log WHERE todo_id = ?", [(todo_id,) for todo_id in done_ids])
        deleted = conn.execute(
            "DELETE FROM todos WHERE guild_id = ? AND done = 1", (interaction.guild_id,)
        ).rowcount
        conn.commit()
    await refresh_board_if_configured(interaction.guild_id)
    await reply(interaction, f"🧹 完了済みのタスクを {deleted} 件削除しました。")


bot.tree.add_command(todo_group)


# ============================================================
# 締切リマインド
# ============================================================

@tasks.loop(seconds=60)
async def reminder_loop():
    try:
        await check_and_send_reminders()
    except Exception as exc:  # 1回の想定外エラーでループを停止させない
        print(f"[reminder_loop] 予期しないエラー: {exc}")


async def check_and_send_reminders():
    now = now_utc()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM todos WHERE done = 0 AND deadline_utc IS NOT NULL AND notify_type != 'none'"
        ).fetchall()
    for row in rows:
        try:
            await process_todo_reminder(row, now)
        except Exception as exc:  # 1件の失敗で他の通知を止めない
            print(f"[reminder] todo_id={row['id']} の処理でエラー: {exc}")


async def process_todo_reminder(row: sqlite3.Row, now: datetime):
    deadline = get_deadline_dt(row)
    if deadline is None:
        return
    if row["notify_type"] == "scheduled":
        milestones = [("at", deadline)]
    elif row["notify_type"] == "deadline":
        milestones = [
            ("d3", deadline - timedelta(days=3)),
            ("d1", deadline - timedelta(days=1)),
            ("h3", deadline - timedelta(hours=3)),
        ]
    else:
        return

    with closing(sqlite3.connect(DB_PATH)) as conn:
        sent = {item[0] for item in conn.execute(
            "SELECT milestone FROM reminder_log WHERE todo_id = ?", (row["id"],)
        ).fetchall()}
    due_unsent = sorted(
        ((name, time) for name, time in milestones if time <= now and name not in sent),
        key=lambda item: item[1],
    )
    if not due_unsent:
        return

    latest_name, latest_time = due_unsent[-1]
    if now - latest_time <= REMINDER_STALE_AFTER:
        board_row = get_board_row(row["guild_id"])
        if board_row is None:
            return  # ボード未設定なら、後で設定した時に通知できるよう未送信のまま残す
        channel = await _resolve_channel(board_row["channel_id"])
        if channel is None:
            return
        if row["notify_type"] == "scheduled":
            content = f"@everyone ⏰ 予定の時間になりました: **{row['task']}**（{format_jst(deadline)}）"
        else:
            label = {"d3": "締切まであと3日", "d1": "締切まであと1日", "h3": "締切まであと3時間"}[latest_name]
            content = f"@everyone ⚠️ {label}: **{row['task']}**（締切 {format_jst(deadline)}）"
        try:
            await channel.send(content, allowed_mentions=EVERYONE_ONLY)
        except discord.HTTPException as exc:
            print(f"[reminder] 送信失敗 todo_id={row['id']}: {exc}")
            return

    # 過去分を連投しないため、今回までに期限を迎えた分を一度に処理済みにする。
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO reminder_log (todo_id, milestone, sent_at) VALUES (?, ?, ?)",
            [(row["id"], name, now.isoformat()) for name, _ in due_unsent],
        )
        conn.commit()
    await refresh_board_if_configured(row["guild_id"])


@bot.event
async def on_ready():
    init_db()
    if not reminder_loop.is_running():
        reminder_loop.start()
    print(f"✅ ログインしました: {bot.user} (ID: {bot.user.id})")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。")
    init_db()
    bot.run(TOKEN)
