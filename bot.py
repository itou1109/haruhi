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
GUILD_ID = os.getenv("GUILD_ID")  # 任意。開発中に即時反映させたい場合はサーバーIDを設定

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "todos.db")

JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc

NO_MENTIONS = discord.AllowedMentions.none()
EVERYONE_ONLY = discord.AllowedMentions(everyone=True, roles=False, users=False)

MAX_PENDING_LINES = 15
MAX_DONE_LINES = 5
REMINDER_STALE_AFTER = timedelta(hours=24)  # これより古い未送信リマインドは送らず「処理済み」にする

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
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # 既存インストールとの互換性のため、まず元のスキーマでテーブルを作成する
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

        # 既存データを消さずに新しい列だけ追加する（自動マイグレーション）
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()}
        if "deadline_utc" not in existing_cols:
            conn.execute("ALTER TABLE todos ADD COLUMN deadline_utc TEXT")
        if "notify_type" not in existing_cols:
            conn.execute("ALTER TABLE todos ADD COLUMN notify_type TEXT NOT NULL DEFAULT 'none'")

        # 固定Todoボードのメッセージ情報を保存するテーブル
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS boards (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                message_id INTEGER
            )
            """
        )

        # 送信済みリマインドを記録するテーブル（再起動しても二重送信しないため）
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
        conn.commit()


# ============================================================
# 日時パース（JST / 自然な入力を受け付ける）
# ============================================================

_RELATIVE_DAY = {"今日": 0, "明日": 1, "明後日": 2}


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def parse_deadline(text: str, reference: datetime) -> Optional[datetime]:
    """ユーザー入力の日時文字列をJST awareなdatetimeに変換する。認識できなければNone。"""
    text = unicodedata.normalize("NFKC", text).strip().replace("　", " ")
    ref_jst = reference.astimezone(JST)

    # 例: 明日18時 / 明日18時30分 / 今日9:00
    m = re.match(r"^(今日|明日|明後日)\s*(\d{1,2})時(?:\s*(\d{1,2})分)?$", text)
    if not m:
        m = re.match(r"^(今日|明日|明後日)\s*(\d{1,2}):(\d{2})$", text)
    if m:
        day_word, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        target_date = (ref_jst + timedelta(days=_RELATIVE_DAY[day_word])).date()
        try:
            return datetime(target_date.year, target_date.month, target_date.day, hh, mm, tzinfo=JST)
        except ValueError:
            return None

    # 例: 9/30 23:59 （年省略、過ぎていれば翌年に繰り上げ）
    m = re.match(r"^(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})$", text)
    if m:
        month, day, hh, mm = map(int, m.groups())
        year = ref_jst.year
        try:
            dt = datetime(year, month, day, hh, mm, tzinfo=JST)
        except ValueError:
            return None
        if dt < ref_jst - timedelta(minutes=1):
            try:
                dt = dt.replace(year=year + 1)
            except ValueError:
                return None
        return dt

    # 例: 9/30 （時刻省略時は23:59とみなす）
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", text)
    if m:
        month, day = map(int, m.groups())
        year = ref_jst.year
        try:
            dt = datetime(year, month, day, 23, 59, tzinfo=JST)
        except ValueError:
            return None
        if dt < ref_jst - timedelta(minutes=1):
            try:
                dt = dt.replace(year=year + 1)
            except ValueError:
                return None
        return dt

    # 例: 2026-09-30 18:00 / 2026/09/30 18:00
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})$", text)
    if m:
        year, month, day, hh, mm = map(int, m.groups())
        try:
            return datetime(year, month, day, hh, mm, tzinfo=JST)
        except ValueError:
            return None

    # 例: 2026-09-30 （時刻省略時は23:59とみなす）
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", text)
    if m:
        year, month, day = map(int, m.groups())
        try:
            return datetime(year, month, day, 23, 59, tzinfo=JST)
        except ValueError:
            return None

    return None


def format_jst(dt_utc: datetime) -> str:
    dt = dt_utc.astimezone(JST)
    return f"{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}"


def get_deadline_dt(row: sqlite3.Row) -> Optional[datetime]:
    if row["deadline_utc"]:
        return datetime.fromisoformat(row["deadline_utc"])
    return None


# ============================================================
# DBアクセス系ヘルパー
# ============================================================

def get_channel_todos(channel_id: int, include_done: bool = True):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM todos WHERE channel_id = ?"
        params = [channel_id]
        if not include_done:
            query += " AND done = 0"
        query += " ORDER BY id ASC"
        return conn.execute(query, params).fetchall()


def get_board_row(channel_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM boards WHERE channel_id = ?", (channel_id,)).fetchone()


def upsert_board_row(channel_id: int, guild_id: int, message_id: Optional[int]):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO boards (channel_id, guild_id, message_id) VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET message_id = excluded.message_id, guild_id = excluded.guild_id
            """,
            (channel_id, guild_id, message_id),
        )
        conn.commit()


