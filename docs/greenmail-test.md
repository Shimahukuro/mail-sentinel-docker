# GreenMailによるローカルテスト

GreenMailを使うと、実際のメールアカウントを操作せずにMail Sentinelの基本動作を確認できる。テスト環境は外部へメールを配送せず、SMTPとIMAPのポートはホストのループバックアドレスだけに公開する。

## 起動

通常のComposeファイルへGreenMail用の上書き設定を追加して起動する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml up -d --build
```

GreenMail用設定では統合テストのため`DRY_RUN=false`を明示している。実メールサーバーへ接続する通常設定の既定値は`true`である。

テスト用アカウントは次のとおり。

| 項目 | 値 |
| --- | --- |
| メールアドレス・ログイン | `test@test.local` |
| パスワード | `greenmail` |
| ホストからのSMTP | `127.0.0.1:3025` |
| ホストからのIMAP | `127.0.0.1:3143` |
| Webメール | `http://127.0.0.1:8081` |

この認証情報はローカルの隔離されたテスト環境専用であり、本物のアカウントには使用しない。

## Roundcubeでメールを確認する

ブラウザで[Roundcube](http://127.0.0.1:8081)を開き、次の情報でログインする。

```text
ユーザー名: test@test.local
パスワード: greenmail
```

正常メールはINBOXに残り、GTUBEメールはworkerの処理後にJunkへ表示される。新着状態が反映されない場合は、Roundcubeの更新ボタンでフォルダ一覧を再読み込みする。

Roundcubeはこのローカルテスト環境専用であり、HTTPポートは`127.0.0.1`だけに公開する。外部ネットワークや本番環境へ公開しない。

## テストメールの投入

正常メールとSpamAssassin公式のGTUBEメールをfixtureとして用意している。`curl`がSMTPに対応している環境では次のように投入できる。

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

送信後、workerログを確認する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml logs -f worker
```

- 正常メールでは`ham checked`と表示され、INBOXに残る。
- GTUBEメールでは`spam moved`と表示され、Junkへ移動する。
- メール本文とパスワードはログへ出力されない。

RoundcubeまたはThunderbirdなどからIMAPへ接続すれば、INBOXとJunkの状態を目視できる。ローカルテストではCompose内部の閉じたネットワークを使うため平文IMAPを使用する。本番用の既定設定はIMAPSと証明書検証のまま変更されない。

## 停止と初期化

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml down
```

GreenMailのメールはコンテナ内だけに保存されるため、コンテナを削除すると初期化される。SpamAssassinのBayesデータも初期化する場合は、名前付きボリュームの削除を伴うため、対象を確認してから明示的に行う。
