# Mail Sentinel 最小 PoC セットアップ

このPoCは、1つのIMAPメールボックスを定期確認し、SpamAssassinがスパムと判定したメールを同じアカウントのJunkフォルダへ移動する。

初期設定後の日常操作、ログの読み方、ルール更新、障害対応については[ユーザーガイド](user-guide.md)を参照する。

## 前提条件

- Docker EngineとDocker Composeが利用できる
- IMAPS（通常はTCP 993）を利用できる
- IMAPサーバーがユーザー定義キーワードをサポートする
- `INBOX`と移動先のJunkフォルダが既に存在する
- パスワード認証またはアプリパスワード認証を利用できる

この段階ではOAuth 2.0、学習フォルダ、初期スキャン、複数アカウントには対応しない。

## 設定

環境設定のひな型をコピーする。

```sh
cp .env.example .env
```

`.env`のIMAPホスト、ユーザー名、フォルダ名などを環境に合わせて変更する。

初回接続では`DRY_RUN=true`のまま起動する。このモードでは判定とログ出力だけを行い、メールの移動やIMAPキーワードの付与は行わない。ログを確認してから`DRY_RUN=false`へ変更する。

次に、IMAPパスワードだけを含むSecretファイルを作成する。

```sh
mkdir -p secrets
printf '%s' 'your-app-password' > secrets/imap_password
chmod 600 secrets/imap_password
```

`.env`と`secrets/imap_password`はGitの管理対象外である。

## 起動前の安全確認

最初は誤判定の影響を限定するため、SpamAssassinの`required_score`を高めに設定することを推奨する。設定場所は`spamassassin/local.cf`である。

また、メールプロバイダー上で次を確認する。

1. `.env`の`IMAP_JUNK`が実際の迷惑メールフォルダ名と一致する。
2. IMAPユーザー定義キーワードを利用できる。
3. 最初はテスト用メールボックスまたは少数のメールで試す。

## ビルドと起動

```sh
docker compose build
docker compose up -d
docker compose logs -f worker
```

workerは通常監視を始める前に、IMAP認証、INBOXとJunkの参照、INBOXの読み取り、SpamAssassinへの接続を診断する。診断に失敗した場合はメールを変更せず終了し、Composeの再起動設定に従って再試行する。診断だけを手動実行する場合は次を使用する。

```sh
docker compose run --rm worker imapfilter -c /etc/mail-sentinel/diagnose.lua
```

ログは1行1イベントのJSON形式で出力する。本文、パスワード、完全なアカウント名は記録しない。`message_classified`ではUID、スコア、判定、実行または予定された操作を確認できる。

正常メールには`MailSentinelChecked`キーワードが付く。スパムメールはJunkへ移動する。メール本文と認証情報は通常ログへ出力しない。

既定では、PoC開始時に過去のメールを一括処理しないよう、当日から1日前までに届いたメールだけを対象とする。対象期間は`.env`の`LOOKBACK_DAYS`で変更できる。

停止する場合は次を実行する。

```sh
docker compose down
```

Bayesデータは名前付きボリュームに残る。データも削除したい場合だけ、影響を確認したうえでボリュームを明示的に削除する。

## SpamAssassinルールの更新

ルール更新は自動実行せず、管理者が次のコマンドで明示的に実行する。

```sh
docker compose run --rm spamassassin update-rules
docker compose restart spamassassin
```

更新コマンドは`sa-update`実行後に`spamassassin --lint`を行う。更新または検証に失敗した場合は異常終了し、稼働中のSpamAssassinは再起動しない。更新済みルールは`spamassassin-rules`ボリュームへ保存される。

## 現段階の制約

- IMAPキーワード非対応のサーバーでは正常メールの処理済み管理ができない。
- `LOOKBACK_DAYS`より前に届いた未処理メールは通常監視の対象外になる。
- SpamAssassinとの通信に失敗したメールは移動せず、次回以降に再試行する。
- `spamc`の最大サイズを超えるメールは安全側で処理を保留する。
- IMAPの接続・操作エラー時にはworkerが待機時間を段階的に増やしながら再試行する。
- SpamAssassinルールの定期的な自動更新はまだ実装していない。
- 再試行間隔は`RETRY_INITIAL_SECONDS`から始まり、連続失敗時に`RETRY_MAX_SECONDS`まで段階的に増加する。
- メール本文をコンテナ内へ永続保存しない。
