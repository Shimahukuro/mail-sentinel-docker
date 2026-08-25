# Changelog

このファイルには、Mail Sentinel Dockerの主な変更内容を記録します。

バージョン番号は[Semantic Versioning](https://semver.org/)に準拠します。`0.x`は開発初期段階を表し、設定形式や動作が将来のリリースで変更される可能性があります。

## [Unreleased]

## [0.1.1] - 2026-08-25

Mail Sentinel Dockerの安全なIMAP移動方式、更新通知、導入ドキュメントを強化したメンテナンスリリースです。

### 追加

- GitHub Releasesの最新安定版を定期確認し、更新可能なリリースを既存Webhookへ1回通知する仕組みを追加
- 稼働バージョンを起動ログ、永続状態、監査イベントへ記録し、障害調査時に確認できるよう改善
- `MOVE`非対応かつ`UIDPLUS`とユーザー定義キーワードを利用できるIMAPサーバー向けに、明示的な`IMAP_MOVE_FALLBACK=auto`で有効になる再開可能な`COPY`＋`UID EXPUNGE`フォールバックを追加
- 移動方式を起動前診断へ追加し、安全な方式がない本運用では起動を停止

### 改善

- 設定したJunkフォルダーをSpecial-Use自動検出より優先し、プロバイダー固有フォルダーを確実に選択
- メール件名、送信者、実フォルダー名、接続先を運用ログへ出さないよう移動関連ログを匿名化
- Gmailアプリパスワードの取得・設定手順をユーザーガイドへ追加
- 最短導入手順をまとめたクイックスタートとGitHub DiscussionsのQ&Aフォームを追加
- 脆弱性の非公開報告先、対応方針、サポート対象を定めたセキュリティポリシーを追加

### バージョンアップ時の注意

- イメージを再ビルドし、workerを再作成してから起動前診断を実行してください。
- `COPY`＋`UID EXPUNGE`フォールバックは自動では有効になりません。必要なアカウントだけ`IMAP_MOVE_FALLBACK=auto`を明示してください。
- 更新通知を使用する場合は汎用Webhookを設定し、`NOTIFICATION_UPDATE_ENABLED=true`を指定してください。

## [0.1.0] - 2026-08-23

Mail Sentinel Dockerの最初のPoCリリースです。既存のメール配送経路を変更せず、IMAPメールボックスをSpamAssassinで検査して迷惑メールをJunkフォルダーへ移動します。

### 主な機能

- 単一または複数のIMAPアカウントを定期監視
- SpamAssassinによるメールの採点とスパム判定
- スパムメールをJunkフォルダーへ移動
- 正常メールをINBOXに保持し、処理済み状態を記録
- IMAPキーワード非対応環境向けのSQLiteフォールバック
- `Learn-Ham`と`Learn-Spam`フォルダーによるフィードバック学習
- 既存メールを使ったBayes初期学習
- 既存INBOXを対象とする確認付き初期スキャン
- IMAP Special-Use、Modified UTF-7、`UTF8=ACCEPT`への互換対応
- パスワード、アプリパスワード、事前取得済みアクセストークンによるXOAUTH2認証
- 起動前診断とメールボックスを変更しないドライラン
- アカウント別プロセス、状態、ロック、再試行の分離
- JSON形式の運用ログ、障害通知、状態確認用管理コマンド
- SpamAssassinルールとBayesデータを含む永続データのバックアップ、検証、復元
- GreenMailとRoundcubeを利用したローカル統合テスト環境
- Docker Composeによるコンテナの構築、起動、永続化

### セキュリティと安全性

- IMAP認証情報をDocker Secretとして読み込み
- 通常コンテナを非root、読み取り専用ファイルシステム、Capability破棄、`no-new-privileges`で実行
- SpamAssassinサービスをホストへ直接公開しない構成
- ログ、通知、監査記録からメール本文、認証情報、完全なアカウント名を除外
- 初回導入向けの`DRY_RUN=true`初期設定
- メールを自動削除せず、判定不能または処理失敗時はINBOXへ残して再試行
- Secret scanningとDependabotの利用を想定した公開リポジトリ設定

### 既知の制約

- 本リリースはPoCであり、本番利用前のバックアップと環境ごとの検証が必要
- OAuth 2.0アクセストークンの取得、自動更新、再認可は未実装
- XOAUTH2では外部で事前取得したアクセストークンが必要
- Web管理画面は未実装
- メールの自動削除には未対応
- フィッシングやUnicode難読化メールに対する追加ルールは今後強化予定
- 複数利用者または組織を収容する場合のBayesデータ分離は未実装
- IMAPサーバーごとの仕様差について、導入先で互換性確認が必要

### 導入時の注意

実メールボックスへ接続する前にバックアップを取得し、最初は必ず`DRY_RUN=true`で起動してください。起動前診断、Junkフォルダー名、判定ログを確認した後に通常運転へ切り替えてください。

詳細な導入方法は[README](README.md)と[ユーザーガイド](docs/user-guide.md)を参照してください。

[Unreleased]: https://github.com/Shimahukuro/mail-sentinel-docker/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Shimahukuro/mail-sentinel-docker/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Shimahukuro/mail-sentinel-docker/releases/tag/v0.1.0
