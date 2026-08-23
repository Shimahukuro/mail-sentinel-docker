# Mail Sentinel Docker ユーザーガイド

## 1. このガイドについて

Mail Sentinel Dockerは、IMAPメールボックスの新着メールをSpamAssassinで検査し、迷惑メールを同じメールボックスのJunkフォルダーへ移動するPoCシステムである。

このガイドでは、現在実装されている次の操作を説明する。

- 初期設定
- ドライランによる安全確認
- 起動前診断
- 通常運転への切り替え
- ログと判定結果の確認
- SpamAssassinルールの更新
- 停止、再起動、障害時の確認
- GreenMailとRoundcubeを使ったローカルテスト

メールの自動削除、OAuth 2.0のトークン自動更新、管理画面にはまだ対応していない。複数アカウント、学習フォルダー、事前取得したアクセストークンによるXOAUTH2認証には対応している。

## 2. 動作の概要

通常運転では、次の順序でメールを処理する。

1. workerが設定されたIMAPサーバーへ接続する。
2. INBOXの未処理メールを取得する。
3. SpamAssassinがメールを採点する。
4. 正常メールには処理済みIMAPキーワードを付け、INBOXに残す。
5. 迷惑メールはJunkフォルダーへ移動する。
6. 判定不能または処理失敗の場合は、メールをINBOXに残して再試行する。

Mail Sentinelを停止しても、メールサーバーによる受信や通常のメールクライアントからの閲覧には影響しない。

## 3. 前提条件

- Docker EngineとDocker Composeが利用できる
- 対象メールサーバーでIMAPまたはIMAPSを利用できる
- IMAPユーザー名とパスワードまたはアプリパスワードを用意できる
- INBOXと迷惑メール用フォルダーを確認できる
- 対象IMAPサーバーがユーザー定義キーワードを利用できる

実メールボックスへ接続する前に、バックアップとメールプロバイダーの利用条件を確認する。

### 3.1 コマンド表記

このガイドはmacOS、Linux、Windowsに対応する。

- `共通`と記載したコマンドは、macOS/LinuxのターミナルとWindows PowerShellのどちらでも実行できる。
- OSごとに構文が異なる操作は、`macOS/Linux`と`Windows PowerShell`を分けて記載する。
- WindowsではDocker DesktopをLinuxコンテナモードで起動し、PowerShellから実行する。詳しい前提条件は「15. Windowsで利用する場合」を参照する。
- コマンドは、いずれもリポジトリのルートディレクトリで実行する。

## 4. 初期設定

### 4.1 アカウント設定ファイル

設定例をコピーする。

macOS/Linux:

```sh
mkdir -p config
cp accounts.example.json config/accounts.json
cp docker-compose.accounts.example.yml config/docker-compose.accounts.yml
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path config | Out-Null
Copy-Item accounts.example.json config/accounts.json
Copy-Item docker-compose.accounts.example.yml config/docker-compose.accounts.yml
```

`config/accounts.json`を対象メールサーバーに合わせて編集する。単一アカウントでも`accounts`配列へ1件を定義する。

```json
{
  "accounts": [{
    "name": "primary",
    "environment": {
      "IMAP_HOST": "imap.example.com",
      "IMAP_USERNAME": "user@example.com",
      "IMAP_PASSWORD_FILE": "/run/secrets/imap_primary_password",
      "IMAP_JUNK": "Junk"
    }
  }]
}
```

主な設定項目は次のとおり。

