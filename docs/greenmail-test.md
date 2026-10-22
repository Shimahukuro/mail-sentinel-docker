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

### RoundcubeとPHPの更新確認

Roundcubeのバージョンと内蔵PHPのバージョンは別に確認する。定義更新後、既存のテスト環境では次のコマンドでイメージを取得し、Roundcubeを再作成する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml pull roundcube
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml up -d --no-deps --force-recreate roundcube
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml exec roundcube php -v
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml exec roundcube apache2ctl -M
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml port roundcube 80
```

この再作成手順はGreenMailが起動済みの環境を対象とする。初回は上記の通常起動手順を使う。

2026年10月22日、`1.7.5-apache`のarm64イメージ（ダイジェスト`sha256:734d9162c5225f4dcd744948ef915373afae6cece69bc1768b544615123ddc05`）でRoundcube 1.7.5、PHP 8.4.27、`php_module`を確認した。PHP 8.4.27は[CVE-2026-109618 / CVE-2026-109619の修正版](https://www.php.net/index.php)である。タグの内容は再ビルドで変わり得るため、別の取得時点・CPUアーキテクチャでも実際のPHPバージョンと配布元の修正状況を確認する。

再作成後はポートが`127.0.0.1:8081`のままであることと、Roundcubeへのログイン、メール表示・送受信を確認する。

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

### サイズ超過メールの保留・退避

`tests/test_oversized_mail.py`で、両処理済み方式、再起動、再試行、学習、サイズ境界とFETCH応答の違いを確認する。

```sh
python3 -m unittest discover -s tests -p test_oversized_mail.py -v
```

実IMAPでの確認は、workerが接続していない使い捨てのGreenMail専用インスタンスで実施する。次のテストは専用アカウント`test@test.local`のINBOXを初期化するため、通常利用中のGreenMailや実アカウントには使用しない。

```sh
docker run --rm -d --name mail-sentinel-oversize-test \
  -p 127.0.0.1:33143:3143 \
  -e 'GREENMAIL_OPTS=-Dgreenmail.setup.test.imap -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=test:greenmail@test.local -Dgreenmail.users.login=email' \
  greenmail/standalone:2.1.14
MAIL_SENTINEL_TEST_IMAP_PORT=33143 python3 -m unittest discover -s tests -p test_oversized_integration.py -v
docker stop mail-sentinel-oversize-test
```

起動完了後にテストを実行する。検査上限をテスト用の256バイトに下げ、超過メール2件の後の通常メールが検査されること、移動先未指定の保留、設定後の安全な移動、未検査ログ、DRY_RUNでの非変更を確認する。採点はスタブに置き換え、IMAPの取得・フォルダー作成・移動には実サーバーを使う。環境変数が未指定の場合、この統合テストはスキップする。

### 学習メールの移動

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
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml --profile tools run --rm admin --account test initial-scan preview --folder INBOX --since-date 2020-01-01 --through-date 2029-12-31 --timezone Asia/Tokyo --max-messages 10 --max-moves 1 --batch-size 1 --threshold 5.0
```

次を確認する。

- `target_count`が投入件数と一致する
- GTUBEだけが`scan_candidate`として表示される
- プレビュー後も両方のメールがINBOXにある
- 出力に`job_id`と`confirmation_token`がある

出力値を指定して適用し、GTUBEだけがJunkへ移動することを確認する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml --profile tools run --rm admin --account test initial-scan apply --job-id PREVIEW_JOB_ID --confirm CONFIRMATION_TOKEN
```

初期学習は、確認済みメールを専用フォルダーへコピーしてから同様にpreview、applyの順で確認する。apply後も元メールが同じフォルダーに残ることと、同じ範囲の再実行で`skipped_count`が増え、Bayes件数が再度増加しないことを確認する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml --profile tools run --rm admin --account test initial-learn preview --folder Learn-Ham --type ham --since-date 2020-01-01 --through-date 2029-12-31 --timezone Asia/Tokyo --max-messages 10 --batch-size 1
```

中断・再開、UIDVALIDITY変更、確認トークン、件数上限、重複防止の状態遷移は次の自動テストでも検証する。

```sh
python3 -m unittest -v tests/test_admin.py tests/test_imap_compat.py
```

IMAPS・STARTTLSの検証は、GreenMailの平文テストとは別に、次のTLS回帰テストで行う。Pythonに加えて`openssl`コマンドとループバック通信が必要になる。証明書とテスト用認証情報を一時生成し、外部接続や実メールは使用しない。

```sh
python3 -m unittest discover -s tests -p test_imap_tls.py -v
```

workerとadminの両方で、未信頼・期限切れ・ホスト名不一致の証明書を認証前に拒否し、独自CAを信頼した正しい証明書では接続できることを確認する。STARTTLS拒否時の停止、workerのXOAUTH2、CAファイルの異常、標準CAの利用、明示的な`none`の維持も対象とする。全回帰テストは`python3 -m unittest discover -s tests -v`で実行する。

テスト後にworkerを再開する。

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml start worker
```

## 停止と初期化

```sh
docker compose -f docker-compose.yml -f docker-compose.greenmail.yml down
```

GreenMailのメールはコンテナ内だけに保存されるため、コンテナを削除すると初期化される。SpamAssassinのBayesデータも初期化する場合は、名前付きボリュームの削除を伴うため、対象を確認してから明示的に行う。