def delete_reminder_log(todo_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM reminder_log WHERE todo_id = ?", (todo_id,))
        conn.commit()


# ============================================================
# 固定Todoボード
# ============================================================

def build_board_embed(rows, now: datetime) -> discord.Embed:
    idx_rows = list(enumerate(rows, start=1))
    pending = [(i, r) for i, r in idx_rows if not r["done"]]
    done = [(i, r) for i, r in idx_rows if r["done"]]

    def urgency_rank(item):
        i, r = item
        deadline = get_deadline_dt(r)
        if deadline is None:
            return (3, datetime.max.replace(tzinfo=UTC))
        if r["notify_type"] == "scheduled":
            rank = 0 if deadline <= now else 2
        else:
            if deadline <= now:
                rank = 0
            elif deadline - now <= timedelta(hours=24):
                rank = 1
            else:
                rank = 2
        return (rank, deadline)

    pending_sorted = sorted(pending, key=urgency_rank)

    lines = []
    shown_pending = pending_sorted[:MAX_PENDING_LINES]
    hidden_pending = len(pending_sorted) - len(shown_pending)
    if not pending_sorted:
        lines.append("（未完了のタスクはありません）")
    for i, r in shown_pending:
        lines.append(format_pending_line(i, r, now))
    if hidden_pending > 0:
        lines.append(f"…ほか{hidden_pending}件")

    embed = discord.Embed(title="📋 Todoボード", description="\n".join(lines), color=discord.Color.blurple())

    if done:
        shown_done = done[-MAX_DONE_LINES:]
        hidden_done = len(done) - len(shown_done)
        done_lines = [f"~~{i}. {r['task']}~~" for i, r in shown_done]
        if hidden_done > 0:
            done_lines.append(f"…ほか{hidden_done}件")
        embed.add_field(name="✅ 完了済み", value="\n".join(done_lines), inline=False)

    updated_jst = now.astimezone(JST)
    embed.set_footer(
        text=f"最終更新: {updated_jst.strftime('%Y/%m/%d %H:%M')} (JST) ／ /todo done, /todo remove の番号と対応"
    )
    return embed


def format_pending_line(i: int, r: sqlite3.Row, now: datetime) -> str:
    deadline = get_deadline_dt(r)
    if deadline is None:
        return f"⬜ **{i}.** {r['task']}"

    text = format_jst(deadline)
    if r["notify_type"] == "scheduled":
        icon = "🔴" if deadline <= now else "🕐"
        return f"{icon} **{i}.** {r['task']}　{text} に通知"
    else:
        if deadline <= now:
            icon = "🔴"
        elif deadline - now <= timedelta(hours=24):
            icon = "🟡"
        else:
            icon = "⬜"
        return f"{icon} **{i}.** {r['task']}　締切: {text}"


async def _resolve_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            return None
    return channel


async def _write_board_message(channel_id: int, guild_id: int, board_row, force_new: bool):
    channel = await _resolve_channel(channel_id)
    if channel is None:
        return

    rows = get_channel_todos(channel_id)
    embed = build_board_embed(rows, now_utc())

    message_id = board_row["message_id"] if board_row else None

    if force_new and message_id:
        try:
            old_msg = await channel.fetch_message(message_id)
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        message_id = None

    if message_id and not force_new:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=embed, allowed_mentions=NO_MENTIONS)
            return
        except discord.NotFound:
            message_id = None  # ボードが削除されていた → 新規作成へフォールバック
        except discord.Forbidden:
            return

    try:
        msg = await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        upsert_board_row(channel_id, guild_id, msg.id)
    except discord.Forbidden:
        pass


