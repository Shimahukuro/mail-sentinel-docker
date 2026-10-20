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

Mail Sentinelの利用バージョンは、worker起動時の`application_started`および`worker_started`ログの`version`で確認できる。永続状態の`mail_sentinel_version`と`runtime_version_recorded`監査イベントにも記録されるため、障害調査では次の出力を採取する。

```sh
docker compose logs --tail=200 worker
docker compose --profile tools run --rm admin --account primary status
docker compose --profile tools run --rm admin --account primary audit --limit 100
```

## 通知

汎用HTTP Webhookを使用する場合、Webhook URLだけを含むSecretを作り、workerとadminの`/run/secrets/notification_webhook_url`へマウントする。アカウント設定へ次を追加する。

```json
{
  "NOTIFICATION_ENABLED": true,
  "NOTIFICATION_WEBHOOK_URL_FILE": "/run/secrets/notification_webhook_url",
  "NOTIFICATION_FAILURE_THRESHOLD": 3,
  "NOTIFICATION_REPEAT_SECONDS": 21600,
  "NOTIFICATION_RECOVERY_ENABLED": true,
  "NOTIFICATION_UPDATE_ENABLED": true,
  "VERSION_CHECK_INTERVAL_SECONDS": 86400,
  "VERSION_CHECK_TIMEOUT_SECONDS": 10,
  "BACKLOG_MESSAGE_THRESHOLD": 100
}
```

同じ障害は閾値到達時に通知され、その後は再通知間隔まで抑制される。正常処理を確認すると復旧通知を送る。通知疎通は次で確認する。

workerはGitHub Releasesの最新安定版を既定で24時間ごとに確認する。`VERSION`より新しいリリースがある場合、`update_available`をリリースごとに1回送信する。Payloadには`current_version`、`latest_version`、`release_url`が含まれる。確認・通知の失敗はメール処理を停止させない。更新通知を止める場合は`NOTIFICATION_UPDATE_ENABLED=false`を設定する。

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

### IMAPのTLS接続と証明書エラー

workerとadminは、`IMAP_TLS_MODE=implicit`（IMAPS）と`starttls`の両方で、認証情報を送る前に証明書の信頼性、有効期限、`IMAP_HOST`とのホスト名一致を検証する。検証やSTARTTLSが失敗しても平文へ自動的に切り替えない。`none`は隔離されたローカルテスト用とする。

更新後に接続できなくなった場合は、まずログの`error_type`を確認する。起動時の失敗は`startup_diagnostic`の`result=fail`に記録される。ログは例外の詳細を省くため、型だけでは証明書エラーの原因を特定できない場合がある。

| 確認事項 | 対応 |
| --- | --- |
| `IMAP_HOST`と証明書の名前が一致するか | プロバイダー指定の接続先名を使用する。IPアドレス指定や別名では一致しない場合がある |
| 証明書の有効期限、サーバー・Dockerホストの時刻 | 管理者へ証明書更新を依頼し、ホストの時刻を確認する |
| 中間証明書を含む証明書チェーン | サーバー管理者へ正しいチェーンの提示を依頼する |
| 独自CAを使用しているか | 管理者から信頼できる経路でCA証明書を入手し、`IMAP_CA_FILE`で標準CAへ追加する |
| CAファイルのパス・PEM形式・読み取り権限 | workerとadminの両方へマウントし、コンテナのUID `10001`で読み取れることを確認する |
| TLS方式、ポート、サーバーの対応 | プロバイダー指定の組み合わせにする。古いTLSしか使えない場合はサーバー更新を相談する |

独自CAの具体的なマウント方法は[ユーザーガイドの接続設定](user-guide.md#41-アカウント設定ファイル)を参照する。サーバーの秘密鍵は配布しない。接続先から取得した証明書を確認せず信頼登録せず、証明書検証の無効化や平文接続への変更で回避しない。

CA証明書を更新した場合、設定・マウントと新旧CAの切り替え時期を管理者と確認する。ファイル置換が既存のバインドマウントへ反映されない場合もあるため、診断には新しい一時コンテナを使用し、workerも再作成する。公的CA配下でのサーバー証明書更新では、信頼チェーンが維持されていれば通常は追加ファイル不要である。

### TLS修正を含むアプリケーション更新

以下は本リポジトリのビルド構成での手順。既存の設定とCAファイルを確認し、運用時の`DRY_RUN`設定を保ったまま診断する。`--diagnose`は監視処理を開始しない。

```sh
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml build worker admin
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml run --rm worker /usr/local/bin/mail-sentinel-supervisor --diagnose
```

終了コードと各アカウントの`startup_diagnostic_complete`の`result=pass`を確認してから反映する。

```sh
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml up -d --force-recreate worker
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml --profile tools run --rm admin --account primary health --live
```

adminの確認では出力の`live.imap`と`live.spamassassin`が`pass`であることを確認する（コマンドの終了コードだけで判断しない）。adminは実行のたびに一時コンテナを作るため、次回から再ビルドしたイメージとCAマウントを使用する。失敗時は設定・証明書を修正して再診断し、既存データのボリュームを削除しない。

### IMAP移動方式

起動ログの`imap_move_method_selected`を確認する。`uid_move`はサーバーの`UID MOVE`、`copy_uid_expunge`は`UIDPLUS`と一時キーワードを使う安全なフォールバック、`unsupported`は移動不可を示す。既定値は`IMAP_MOVE_FALLBACK=disabled`であり、フォールバックは`auto`を明示したアカウントだけで有効になる。`unsupported`の場合は`DRY_RUN=false`へ切り替えず、設定値とIMAPサーバーの`MOVE`、`UIDPLUS`、移動元・先の`PERMANENTFLAGS`を確認する。

`copy_uid_expunge`の中断状態は永続状態DBの`move_transactions`から自動再開される。手動でJunkの一時キーワードや状態DBを削除しない。再試行後も復旧しない場合はworkerを停止してバックアップを取得し、`error_type`と方式判定理由を確認する。

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
`IMAP_CA_FILE`で追加したCA証明書と、そのComposeマウント設定もこのアーカイブには含まれない。復元先で同じ信頼設定を再構成できるよう別途保全する。CA証明書は公開情報だが、置き換えられないよう完全性とアクセス権を管理する。

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