| 設定 | 内容 | 初期推奨値 |
| --- | --- | --- |
| `IMAP_HOST` | IMAPサーバー名 | プロバイダー指定値 |
| `IMAP_PORT` | IMAPポート | IMAPSでは`993` |
| `IMAP_TLS_MODE` | `implicit`、`starttls`、`none` | 実環境では`implicit` |
| `IMAP_USERNAME` | IMAPログイン名 | 対象アカウント |
| `IMAP_INBOX` | 監視フォルダー | `INBOX` |
| `IMAP_JUNK` | 迷惑メール移動先 | 実際のフォルダー名 |
| `LEARNING_ENABLED` | ユーザーフィードバック学習を有効にするか | 初回は`false` |
| `IMAP_LEARN_HAM` | 正常メール学習用IMAPフォルダー | `Learn-Ham` |
| `IMAP_LEARN_SPAM` | 迷惑メール学習用IMAPフォルダー | `Learn-Spam` |
| `LEARNED_FLAG` | 学習成功済みを示すIMAPキーワード | `MailSentinelLearned` |
| `LEARNING_BATCH_SIZE` | 種別ごとの1回の最大学習件数 | `25` |
| `POLL_INTERVAL_SECONDS` | 確認間隔 | `60` |
| `BATCH_SIZE` | 1回の最大処理件数 | `25` |
| `LOOKBACK_DAYS` | 通常監視の対象日数 | `1` |
| `CREATE_MISSING_FOLDERS` | Junkがない場合に作成するか | 実環境では`false` |
| `DRY_RUN` | メールを変更せず判定だけ行うか | 初回は`true` |
| `PROCESSED_STATE` | 処理済み状態の管理方式（`auto`、`imap_keyword`、`local_database`） | `auto` |
| `RETRY_INITIAL_SECONDS` | 障害時の最初の再試行待機 | `5` |
| `RETRY_MAX_SECONDS` | 再試行待機時間の上限 | `300` |

共通値はトップレベルの`defaults`へ、アカウント固有値は各`environment`へ記述する。`IMAP_TLS_MODE=none`は、GreenMailなどローカルの隔離されたテスト環境だけで使用する。

### 4.2 ユーザーフィードバック学習

学習機能を利用する場合は、メールクライアントまたはプロバイダーのWebメールで`Learn-Ham`と`Learn-Spam`を事前に作成する。workerは実環境の学習フォルダーを自動作成しない。

フォルダー作成後、次を設定して`DRY_RUN=true`のまま診断する。

```json
"LEARNING_ENABLED": true,
"IMAP_LEARN_HAM": "Learn-Ham",
"IMAP_LEARN_SPAM": "Learn-Spam",
"LEARNED_FLAG": "MailSentinelLearned",
"LEARNING_BATCH_SIZE": 25,
"DRY_RUN": true
```

診断では学習フォルダーの存在と参照可否を確認する。IMAPにはフォルダー作成可否を変更なしで確定する標準操作がないため、存在しないフォルダーの作成可否はDry-Runでは確認できない。診断が成功した後に`DRY_RUN=false`へ変更する。

- 正常なのにJunkへ移動されたメールは`Learn-Ham`へ移動する。
- 迷惑メールなのにINBOXへ残ったメールは`Learn-Spam`へ移動する。
- 学習成功後、hamはINBOX、spamはJunkへ移動する。
- 学習失敗時は元の学習フォルダーへ残り、次回の監視で再試行する。

IMAPキーワード方式では学習済みメールに`LEARNED_FLAG`が付く。ローカルDB方式では同じ状態をSQLiteへ保存する。どちらも学習成功後に移動だけが失敗した場合、次回はBayes学習を繰り返さず移動だけを再試行する。キーワード方式のhamには`PROCESSED_FLAG`も付くため、INBOXへ戻った直後に通常判定されない。ローカルDB方式で移動先UIDを取得できない場合は、安全側で通常判定を一度行うことがある。

### 4.3 IMAPパスワード

パスワードは`accounts.json`へ記載せず、Secretファイルへ保存する。

macOS/Linux:

```sh
mkdir -p secrets
printf 'IMAP password: '
read -r -s IMAP_PASSWORD
printf '\n'
printf '%s' "$IMAP_PASSWORD" > secrets/imap_primary_password
unset IMAP_PASSWORD
chmod 600 secrets/imap_primary_password
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path secrets | Out-Null
$securePassword = Read-Host 'IMAP password' -AsSecureString
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)

try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    $secretPath = Join-Path $PWD 'secrets\imap_primary_password'
    [IO.File]::WriteAllText($secretPath, $plainPassword, [Text.UTF8Encoding]::new($false))
}
finally {
    if ($null -ne $plainPassword) { Remove-Variable plainPassword }
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
}
```

