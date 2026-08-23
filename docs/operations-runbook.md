# Mail Sentinel 運用Runbook

このRunbookは日常確認、障害通知、更新、バックアップ、復元を扱う。コマンド中のComposeファイル指定は、導入時に使用したものへ読み替える。

```sh
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml COMMAND
```

## 日常確認

コンテナとアカウント別状態を確認する。

```sh
docker compose ps
docker compose --profile tools run --rm admin --account primary health
docker compose --profile tools run --rm admin --account primary status
docker compose --profile tools run --rm admin --account primary incidents
```

外部接続を含む確認には`health --live`を使用する。出力にはメール本文、件名、送信者、完全なアカウント名を含めない。

## 通知

汎用HTTP Webhookを使用する場合、Webhook URLだけを含むSecretを作り、workerとadminの`/run/secrets/notification_webhook_url`へマウントする。アカウント設定へ次を追加する。

```json
{
  "NOTIFICATION_ENABLED": true,
  "NOTIFICATION_WEBHOOK_URL_FILE": "/run/secrets/notification_webhook_url",
  "NOTIFICATION_FAILURE_THRESHOLD": 3,
  "NOTIFICATION_REPEAT_SECONDS": 21600,
  "NOTIFICATION_RECOVERY_ENABLED": true,
  "BACKLOG_MESSAGE_THRESHOLD": 100
}
```

同じ障害は閾値到達時に通知され、その後は再通知間隔まで抑制される。正常処理を確認すると復旧通知を送る。通知疎通は次で確認する。

```sh
docker compose --profile tools run --rm admin --account primary notification-test
```

通知送信失敗はメール処理を停止させない。`notification_failed`イベントと`notification_failed_count`を確認する。

## 障害対応

1. `health`、`incidents`、workerとspamassassinのログを取得する。
2. `event_code`に従ってIMAP接続、spamd、移動、学習、滞留を切り分ける。
3. Secretの内容をログやIssueへ貼り付けない。
4. 復旧後、`health --live`と復旧通知を確認する。

```sh
docker compose logs --tail=200 worker spamassassin
docker compose --profile tools run --rm admin --account primary audit --limit 100
```

## ルール更新

```sh
docker compose run --rm spamassassin update-rules
docker compose restart spamassassin
docker compose --profile tools run --rm admin --account primary status
```

`spamassassin --lint`が失敗した場合は再起動しない。成功日時は永続ルール領域へ記録され、`status`で確認できる。

## バックアップ

バックアップ中のSQLite WALとBayes更新を止めるため、workerとspamassassinを停止する。

```sh
docker compose stop worker spamassassin
docker compose --profile maintenance run --rm maintenance backup --output /backups/mail-sentinel.tar.gz
docker compose --profile maintenance run --rm maintenance verify --archive /backups/mail-sentinel.tar.gz
docker compose start spamassassin worker
```

アーカイブには処理状態、Bayesデータ、SpamAssassinルール、manifest、各構成物のSHA-256が含まれる。IMAP Secretと`accounts.json`は含まれないため、別のSecret管理手順で保全する。

保守コンテナは制限付き所有権のファイルも保全するためrootで動作するが、通常起動されない`maintenance` profileに限定する。capabilityは所有権維持と読み書きに必要な`CHOWN`、`DAC_OVERRIDE`、`DAC_READ_SEARCH`、`FOWNER`だけを戻し、`no-new-privileges`と読み取り専用ルートファイルシステムを使用する。

## 復元

復元は対象ボリュームを置換する。新しい環境または停止済みで復元してよい環境だけで実行する。

```sh
docker compose stop worker spamassassin
docker compose --profile maintenance run --rm maintenance verify --archive /backups/mail-sentinel.tar.gz
docker compose --profile maintenance run --rm maintenance restore --archive /backups/mail-sentinel.tar.gz --confirm RESTORE
docker compose --profile maintenance run --rm maintenance check-state
docker compose run --rm spamassassin spamassassin --lint
docker compose run --rm spamassassin sa-learn --dump magic
docker compose start spamassassin worker
docker compose --profile tools run --rm admin --account primary health --live
```

## ログ保持と外部収集

標準Compose設定ではDockerの`json-file`を1ファイル10MB、3世代まで保持する。長期保持が必要な場合は、Docker daemonのlogging driverまたはホスト上のログ収集エージェントを使用する。外部へ送る前に、許可フィールドを`timestamp`、`level`、`event`、匿名化`account_id`、UID、件数、スコア、`error_type`へ限定する。

## Windows、macOS、Linux

- Linux: Docker EngineとCompose pluginから上記コマンドを実行する。
- macOS: Docker Desktopのターミナルから同じコマンドを実行する。
- Windows: Docker DesktopをLinux containersモードで使用し、PowerShellまたはWSLから同じComposeコマンドを実行する。シェル変数に依存しないためコマンド構造は共通である。

どのOSでもバックアップ先はリポジトリの`backups`ディレクトリへ作成される。バックアップファイルはGitへ登録しない。