async def refresh_board_if_configured(channel_id: int, guild_id: int):
    """通常のTodo操作後に呼ぶ。/todo setup 済みのチャンネルだけ更新する。"""
    board_row = get_board_row(channel_id)
    if board_row is None:
        return
    try:
        await _write_board_message(channel_id, guild_id, board_row, force_new=False)
    except Exception as e:  # noqa: BLE001 - ボード更新の失敗でコマンド応答自体は止めない
        print(f"[board] 更新中にエラー: {e}")


async def setup_board(channel_id: int, guild_id: int):
    """/todo setup 用。既存メッセージを削除して新規に作り直す。"""
    board_row = get_board_row(channel_id)
    if board_row is None:
        upsert_board_row(channel_id, guild_id, None)
        board_row = get_board_row(channel_id)
    await _write_board_message(channel_id, guild_id, board_row, force_new=True)


# ============================================================
# Bot 本体
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

todo_group = app_commands.Group(name="todo", description="このチャンネルのTodoリストを管理します")


async def reply(interaction: discord.Interaction, text: str, ephemeral: bool = False):
    if interaction.response.is_done():
        await interaction.followup.send(text, allowed_mentions=NO_MENTIONS, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(text, allowed_mentions=NO_MENTIONS, ephemeral=ephemeral)


def validate_number(rows, number: int) -> Optional[sqlite3.Row]:
    if number < 1 or number > len(rows):
        return None
    return rows[number - 1]


# ---------------- /todo setup ----------------

@todo_group.command(name="setup", description="このチャンネルに固定Todoボードを作成（または再作成）します")
@app_commands.guild_only()
async def todo_setup(interaction: discord.Interaction):
    await interaction.response.defer()
    await setup_board(interaction.channel_id, interaction.guild_id)
    await reply(interaction, "📋 このチャンネルにTodoボードを作成しました。以後、追加・完了・削除のたびに自動更新されます。")


# ---------------- /todo add ----------------

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
            await reply(
                interaction,
                "⚠️ 期限(deadline)が未指定のため通知は設定できません。\n"
                "例: `/todo add task:レポート提出 deadline:9/30 23:59 notify:deadline`",
                ephemeral=True,
            )
            return
    else:
        deadline_dt = parse_deadline(deadline, now_utc())
        if deadline_dt is None:
            await reply(interaction, f"⚠️ {DEADLINE_EXAMPLE_TEXT}", ephemeral=True)
            return
        notify_value = notify.value if notify else "scheduled"

    await interaction.response.defer()

    deadline_utc_iso = deadline_dt.astimezone(UTC).isoformat() if deadline_dt else None

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO todos (guild_id, channel_id, author_id, task, done, created_at, deadline_utc, notify_type) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                task,
                now_utc().isoformat(),
                deadline_utc_iso,
                notify_value,
            ),
        )
        conn.commit()

    await refresh_board_if_configured(interaction.channel_id, interaction.guild_id)

    if deadline_dt:
        await reply(interaction, f"✅ タスクを追加しました: **{task}**（{format_jst(deadline_dt)}）")
    else:
        await reply(interaction, f"✅ タスクを追加しました: **{task}**")


# ---------------- /todo update ----------------