`config/accounts.json`と`secrets/*`はGitの管理対象外である。Secretファイルを他のOSユーザーと共有しない。共用PCではファイルのアクセス権も制限する。

## 5. 初回起動

### 5.1 設定内容の確認

Compose設定を検証する。

共通:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml config --quiet
```

何も表示されず終了すれば、Composeの構文は正常である。

### 5.2 ビルド

共通:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml build
```

### 5.3 ドライランで起動

最初は必ず`config/accounts.json`の`defaults`を次の状態にする。

```json
"DRY_RUN": true
```

起動する。

共通:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml up -d
```

状態を確認する。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml ps
```

workerは通常監視を開始する前に、読み取り専用の起動前診断を自動実行する。診断に失敗した場合、メールの移動やキーワード付与は行わない。

## 6. 起動前診断

起動前診断では次を確認する。

- IMAP接続と認証
- INBOXの存在
- Junkフォルダーの存在
- INBOXの読み取り
- SpamAssassinへの接続と採点

Junkフォルダーが存在せず、`CREATE_MISSING_FOLDERS=true`の場合は`fallback`として記録し、通常監視開始後に作成する。`false`の場合は診断に失敗する。

診断結果を確認する。

共通:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml logs worker
```

成功例:

```json
{"level":"info","event":"startup_diagnostic","check":"imap_connection","result":"pass"}
{"level":"info","event":"startup_diagnostic","check":"inbox_folder","folder":"INBOX","result":"pass"}
{"level":"info","event":"startup_diagnostic","check":"junk_folder","folder":"Junk","result":"pass"}
{"level":"info","event":"startup_diagnostic","check":"spamassassin_connection","result":"pass"}
{"level":"info","event":"startup_diagnostic_complete","result":"pass"}
```

診断だけを手動実行する場合は次を使用する。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml run --rm worker /usr/local/bin/mail-sentinel-supervisor --diagnose
```

この診断は既存メールの本文取得、移動、削除、フラグ変更を行わない。

## 7. ドライランの確認

ドライラン中もメールの取得とSpamAssassinによる採点は行うが、次の変更は行わない。

- Junkフォルダーへの移動
- 正常メールへの処理済みキーワード付与

ログを継続表示する。

共通:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml logs -f worker
```

迷惑メールのドライラン例:

```json
{"event":"message_classified","uid":12,"classification":"spam","score":"8.4/5.0","action":"would_move","destination":"Junk","dry_run":true}
```

正常メールのドライラン例:

```json
{"event":"message_classified","uid":13,"classification":"ham","score":"0.2/5.0","action":"would_mark","dry_run":true}
```

ドライランでは処理済みキーワードを付けないため、同じメールが次回も判定対象になる。これはメールボックスを変更しないための意図した動作である。

## 8. 通常運転への切り替え

次を確認してから切り替える。

1. 起動前診断がすべて`pass`または意図した`fallback`になっている。
2. `IMAP_JUNK`がメールクライアントで使用する迷惑メールフォルダーと一致している。
3. ドライランの判定結果に重大な誤判定がない。
4. `LOOKBACK_DAYS`と`BATCH_SIZE`が意図した範囲になっている。
5. 最初はテスト用または少数のメールで確認している。

`config/accounts.json`を変更する。

```json
"DRY_RUN": false
```

workerを再作成する。

共通:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml up -d --force-recreate worker
```

