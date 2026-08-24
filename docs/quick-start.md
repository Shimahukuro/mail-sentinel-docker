# Mail Sentinel Docker クイックスタート

このガイドでは、Mail Sentinel Dockerを初めて導入する利用者向けに、リポジトリの取得からドライラン、本番運用への切り替えまでを説明します。

実メールボックスへ接続する前にバックアップを取得してください。最初は必ず`DRY_RUN=true`で起動し、判定結果を確認するまで本番運用へ切り替えないでください。

## 1. 前提条件

- Gitがインストールされ、`git`コマンドを利用できる
- Docker EngineとDocker Compose v2が利用できる
- 対象メールサーバーでIMAPまたはIMAPSを利用できる
- IMAPユーザー名とパスワード、アプリパスワード、またはアクセストークンを用意できる
- INBOXと迷惑メール用フォルダーを確認できる

WindowsではDocker DesktopのLinuxコンテナを使用し、PowerShellから操作します。Linuxコンテナは既定のため、通常は切り替え操作を必要としません。次のコマンドが`linux`を表示することを確認してください。

```powershell
docker info --format '{{.OSType}}'
```

`windows`と表示される場合だけ、タスクバーのDocker Desktopメニューから`Switch to Linux containers`を選択します。メニューに切り替え項目がなく、上のコマンドが`linux`を表示する場合はそのまま利用できます。

## 2. リポジトリを取得する

```sh
git clone https://github.com/Shimahukuro/mail-sentinel-docker.git
cd mail-sentinel-docker
```

## 3. 設定ファイルを作成する

配布用の雛形をコピーします。

macOS / Linux:

```sh
mkdir -p config secrets
cp accounts.example.json config/accounts.json
cp docker-compose.accounts.example.yml config/docker-compose.accounts.yml
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path config, secrets | Out-Null
Copy-Item accounts.example.json config/accounts.json
Copy-Item docker-compose.accounts.example.yml config/docker-compose.accounts.yml
```

### 3.1 `accounts.json`を編集する

`config/accounts.json`を開き、対象メールサーバーに合わせて編集します。初回は`DRY_RUN`を必ず`true`にします。

```json
{
  "defaults": {
    "IMAP_PORT": 993,
    "IMAP_TLS_MODE": "implicit",
    "IMAP_AUTH_METHOD": "password",
    "IMAP_INBOX": "INBOX",
    "POLL_INTERVAL_SECONDS": 60,
    "BATCH_SIZE": 25,
    "LOOKBACK_DAYS": 1,
    "CREATE_MISSING_FOLDERS": false,
    "DRY_RUN": true
  },
  "accounts": [
    {
      "name": "primary",
      "environment": {
        "IMAP_HOST": "imap.example.com",
        "IMAP_USERNAME": "user@example.com",
        "IMAP_PASSWORD_FILE": "/run/secrets/imap_primary_password",
        "IMAP_JUNK": "Junk"
      }
    }
  ]
}
```

実際には、`accounts.example.json`に含まれる他の既定値も残してください。

### 3.2 `docker-compose.accounts.yml`を確認する

単一アカウントで雛形と同じSecret名を使用する場合、コピーした`config/docker-compose.accounts.yml`をそのまま利用できます。

```yaml
services:
  worker:
    secrets:
      - imap_primary_password

  admin:
    secrets:
      - imap_primary_password

secrets:
  imap_primary_password:
    file: ./secrets/imap_primary_password
```

複数アカウントを使用する場合は、アカウントごとに別のSecretを定義し、workerとadminの両方へ割り当てます。

## 4. IMAPパスワードをSecretへ保存する

パスワードを`accounts.json`へ直接書かず、Secretファイルへ保存します。

macOS / Linux:

```sh
printf 'IMAP password: '
read -r -s IMAP_PASSWORD
printf '\n'
printf '%s' "$IMAP_PASSWORD" > secrets/imap_primary_password
unset IMAP_PASSWORD
chmod 600 secrets/imap_primary_password
```