@todo_group.command(name="update", description="タスク名・期限・通知方式を変更します")
@app_commands.describe(
    number="/todo list またはボードに表示された番号",
    task="新しいタスク名（変更する場合のみ）",
    deadline="新しい期限。例: 明日18時 / 9/30 23:59 ／ 'none'で期限を削除",
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
        await reply(interaction, "⚠️ task / deadline / notify のいずれかを指定してください。", ephemeral=True)
        return

    rows = get_channel_todos(interaction.channel_id)
    target = validate_number(rows, number)
    if target is None:
        await reply(interaction, "その番号のタスクは見つかりませんでした。", ephemeral=True)
        return

    new_task = task if task is not None else target["task"]

    clear_deadline = False
    new_deadline_dt = None
    if deadline is not None:
        if deadline.strip().lower() in CLEAR_KEYWORDS:
            clear_deadline = True
        else:
            new_deadline_dt = parse_deadline(deadline, now_utc())
            if new_deadline_dt is None:
                await reply(interaction, f"⚠️ {DEADLINE_EXAMPLE_TEXT}", ephemeral=True)
                return
    else:
        existing = get_deadline_dt(target)
        if existing is not None:
            new_deadline_dt = existing

    final_has_deadline = (not clear_deadline) and (new_deadline_dt is not None)
    new_notify = notify.value if notify is not None else (target["notify_type"] or "none")
    if clear_deadline:
        new_notify = "none"
    if not final_has_deadline and new_notify != "none":
        await reply(interaction, "⚠️ 期限が設定されていないため、通知(notify)は指定できません。", ephemeral=True)
        return

    await interaction.response.defer()

    deadline_utc_iso = new_deadline_dt.astimezone(UTC).isoformat() if final_has_deadline else None

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE todos SET task = ?, deadline_utc = ?, notify_type = ? WHERE id = ?",
            (new_task, deadline_utc_iso, new_notify, target["id"]),
        )
        conn.commit()

    # スケジュールが変わったので、送信済みリマインド履歴はリセットする
    delete_reminder_log(target["id"])

    await refresh_board_if_configured(interaction.channel_id, interaction.guild_id)
    await reply(interaction, f"✏️ タスクを更新しました: **{new_task}**")


# ---------------- /todo list ----------------

@todo_group.command(name="list", description="このチャンネルのTodo一覧を表示します（ボードを見失った場合の補助用）")
@app_commands.guild_only()
async def todo_list(interaction: discord.Interaction):
    rows = get_channel_todos(interaction.channel_id)
    if not rows:
        await reply(
            interaction,
            "このチャンネルにはまだTodoがありません。`/todo add` で追加してください。",
            ephemeral=True,
        )
        return

    embed = build_board_embed(rows, now_utc())
    embed.title = "📋 Todoリスト（一時表示）"
    await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS, ephemeral=True)


# ---------------- /todo done ----------------

@todo_group.command(name="done", description="指定した番号のTodoを完了にします")
@app_commands.describe(number="ボードまたは /todo list に表示された番号")
@app_commands.guild_only()
async def todo_done(interaction: discord.Interaction, number: int):
    rows = get_channel_todos(interaction.channel_id)
    target = validate_number(rows, number)
    if target is None:
        await reply(interaction, "その番号のタスクは見つかりませんでした。", ephemeral=True)
        return

    await interaction.response.defer()

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE todos SET done = 1 WHERE id = ?", (target["id"],))
        conn.commit()

    await refresh_board_if_configured(interaction.channel_id, interaction.guild_id)
    await reply(interaction, f"🎉 完了にしました: **{target['task']}**")


# ---------------- /todo remove ----------------

@todo_group.command(name="remove", description="指定した番号のTodoを削除します")
@app_commands.describe(number="ボードまたは /todo list に表示された番号")
@app_commands.guild_only()
async def todo_remove(interaction: discord.Interaction, number: int):
    rows = get_channel_todos(interaction.channel_id)
    target = validate_number(rows, number)
    if target is None:
        await reply(interaction, "その番号のタスクは見つかりませんでした。", ephemeral=True)
        return

    await interaction.response.defer()

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (target["id"],))
        conn.commit()
    delete_reminder_log(target["id"])

    await refresh_board_if_configured(interaction.channel_id, interaction.guild_id)
    await reply(interaction, f"🗑️ 削除しました: **{target['task']}**")


# ---------------- /todo clear ----------------

