# Mail Sentinel 最小 PoC セットアップ

このPoCは、1つのIMAPメールボックスを定期確認し、SpamAssassinがスパムと判定したメールを同じアカウントのJunkフォルダへ移動する。

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

正常メールには`MailSentinelChecked`キーワードが付く。スパムメールはJunkへ移動する。メール本文と認証情報は通常ログへ出力しない。

既定では、PoC開始時に過去のメールを一括処理しないよう、当日から1日前までに届いたメールだけを対象とする。対象期間は`.env`の`LOOKBACK_DAYS`で変更できる。

停止する場合は次を実行する。

```sh
docker compose down
```

Bayesデータは名前付きボリュームに残る。データも削除したい場合だけ、影響を確認したうえでボリュームを明示的に削除する。

## 現段階の制約

- IMAPキーワード非対応のサーバーでは正常メールの処理済み管理ができない。
- `LOOKBACK_DAYS`より前に届いた未処理メールは通常監視の対象外になる。
- SpamAssassinとの通信に失敗したメールは移動せず、次回以降に再試行する。
- `spamc`の最大サイズを超えるメールは安全側で処理を保留する。
- IMAPの接続・操作エラー時にはworkerコンテナが終了し、Composeの再起動設定によって再試行する。
- SpamAssassinルールの更新はまだ自動化していない。
- メール本文をコンテナ内へ永続保存しない。