Windows PowerShellでSecretを安全に作成する手順は[ユーザーガイド](user-guide.md#43-imapパスワード)を参照してください。

`config/*`と`secrets/*`はGitの管理対象外です。SecretファイルをIssue、ログ、チャットへ貼り付けないでください。

### Gmailの場合

Gmailでは通常のGoogleアカウントパスワードを使用できません。Googleアカウントで2段階認証を有効にしてアプリパスワードを発行し、その値をSecretファイルへ保存します。

```json
"IMAP_HOST": "imap.gmail.com",
"IMAP_AUTH_METHOD": "app_password",
"IMAP_USERNAME": "user@gmail.com",
"IMAP_PASSWORD_FILE": "/run/secrets/imap_primary_password",
"IMAP_JUNK": "[Gmail]/Spam"
```

詳しくは[ユーザーガイドのGmail設定](user-guide.md#44-gmailで使用する場合)を参照してください。

## 5. 構成を検証する

```sh
docker compose \
  -f docker-compose.yml \
  -f config/docker-compose.accounts.yml \
  config --quiet
```

何も表示されず終了すれば、Composeの構文は正常です。エラーが表示された場合は起動せず、ファイル名、Secret名、YAMLのインデントを確認してください。

## 6. ドライランで起動する

`config/accounts.json`で`DRY_RUN=true`になっていることを再確認してから起動します。

```sh
docker compose \
  -f docker-compose.yml \
  -f config/docker-compose.accounts.yml \
  up -d --build
```

コンテナの状態を確認します。

```sh
docker compose \
  -f docker-compose.yml \
  -f config/docker-compose.accounts.yml \
  ps
```

## 7. 診断結果と判定ログを確認する

```sh
docker compose \
  -f docker-compose.yml \
  -f config/docker-compose.accounts.yml \
  logs -f worker
```

次の点を確認します。

- `startup_diagnostic_complete`の`result`が`pass`になっている
- IMAP認証エラーや接続エラーがない
- INBOXとJunkフォルダーが意図した名前で認識されている
- 迷惑メールの判定結果が`would_move`になっている
- 正常メールの判定結果が`would_mark`になっている
- 重大な誤判定がない

ドライランではメールの取得と採点を行いますが、メールの移動、削除、フラグ変更は行いません。同じメールが監視のたびに再判定されるのは正常な動作です。

診断だけを手動実行する場合:

```sh
docker compose \
  -f docker-compose.yml \
  -f config/docker-compose.accounts.yml \
  run --rm worker /usr/local/bin/mail-sentinel-supervisor --diagnose
```

## 8. 本番運用へ切り替える

診断とドライランに問題がないことを確認した後、`config/accounts.json`を変更します。

```json
"DRY_RUN": false
```

workerを再作成して設定を反映します。

```sh
docker compose \
  -f docker-compose.yml \
  -f config/docker-compose.accounts.yml \
  up -d --force-recreate worker
```

切り替え後もログを確認し、迷惑メールだけがJunkへ移動していることを確かめてください。

## 9. 停止と再開

停止:

```sh
docker compose \
  -f docker-compose.yml \
  -f config/docker-compose.accounts.yml \
  down
```

再開:

```sh
docker compose \
  -f docker-compose.yml \
  -f config/docker-compose.accounts.yml \
  up -d
```

`down`では永続ボリュームを削除しません。`down -v`は処理状態、Bayes学習データ、SpamAssassinルールを削除するため、通常運用では実行しないでください。

## 次に読むドキュメント

- [ユーザーガイド](user-guide.md) — 全設定項目、学習機能、初期スキャン、トラブルシューティング
- [運用Runbook](operations-runbook.md) — 監視、障害対応、バックアップと復元
- [GreenMailによるローカルテスト](greenmail-test.md) — 実メールボックスを使わない統合テスト

インストール、設定、操作方法について分からない点がある場合は、[Q&Aを新規作成](https://github.com/Shimahukuro/mail-sentinel-docker/discussions/new?category=q-a)から質問してください。リンクを開くと、GitHub DiscussionsのQ&Aカテゴリを選択した新規投稿フォームが表示されます。利用OS、Dockerのバージョン、実行したコマンド、Secretを除いたエラーメッセージを添えてください。パスワード、アクセストークン、実際のメールアドレス、メール本文は投稿しないでください。