@todo_group.command(name="clear", description="このチャンネルの完了済みTodoを一括削除します")
@app_commands.guild_only()
async def todo_clear(interaction: discord.Interaction):
    await interaction.response.defer()

    with closing(sqlite3.connect(DB_PATH)) as conn:
        done_ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM todos WHERE channel_id = ? AND done = 1", (interaction.channel_id,)
            ).fetchall()
        ]
        if done_ids:
            conn.executemany("DELETE FROM reminder_log WHERE todo_id = ?", [(i,) for i in done_ids])
        cur = conn.execute(
            "DELETE FROM todos WHERE channel_id = ? AND done = 1", (interaction.channel_id,)
        )
        conn.commit()
        deleted = cur.rowcount

    await refresh_board_if_configured(interaction.channel_id, interaction.guild_id)
    await reply(interaction, f"🧹 完了済みのタスクを {deleted} 件削除しました。")


bot.tree.add_command(todo_group)


# ============================================================
# 締切リマインド（バックグラウンドループ）
# ============================================================

@tasks.loop(seconds=60)
async def reminder_loop():
    try:
        await check_and_send_reminders()
    except Exception as e:  # noqa: BLE001 - ループ自体は絶対に止めない
        print(f"[reminder_loop] 予期しないエラー: {e}")


async def check_and_send_reminders():
    now = now_utc()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM todos WHERE done = 0 AND deadline_utc IS NOT NULL AND notify_type != 'none'"
        ).fetchall()

    for row in rows:
        try:
            await _process_todo_reminder(row, now)
        except Exception as e:  # noqa: BLE001 - 1件の失敗で他のリマインドを止めない
            print(f"[reminder] todo_id={row['id']} の処理でエラー: {e}")


async def _process_todo_reminder(row: sqlite3.Row, now: datetime):
    todo_id = row["id"]
    deadline = get_deadline_dt(row)
    notify_type = row["notify_type"]

    if notify_type == "scheduled":
        milestones = [("at", deadline)]
    elif notify_type == "deadline":
        milestones = [
            ("d3", deadline - timedelta(days=3)),
            ("d1", deadline - timedelta(days=1)),
            ("h3", deadline - timedelta(hours=3)),
        ]
    else:
        return

    with closing(sqlite3.connect(DB_PATH)) as conn:
        sent = {r[0] for r in conn.execute(
            "SELECT milestone FROM reminder_log WHERE todo_id = ?", (todo_id,)
        ).fetchall()}

    due_unsent = sorted(
        ((name, t) for name, t in milestones if t <= now and name not in sent),
        key=lambda x: x[1],
    )
    if not due_unsent:
        return

    # 複数のマイルストーンが同時に溜まっていても、最新の1件だけ通知して過去分は連投しない
    latest_name, latest_time = due_unsent[-1]
    should_send = (now - latest_time) <= REMINDER_STALE_AFTER

    if should_send:
        channel = await _resolve_channel(row["channel_id"])
        if channel is not None:
            content = _build_reminder_content(row, latest_name, deadline)
            try:
                await channel.send(content, allowed_mentions=EVERYONE_ONLY)
            except Exception as e:  # noqa: BLE001 - 送信失敗時は既読にせず次回リトライ
                print(f"[reminder] 送信失敗 todo_id={todo_id}: {e}")
                return

    sent_at = now.isoformat()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO reminder_log (todo_id, milestone, sent_at) VALUES (?, ?, ?)",
            [(todo_id, name, sent_at) for name, _ in due_unsent],
        )
        conn.commit()


def _build_reminder_content(row: sqlite3.Row, milestone_name: str, deadline: datetime) -> str:
    task = row["task"]
    deadline_txt = format_jst(deadline)
    if row["notify_type"] == "scheduled":
        return f"@everyone ⏰ 予定の時間になりました: **{task}**（{deadline_txt}）"
    label = {"d3": "締切まであと3日", "d1": "締切まであと1日", "h3": "締切まであと3時間"}[milestone_name]
    return f"@everyone ⚠️ {label}: **{task}**（締切 {deadline_txt}）"


# ============================================================
# 起動処理
# ============================================================

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
