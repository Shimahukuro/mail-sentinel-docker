# GreenMailによるローカルテスト

GreenMailを使うと、実際のメールアカウントを操作せずにMail Sentinelの基本動作を確認できる。テスト環境は外部へメールを配送せず、SMTPとIMAPのポートはホストのループバックアドレスだけに公開する。

## 起動

通常のComposeファイルへGreenMail用の上書き設定を追加して起動する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml up -d --build
```

GreenMail用設定では統合テストのため`DRY_RUN=false`を明示している。実メールサーバーへ接続する通常設定の既定値は`true`である。

テスト専用の`greenmail-setup`サービスが、workerの起動前にGreenMail上へ`Junk`、`Learn-Ham`、`Learn-Spam`を作成する。本番用Composeは学習フォルダーを自動作成しない。

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

- 正常メールでは`message_classified`の`classification`が`ham`となり、INBOXに残る。
- GTUBEメールでは`message_classified`の`classification`が`spam`となり、Junkへ移動する。
- メール本文とパスワードはログへ出力されない。

RoundcubeまたはThunderbirdなどからIMAPへ接続すれば、INBOXとJunkの状態を目視できる。ローカルテストではCompose内部の閉じたネットワークを使うため平文IMAPを使用する。本番用の既定設定はIMAPSと証明書検証のまま変更されない。

## フィードバック学習の統合テスト

Roundcubeでフォルダー一覧を更新すると、`Learn-Ham`と`Learn-Spam`が表示される。

1. 正常メールをJunkから`Learn-Ham`へ移動する。
2. workerの次回監視後、メールがINBOXへ移動することを確認する。
3. GTUBEメールをINBOXから`Learn-Spam`へ移動する。
4. workerの次回監視後、メールがJunkへ移動することを確認する。
5. workerログで`learning_succeeded`と`learning_move`を確認する。

GTUBEが通常判定で既にJunkへ移動している場合は、RoundcubeでいったんINBOXへ戻してから`Learn-Spam`へ移動する。

ログには学習種別、UID、成功・失敗、移動先と集計件数が記録される。本文、件名、送信者、パスワードは記録されない。

### 失敗と再試行

学習対象をフォルダーへ置いた直後にSpamAssassinを停止する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml stop spamassassin
```

workerログに`learning_failed`と`"retry":true`が出て、対象メールが学習フォルダーに残ることを確認する。SpamAssassinを再開すると次回監視で学習される。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml start spamassassin
```

### 重複学習の防止

学習後のメールを同じ学習フォルダーへもう一度移動する。`MailSentinelLearned`キーワードが保持されていれば、`learning_succeeded`を再出力せず、移動だけが行われる。

### Bayesデータの永続化

学習後にBayes件数を確認し、SpamAssassinコンテナを再作成してから再度確認する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml exec spamassassin sa-learn --dump magic
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml up -d --force-recreate spamassassin
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml exec spamassassin sa-learn --dump magic
```

ham/spamの学習件数が再作成前後で維持されることを確認する。`spamassassin-data`名前付きボリュームはコンテナ再作成では削除されない。

`sa-learn --dump magic`の`nham`はham学習数、`nspam`はspam学習数を示す。SpamAssassinの既定では、`nham`と`nspam`が**それぞれ200以上**になるまでBayes判定は通常の採点へ参加しない。GreenMailの1件ずつのfixtureは学習処理、重複防止、永続化を確認するためのものであり、`BAYES_*`ルールの発火確認には不足する。200件は分類器を有効にする最低条件であり、精度保証ではない。

## 初期学習・初期スキャン管理ジョブ

通常workerによる先行処理を避けるため、テストメールを投入する前にworkerを停止する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml stop worker
```

正常メールとGTUBEメールをINBOXへ投入し、初期スキャンをプレビューする。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml --profile tools run --rm admin initial-scan preview --folder INBOX --since-date 2020-01-01 --through-date 2029-12-31 --timezone Asia/Tokyo --max-messages 10 --max-moves 1 --batch-size 1 --threshold 5.0
```

次を確認する。

- `target_count`が投入件数と一致する
- GTUBEだけが`scan_candidate`として表示される
- プレビュー後も両方のメールがINBOXにある
- 出力に`job_id`と`confirmation_token`がある

出力値を指定して適用し、GTUBEだけがJunkへ移動することを確認する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml --profile tools run --rm admin initial-scan apply --job-id PREVIEW_JOB_ID --confirm CONFIRMATION_TOKEN
```

初期学習は、確認済みメールを専用フォルダーへコピーしてから同様にpreview、applyの順で確認する。apply後も元メールが同じフォルダーに残ることと、同じ範囲の再実行で`skipped_count`が増え、Bayes件数が再度増加しないことを確認する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml --profile tools run --rm admin initial-learn preview --folder Learn-Ham --type ham --since-date 2020-01-01 --through-date 2029-12-31 --timezone Asia/Tokyo --max-messages 10 --batch-size 1
```

中断・再開、UIDVALIDITY変更、確認トークン、件数上限、重複防止の状態遷移は次の自動テストでも検証する。

```sh
python3 -m unittest -v tests/test_admin.py tests/test_imap_compat.py
```

テスト後にworkerを再開する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml start worker
```

## 停止と初期化

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml down
```

GreenMailのメールはコンテナ内だけに保存されるため、コンテナを削除すると初期化される。SpamAssassinのBayesデータも初期化する場合は、名前付きボリュームの削除を伴うため、対象を確認してから明示的に行う。
