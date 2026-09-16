# Discord Todo Bot

テキストチャンネル上で使えるシンプルなTodo管理Discord Botです。
スラッシュコマンド（`/todo ...`）でタスクの追加・一覧表示・完了・削除ができます。
GitHub連携型のホスティングサービス（Railway等）にデプロイすることで、自分のPCを起動していなくても24時間動き続けます。

## できること

| コマンド | 内容 |
|---|---|
| `/todo add task:買い物に行く` | タスクを追加 |
| `/todo list` | このチャンネルのTodo一覧を表示 |
| `/todo done number:1` | 番号を指定して完了にする |
| `/todo remove number:1` | 番号を指定して削除する |
| `/todo clear` | 完了済みタスクをまとめて削除 |

タスクは **チャンネルごと** に保存されます（SQLiteデータベース `data/todos.db` に保存）。

---

## 1. Discord Bot を作成する

1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセスし、「New Application」で新規作成。
2. 左メニューの **Bot** を開き、「Reset Token」でトークンを発行してコピーしておく（後で使います。人に見せないこと）。
3. 同じ **Bot** ページで、特に権限（Privileged Gateway Intents）は今回オンにしなくてOKです（メッセージ内容は使わないため）。
4. 左メニューの **OAuth2 > URL Generator** を開き、
   - **SCOPES**: `bot`, `applications.commands` にチェック
   - **BOT PERMISSIONS**: `Send Messages`, `Embed Links`, `Read Message History` にチェック
5. 生成されたURLをブラウザで開き、Botを導入したいサーバーを選んで招待する。

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
├── bot.py              # Bot本体
├── requirements.txt    # 依存パッケージ
├── Procfile            # ホスティングサービス用の起動コマンド
├── .env.example        # 環境変数のサンプル
├── .gitignore
├── data/
│   └── .gitkeep         # todos.db が保存される場所
└── README.md
```
