# Discord Todo Bot

テキストチャンネル上で使えるTodo管理Discord Botです。
チャンネルに「固定Todoボード」を1つ設置し、追加・完了・削除のたびに自動で編集更新されるので、`/todo list` を毎回打たなくても常に最新状態が見えます。締切前の自動リマインド（`@everyone`）にも対応しています。
GitHub連携型のホスティングサービス（Railway等）にデプロイすることで、自分のPCを起動していなくても24時間動き続けます。

## できること

| コマンド | 内容 |
|---|---|
| `/todo setup` | このチャンネルに固定Todoボードを作成（または作り直し）する |
| `/todo add task:` | 期限なしのTodoを最短入力で追加する |
| `/todo add task: deadline:` | 期限付きTodoを追加する |
| `/todo add task: deadline: notify:deadline` | 締切前リマインド付きTodoを追加する |
| `/todo update number:` | タスク名・期限・通知方式を変更する |
| `/todo done number:` | 番号を指定して完了にする（ボードも即更新） |
| `/todo remove number:` | 番号を指定して削除する（ボードも即更新） |
| `/todo clear` | 完了済みタスクをまとめて削除する（ボードも即更新） |
| `/todo list` | ボードを見失った時の補助用。自分にだけ見える一覧を表示 |

タスクは **チャンネルごと** に保存されます（SQLiteデータベース `data/todos.db` に保存）。

### `/todo add` の使用例

```text
/todo add task:買い物
/todo add task:洗濯機回収 deadline:明日18時
/todo add task:レポート提出 deadline:9/30 23:59 notify:deadline
```

- `deadline`（任意）: 期限なしなら省略。次のような形式に対応
  - `明日18時` / `今日9:00` / `明後日20時30分`
  - `9/30 23:59` / `9/30`（時刻省略時は23:59扱い）
  - `2026-09-30 18:00` / `2026-09-30`
  - 日時はすべて **日本時間（JST）** として解釈されます。認識できない形式はエラーとして拒否され、保存されません。
- `notify`（任意。`deadline` 指定時のみ有効）
  - `scheduled`（既定）: 指定時刻に1回だけ `@everyone` 通知
  - `deadline`: 締切の **3日前・1日前・3時間前** に `@everyone` でリマインド（締切ちょうどの時刻には通知しません）
  - `none`: 期限は表示するが通知はしない

### Todoボードの見方

- 🔴 期限切れ　🟡 24時間以内に締切　⬜ 期限なし、または期限に余裕あり　🕐 `scheduled`通知待ち
- 完了済みタスクは下部に取り消し線でまとめて表示されます
- タスクの番号は `/todo done`・`/todo remove` にそのまま使える番号と一致しています
- ボードのメッセージが誤って削除された場合、次にTodoを操作した時（または `/todo setup` 実行時）に自動で新しいボードが作成されます
- 表示件数が多い場合は一部を省略し「…ほかN件」と表示します（Discordのメッセージ長制限のため）

### 通知について

- `@everyone` は締切リマインド専用メッセージでのみ使用します。タスクの追加・完了・削除の確認メッセージやTodoボードの更新では、通知が飛ばないよう明示的にメンションを無効化しています。
- リマインドの送信状況はDBに保存されるため、Bot再起動後も二重送信されません。
- 長期間Botが停止していた等の理由で複数の通知タイミングが同時に溜まっていた場合でも、まとめて連投せず直近1件だけ通知し、古いものは静かに処理済みとして扱います。
- 締切から24時間以上経過した未送信リマインドは、通知を送らずに処理済みとしてスキップします（意味のない大幅遅延通知を防ぐため）。

---

## 1. Discord Bot を作成する

1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセスし、「New Application」で新規作成。
2. 左メニューの **Bot** を開き、「Reset Token」でトークンを発行してコピーしておく（後で使います。人に見せないこと）。
3. 同じ **Bot** ページで、特に権限（Privileged Gateway Intents）は今回オンにしなくてOKです（メッセージ内容は使わないため）。
4. 左メニューの **OAuth2 > URL Generator** を開き、
   - **SCOPES**: `bot`, `applications.commands` にチェック
   - **BOT PERMISSIONS**: `Send Messages`, `Embed Links`, `Read Message History`、および **`Mention @everyone, @here, and All Roles`**（締切リマインドで`@everyone`を送るために必須）にチェック