ログで`dry_run:false`を確認する。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml logs --tail=50 worker
```

通常運転の迷惑メール例:

```json
{"event":"message_classified","uid":12,"classification":"spam","score":"8.4/5.0","action":"moved","destination":"Junk","dry_run":false}
```

通常運転の正常メール例:

```json
{"event":"message_classified","uid":13,"classification":"ham","score":"0.2/5.0","action":"marked","dry_run":false}
```

運用状態、通知、バックアップ、復元の詳細は[運用Runbook](operations-runbook.md)を参照する。

## 9. ログの見方

workerは1行につき1つのJSONイベントを出力する。

| イベント | 内容 |
| --- | --- |
| `startup_diagnostic` | 起動前診断の個別結果 |
| `startup_diagnostic_complete` | 起動前診断の最終結果 |
| `worker_started` | 通常監視の開始 |
| `folder_created` | 設定に従ってフォルダーを作成 |
| `message_classified` | メールの判定と操作結果 |
| `message_deferred` | 判定または処理を保留 |
| `scan_complete` | 1回の監視処理の集計 |
| `scan_failed` | IMAPまたはSpamAssassin処理の失敗 |

本文、パスワード、完全なIMAPアカウント名はworkerの構造化ログへ出力しない。

主な確認コマンド:

共通:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml logs --tail=100 worker
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml logs -f worker spamassassin
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml ps
```

ログはコンテナごとに最大10MB、3ファイルまで保持する。長期的な監査記録が必要な場合は、別途ログ収集基盤を用意する。

## 10. 障害時の再試行

通常監視中にIMAP接続やSpamAssassin処理が失敗すると、workerはメールを変更せず再試行する。

待機時間は`RETRY_INITIAL_SECONDS`から始まり、連続失敗ごとに倍増し、`RETRY_MAX_SECONDS`を上限とする。正常な監視が1回完了すると、待機時間は初期値へ戻る。

例:

```json
{"event":"scan_failed","error":"login request failed","retry_in_seconds":5}
{"event":"scan_failed","error":"login request failed","retry_in_seconds":10}
{"event":"scan_failed","error":"login request failed","retry_in_seconds":20}
```

## 11. SpamAssassinルールの更新

ルール更新は自動では実行しない。管理者が次を実行する。

共通:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml run --rm spamassassin update-rules
```

更新処理は次を行う。

1. 公式更新チャンネルからルールを取得する。
2. GPG署名を検証する。
3. 更新ルールを`spamassassin-rules`ボリュームへ保存する。
4. `spamassassin --lint`でルールを検証する。

成功例:

```text
SpamAssassin rules updated.
SpamAssassin rule validation passed.
```

更新済みの場合:

```text
SpamAssassin rules are already current.
SpamAssassin rule validation passed.
```

成功後にSpamAssassinを再起動する。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml restart spamassassin
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml ps
```

更新または検証に失敗した場合は再起動せず、ネットワーク、DNS、時刻、ボリュームの書き込み権限を確認する。署名検証を無効化して回避しない。

## 12. 停止と再起動

### 停止

以下のDocker Composeコマンドはすべて共通である。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml down
```

この操作ではSpamAssassinのBayesデータと更新ルールの名前付きボリュームは削除されない。

### 起動

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml up -d
```

### workerだけを再起動

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml restart worker
```

`config/accounts.json`を変更した場合は、単純な再起動ではなく再作成する。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml up -d --force-recreate worker
```

### 状態確認

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml ps
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml logs --tail=100 worker spamassassin
```

## 13. トラブルシューティング

### `startup diagnostics failed`

次を確認する。

- `IMAP_HOST`と`IMAP_PORT`
- TLS方式
- ユーザー名
- Secretファイルの存在と内容
- INBOXとJunkの実際のフォルダー名
- メールプロバイダーでのIMAP利用許可
- アプリパスワードの要否

診断を単独実行して、最初に`fail`となる項目を確認する。

共通:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml run --rm worker /usr/local/bin/mail-sentinel-supervisor --diagnose
```

### Junkフォルダーが存在しない

実環境ではメールクライアントまたはプロバイダーのWebメールからJunkフォルダーを作成し、`accounts.json`の`IMAP_JUNK`を一致させることを推奨する。

自動作成する場合だけ次を指定する。

```json
"CREATE_MISSING_FOLDERS": true
```

### メールが何度も判定される

- `DRY_RUN=true`では意図した動作である。
- 通常運転の場合はIMAPサーバーがユーザー定義キーワードを保持できるか確認する。
- `PROCESSED_FLAG`を途中で変更していないか確認する。

### 迷惑メールがJunkへ移動しない

- ログの`classification`と`score`を確認する。
- `dry_run`が`true`になっていないか確認する。
- SpamAssassinの必要スコアは`spamassassin/local.cf`の`required_score`で確認する。
- `message_deferred`または`scan_failed`を確認する。

### メールが`message_too_large`で保留される

メールサイズが`SPAMC_MAX_SIZE_BYTES`を超えている。上限を変更する場合は、コンテナのメモリ使用量と処理時間への影響を確認する。保留中のメールは移動も処理済みキーワード付与も行わない。

### SpamAssassinルール更新に失敗する

- 外部ネットワークとDNSを確認する。
- ホストの時刻を確認する。
- `spamassassin-rules`ボリュームの状態を確認する。
- GPG署名エラーの場合は更新を適用しない。

## 14. 既存メールの初期学習と初期スキャン

初期処理は常駐workerから独立した`admin`管理ジョブとして、必ずプレビューと適用の2段階で実行する。日時にはタイムゾーンを含め、開始日時を含み終了日時を含まない範囲を指定する。

### 14.1 初期学習

確認済みspamをプレビューする例:

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml --profile tools run --rm admin --account primary initial-learn preview --folder Junk --type spam --since-date 2026-01-01 --through-date 2026-07-31 --timezone Asia/Tokyo --max-messages 1000 --batch-size 50
```

結果に出力された`job_id`と`confirmation_token`を使って適用する。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml --profile tools run --rm admin --account primary initial-learn apply --job-id PREVIEW_JOB_ID --confirm CONFIRMATION_TOKEN
```

初期学習は元メールを移動、削除、フラグ変更しない。同じメッセージ内容と学習種別の成功記録があればスキップする。hamとspamの対象を目視確認し、片方だけに偏らないようにする。

#### Bayes判定が有効になる最低学習件数

SpamAssassinの既定値では、Bayes判定を有効にするために、確認済みhamと確認済みspamを**それぞれ200件以上**学習させる必要がある。これは`bayes_min_ham_num=200`と`bayes_min_spam_num=200`に相当し、片方だけが200件へ達してもBayes判定は有効にならない。両方の最低件数を満たすまでは、学習コマンドが成功してデータが保存されていても、`BAYES_00`～`BAYES_99`ルールは通常の採点へ参加しない。

学習件数は次のコマンドで確認する。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml exec spamassassin sa-learn --dump magic
```

出力の`nham`がham学習数、`nspam`がspam学習数である。両方が200以上であることを確認してから、初期スキャンを再度プレビューし、`BAYES_*`ルールの発火、スコア分布、誤検出および検出漏れを確認する。

200件はBayes分類器が採点へ参加するための既定の最低条件であり、判定精度を保証する件数ではない。誤った教師データは精度を低下させるため、確実に分類できるメールだけを学習させる。最低件数を設定で引き下げると少量データへ過度に適合する危険があるため、本番環境では原則として既定値を維持する。

#### Yahoo!メールの迷惑メールを初期学習する場合

Yahoo!メールでは、IMAP上の`Bulk Mail`がウェブメールの「迷惑メール」フォルダーに対応する。迷惑メールフォルダー内のメールをほかのフォルダーへ移動すると、Yahoo!メール側で「迷惑メールではない」という利用者フィードバックとして扱われる場合がある。そのため、初期spam学習の準備として`Bulk Mail`内のメールを`Learn-Spam`やINBOXへ移動してはならない。

Yahoo!メールの初期spam学習では、`Bulk Mail`を直接、非破壊の学習元として指定する。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml --profile tools run --rm admin --account yahoo initial-learn preview --folder "Bulk Mail" --type spam --max-messages 500 --batch-size 25
```