5. 生成されたURLをブラウザで開き、Botを導入したいサーバーを選んで招待する。

> ⚠️ **`Mention @everyone, @here, and All Roles` 権限がないと、締切リマインドの `@everyone` がただの文字列として表示され、通知が飛びません。** 既に招待済みのBotに権限を追加する場合は、サーバー設定のロール一覧からBotのロールにこの権限をオンにしてください。

## 2. コードをGitHubにアップロードする

このフォルダの中身をそのままGitHubリポジトリにpushしてください。

```bash
cd discord-todo-bot
git init
git add .
git commit -m "Initial commit: Discord Todo Bot"
git branch -M main
git remote add origin https://github.com/あなたのユーザー名/リポジトリ名.git
git push -u origin main
```

`.env` ファイルは `.gitignore` で除外されるため、**トークンが誤って公開される心配はありません**。

## 3. Railway でデプロイする（PCを起動しなくても24時間稼働）

Railwayは無料枠があり、GitHubリポジトリと連携するだけで自動デプロイできるためおすすめです。
（Render、Fly.ioなど他のサービスでも同様の手順でデプロイ可能です。）

1. [Railway](https://railway.app/) にアクセスし、GitHubアカウントでログイン。
2. 「New Project」→「Deploy from GitHub repo」を選択し、先ほど作成したリポジトリを選ぶ。
3. デプロイ後、プロジェクトの **Variables** タブを開き、以下の環境変数を追加する。
   - `DISCORD_TOKEN` : 手順1でコピーしたBotトークン
4. **Settings** タブで Start Command が `python bot.py`（Procfileから自動認識されます）になっていることを確認。
5. **重要（データ永続化）**: 再デプロイのたびにファイルシステムがリセットされるため、Todoを保存し続けるには **Volume（永続ボリューム）** を追加してください。
   - Railwayの場合: プロジェクトの **Settings > Volumes** から Volume を作成し、マウントパスを `/app/data` に設定する。
   - これによりコンテナが再起動・再デプロイされても `data/todos.db` の内容が消えなくなります。
6. デプロイが完了すると自動的にBotが起動し、そのままオンライン状態になります。

### 既存ユーザーへの注意（バージョンアップ時）

以前からこのBotを使っている場合、`bot.py` を新しいものに差し替えて再デプロイするだけで問題ありません。
初回起動時に `todos` テーブルへ `deadline_utc` / `notify_type` 列が自動追加され、**既存のTodoデータは削除されません**。追加で `boards`（固定ボード管理用）・`reminder_log`（送信済みリマインド管理用）テーブルも自動作成されます。手動でのDB操作は不要です。

各チャンネルで固定ボードを使い始めるには、一度だけ `/todo setup` を実行してください（実行しないチャンネルは今まで通り `/todo list` のみで確認する運用になります）。

### Railwayへの再デプロイ手順

```bash
git add .
git commit -m "Todoボード・締切リマインド機能を追加"
git push
```

GitHubリポジトリと連携済みのRailwayプロジェクトは、pushを検知して自動的に再ビルド・再デプロイされます。環境変数やVolumeの設定を変更する必要はありません。デプロイ完了後、Discord上のBotのロールに `Mention @everyone, @here, and All Roles` 権限が付与されているか念のため確認してください。

## 4. 動作確認

Discordサーバーで `/todo add` などと入力し、コマンド候補が表示されれば成功です。
反映されない場合は、Botを再起動するかDiscordクライアントを再起動してみてください（スラッシュコマンドの反映に数分〜1時間ほどかかることがあります）。

開発中に即座に反映させたい場合は、`.env` に `GUILD_ID`（対象サーバーのID）を設定すると、そのサーバーだけ即時反映されます。

## ローカルでテストする場合

```bash
python -m venv venv
source venv/bin/activate  # Windowsの場合: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# .env を開いて DISCORD_TOKEN を入力
python bot.py
```

## ファイル構成

```
discord-todo-bot/
├── bot.py              # Bot本体（Todo管理・固定ボード・締切リマインド）
├── requirements.txt    # 依存パッケージ
├── Procfile            # ホスティングサービス用の起動コマンド
├── .env.example        # 環境変数のサンプル
├── .gitignore
├── data/
│   └── .gitkeep         # todos.db が保存される場所
└── README.md
```