previewで対象を確認してからapplyする。`initial-learn`管理ジョブはメール本文を学習に使用するだけで、元メールの移動、削除、フラグ変更を行わない。実行結果の`moved_count`が`0`であり、学習後も対象メールが`Bulk Mail`に残っていることを確認する。

`Bulk Mail`から別フォルダーへ移動する操作は、そのメールが実際にはspamではなく、Yahoo!メール側の判定を訂正する場合だけ行う。Yahoo!メールの「迷惑メールではない」という報告と受信箱への移動については、[Yahoo!メール公式ヘルプ](https://support.yahoo-net.jp/PccMail/s/article/H000011502)も参照する。

### 14.2 初期スキャン

最初は短い期間と小さい移動上限でプレビューする。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml --profile tools run --rm admin --account primary initial-scan preview --folder INBOX --since-date 2026-07-01 --through-date 2026-07-31 --timezone Asia/Tokyo --max-messages 500 --max-moves 20 --batch-size 25 --threshold 5.0
```

`scan_candidate`として表示されるUID、受信日時、件名、送信者、スコア、命中ルールを確認してから適用する。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml --profile tools run --rm admin --account primary initial-scan apply --job-id PREVIEW_JOB_ID --confirm CONFIRMATION_TOKEN
```

プレビューはメールを変更しない。適用時にUIDVALIDITYまたはルールセットが変わっていた場合は処理を拒否するため、新しいプレビューを作成する。失敗したメッセージがあるジョブは`interrupted`となり、同じpreviewのapplyコマンドを再実行すると同じ適用ジョブを再開する。

`--since-date`と`--through-date`は利用者のタイムゾーンにおける日付で、両端の日を含む。`--timezone`の既定値は`Asia/Tokyo`である。例えば`--since-date 2026-06-25 --through-date 2026-08-18`は、2026年6月25日00:00 JST以上、8月19日00:00 JST未満として処理される。IMAPの日単位検索では範囲を安全側へ前後1日広げ、取得した各メールの`INTERNALDATE`で厳密に絞り込む。

時刻単位の指定が必要な場合だけ、`--since-datetime`と`--before-datetime`へUTCオフセットを含むISO 8601日時を指定する。

ジョブ状態は次のコマンドで確認できる。

```console
docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml --profile tools run --rm admin --account primary status --job-id JOB_ID
```

### 14.3 バックアップとロールバック上の注意

ジョブ履歴は`mail-sentinel-state`、Bayesデータは`spamassassin-data`名前付きボリュームへ保存される。初期学習前には両方をバックアップする。Bayes学習は単純なファイル削除では安全に取り消せないため、誤学習時は対象を確認して反対種別で訂正するか、停止後にバックアップから復元する。

初期スキャンはspamだけをJunkへ移動し、削除しない。元へ戻す場合はジョブ結果とJunkの内容を確認してメールクライアントから行う。Junkへ元から存在したメールを一括してINBOXへ戻してはいけない。

通常workerと管理ジョブは同じアカウント・フォルダーの共有ロックを使う。管理ジョブの実行中は通常監視が待機する場合がある。

## 15. GreenMailによるローカルテスト

実メールボックスを操作せず確認する場合は、GreenMail用Compose設定を使用する。

共通:

```console
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml up -d --build
```

テスト用アカウント:

| 項目 | 値 |
| --- | --- |
| ユーザー名 | `test@test.local` |
| パスワード | `greenmail` |
| SMTP | `127.0.0.1:3025` |
| IMAP | `127.0.0.1:3143` |
| Roundcube | `http://127.0.0.1:8081` |

正常メールとGTUBEを投入する。

macOS/Linux:

```sh
curl --url smtp://127.0.0.1:3025 \
  --mail-from sender@test.local \
  --mail-rcpt test@test.local \
  --upload-file tests/greenmail/messages/ham.eml

curl --url smtp://127.0.0.1:3025 \
  --mail-from sender@test.local \
  --mail-rcpt test@test.local \
  --upload-file tests/greenmail/messages/gtube.eml
```

Windows PowerShell（`curl`ではなく`curl.exe`を使用する）:

```powershell
curl.exe --url smtp://127.0.0.1:3025 --mail-from sender@test.local --mail-rcpt test@test.local --upload-file tests/greenmail/messages/ham.eml
curl.exe --url smtp://127.0.0.1:3025 --mail-from sender@test.local --mail-rcpt test@test.local --upload-file tests/greenmail/messages/gtube.eml
```

Roundcubeへ`test@test.local`と`greenmail`でログインし、正常メールがINBOX、GTUBEがJunkにあることを確認する。

学習を確認する場合は、正常メールを`Learn-Ham`へ、GTUBEメールを`Learn-Spam`へ移動する。次回監視後、正常メールがINBOX、GTUBEメールがJunkへ移動し、workerログへ`learning_succeeded`が記録されることを確認する。失敗・再試行、重複防止、Bayes永続化の詳しい手順は[greenmail-test.md](greenmail-test.md)を参照する。

GreenMail用設定は統合試験のため`DRY_RUN=false`を指定している。実メール用の通常設定とは異なる点に注意する。

停止する。

共通:

```console
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml down
```

## 16. Windowsで利用する場合

### 16.1 前提条件

WindowsではDocker Desktop上のLinuxコンテナとして実行する。Windowsコンテナモードには対応していない。

推奨環境:

- Windows 10またはWindows 11
- WSL 2を有効にしたDocker Desktop
- Docker DesktopのLinux containersモード
- PowerShell 7またはWindows PowerShell 5.1
- Git for Windows

Docker Desktopを起動し、PowerShellで次を確認する。

```powershell
docker version
docker compose version
docker info --format '{{.OSType}}'
```

最後のコマンドが`linux`を表示することを確認する。`windows`の場合は、Docker DesktopのメニューからLinuxコンテナへ切り替える。

### 16.2 リポジトリと改行コード

workerのPythonファイルはLinuxコンテナ内で実行されるため、LF改行である必要がある。このリポジトリでは`.gitattributes`によって、Python、シェルスクリプト、Dockerfile、ComposeファイルをLFへ固定している。

通常は追加設定不要だが、古い作業コピーを使用している場合は、変更を退避またはコミットしたうえで再度チェックアウトする。エディターで保存する場合も、対象ファイルをCRLFへ変換しない。

### 16.3 Windows固有の補足

初期設定、起動、診断、GreenMailテストは、それぞれ本文のWindows PowerShellまたは共通の手順を上から順に実行する。

- SecretファイルはUTF-8、BOMなし、末尾改行なしで作成される。
- PowerShell環境によっては`curl`が別コマンドの別名になっているため、本文のSMTPテストでは`curl.exe`を使用する。
- Secretファイルのアクセス権は、ファイルのプロパティにある「セキュリティ」から確認できる。

### 16.4 Windows固有のトラブルシューティング

#### `exec ... no such file or directory`または`^M`を含むエラー

シェルスクリプトがCRLFへ変換されている可能性がある。`.gitattributes`が存在することを確認し、ローカル変更を退避してからファイルを再チェックアウトする。

#### Dockerへ接続できない

- Docker Desktopが起動しているか確認する。
- Docker DesktopがLinuxコンテナモードか確認する。
- WSL 2バックエンドの状態を確認する。
- PowerShellを開き直して再度`docker version`を実行する。

#### Secretファイルを読み取れない

- `accounts.json`の`IMAP_PASSWORD_FILE`とCompose Secretの対応を確認する。
- Secretファイル名へ意図せず`.txt`が付いていないか確認する。
- エクスプローラーで拡張子を表示して確認する。
- Docker Desktopにプロジェクトディレクトリへのアクセスが許可されているか確認する。

#### ポートを利用できない

GreenMailまたはRoundcubeの起動時にポートエラーが出る場合、`3025`、`3143`、`8081`を他のアプリケーションが使用していないか確認する。

```powershell
Get-NetTCPConnection -State Listen |
  Where-Object LocalPort -In 3025, 3143, 8081
```

## 17. 安全上の注意

- 実メールをテストfixtureとしてGitへコミットしない。
- `config/accounts.json`、パスワード、トークンをGitへ登録しない。
- 初回は必ずドライランを使用する。
- 誤判定メールを自動削除しない。
- GPG署名検証を無効化してルール更新を強行しない。
- `docker compose -f docker-compose.yml -f config/docker-compose.accounts.yml down -v`は永続データを削除するため、通常運用では使用しない。
- 実メールに含まれるURLや添付ファイルを不用意に開かない。

より詳しい設計と現在のPoC範囲は、[system-overview.md](system-overview.md)を参照する。GreenMail固有のテスト手順は、[greenmail-test.md](greenmail-test.md)を参照する。
# 複数アカウント設定

単一・複数アカウントのどちらも`accounts.json`で設定する。単一アカウントは`accounts`配列へ1件だけ定義する。

1. `accounts.example.json`を`config/accounts.json`へ、`docker-compose.accounts.example.yml`を`config/docker-compose.accounts.yml`へコピーする。
2. `accounts`にアカウントを追加する。`name`はログへ出力されないが、匿名識別子を安定させるため変更しない運用名にする。
3. 各アカウントの接続先、ユーザー名、フォルダー、処理設定をそれぞれの`environment`に記述する。
4. パスワードやトークンそのものはJSONに書かず、アカウントごとに別のCompose Secretとして雛形へ追加し、そのコンテナ内パスだけを`IMAP_PASSWORD_FILE`または`IMAP_OAUTH_ACCESS_TOKEN_FILE`に指定する。

アカウントを増やす場合は、JSONのアカウント要素、workerとadminのSecret割り当て、トップレベルのSecret定義、ホスト側Secretファイルの4点を同じSecret名で追加する。

スーパーバイザーはアカウントごとに独立したワーカープロセスを起動する。一方の接続診断や監視が失敗して終了しても、他方は停止せず、失敗したプロセスだけが再起動される。各ログには設定名を SHA-256 で変換した 12 桁の `account_id` が付く。状態とロックは `STATE_DIR/accounts/<account_id>` に分離される。

`IMAP_AUTH_METHOD` はアカウントごとに次から選択できる。

| 値 | Secret パスの設定 | 用途 |
| --- | --- | --- |
| `password` | `IMAP_PASSWORD_FILE` | 通常のパスワード |
| `app_password` | `IMAP_PASSWORD_FILE` | プロバイダー発行のアプリパスワード |
| `xoauth2` | `IMAP_OAUTH_ACCESS_TOKEN_FILE` | 事前取得した短寿命アクセストークンによる XOAUTH2 |

現段階の `xoauth2` はアクセストークンを読み込んで IMAP 認証する境界までを提供する。更新トークンによる自動取得・更新と ISP 別エンドポイントプロファイルは未実装であり、期限前に外部の安全な仕組みで Secret を更新してワーカーを再起動する必要がある。Secret の内容は通常ログへ出力しない。

`PROCESSED_STATE=auto`では、INBOX選択時の`PERMANENTFLAGS`からユーザー定義IMAPキーワードの可否を判定する。利用可能なら`imap_keyword`、利用できなければ`local_database`を選択する。ローカルDBは`STATE_DIR/accounts/<account_id>/worker-state.sqlite3`へ保存され、`UIDVALIDITY`、フォルダー、UIDの組で処理済み状態と学習再開状態を分離する。方式を固定する場合は`imap_keyword`または`local_database`を明示する。

SpamAssassin の Bayes データは現在 `spamassassin-data` ボリュームを全アカウントで共有する。IMAP の処理状態は分離されるが、Bayes 学習データをアカウント単位に分離するモードは未実装である。異なる信頼境界の利用者を同じ構成へ収容しないこと。
