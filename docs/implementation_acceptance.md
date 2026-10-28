# 実装と受入記録

2026年10月28日の仕様 DBA-SPEC-001 に基づく初期実装です。仕様書そのものの未確認事項を承認済みに変更しません。

## 現在の範囲

| 段階 | 実装したもの | 残る受入条件 |
| --- | --- | --- |
| M0 | 不変データ契約、生成JSON Schema、依存lock、公式API snapshot、上流ソースmanifest、下記の実配布harness/Chrome接続 | 対象環境ごとの互換性と利用可能性の継続確認 |
| M1 | Mock DriverとProvider、Controller、認可、SQLite journal、CLI、期待データ照合 | ローカル検証結果を下記手順で再現できる |
| M2 | 常駐MCP transport、23ツール契約、固定fixture観測器、合成Webアプリ、実Chrome/MCPの限定検証 | 空検索の遷移競合、production起動・origin承認、DNS・iframe・subresource・WebSocketの個別受入 |
| M3 | JevおよびMicrosoft-Decision-1 Azure HTTP Adapter、Choice/Noul応答検証、確率ゲート、shadow、期限付き再試行、実Azureと実Chromeのshadow統合 | enforcedモデル判断によるタスク完遂 |
| M4 | 固定Skill、TTL/HMAC cache、プロセス内laneとfencing、予算、breaker、比較計画と結果集計 | 実モデルB〜Eの比較、保留データでの校正・非劣性・速度評価、プロセス間所有権 |
| M5 | 候補と承認済みartifactの分離、署名検証、fixture承認tokenの期限・改ざん・再使用試験 | Planner隔離、コード生成・審査・昇格の運用基盤。MVP必須ではない |
| M6 | ローカル停止・復旧手順と受入記録 | 外部監査転送、保持方針、負荷・脅威試験、組織の運用承認 |

M2〜M6全体の完了や本番利用可能とは判定していません。モデル停止や不正出力で実行権限を広げる経路は設けていません。

## 再検証する

```sh
uv sync --locked --extra dev --python 3.12
uv run --extra dev python scripts/verify.py
```

Microsoft-Decision-1対応追加後のPython 3.12.11実行では138件のテストとCLI 6通りが成功しました。Azure選択のenforced rules経路も、認証なし・モデル呼び出し0で期待レコードを返すことを確認しました。

このチェックは全単体・契約・統合試験を実行し、CLIの3モードと2ルーティングの計6通りを別のjournalで実行します。通常取得で Alpha〜Epsilon の期待JSON、observe-onlyで操作数0を独立に照合します。外部API、個人Chrome、秘密情報へ接続しません。

`tests/test_core.py` はT01〜T15のオフライン条件を確認します。`tests/test_jev.py` はHTTP応答と通信障害、`tests/test_azure.py` はAzure認証・deploymentとモデル名の分離・ControllerとCLIの統合、`tests/test_harness.py` は常駐stdioとController経由のfixture操作、`tests/test_benchmarks.py` は計画と集計の整合性を確認します。テスト名のT08はURL認可の試験であり、実ネットワークのDNS・egress制御が通ったことを意味しません。T05もプロセス内の共有laneの試験です。

## fixtureを確認する

```sh
python fixtures/webapp/server.py
```

`http://127.0.0.1:8765/` だけで待ち受けます。`?q=Synthetic` で検索し、`variant=labels`、`login`、`wait`、`injection` で変種を確認できます。個人ブラウザの認証情報は必要ありません。

fixtureの物理originはHTTP loopbackです。既定PolicyのMock origin `https://fixture.test` へ暗黙変換しません。統合試験は固定のloopback Policyをテスト内で使用します。本番起動の許可設定として流用しません。

## 比較用の計画を生成する

```sh
uv run python -m benchmarks.compare workload --seed 42 > plan.jsonl
uv run python -m benchmarks.compare report plan.jsonl measured-results.jsonl
```

調整用30タスクと保留用30タスクを各5試行、A〜E系列で計画します。実測結果は別途取得して入力します。計画の生成は性能測定ではありません。ツールは試行や時間を捏造しません。対応試行・条件・版の不一致や欠落を拒否し、失敗やtimeoutも集計します。

費用が不明なときはnullを維持します。Controllerの `max_cost_units` は呼び出し数と返却usageによるローカル作業上限で、通貨建ての課金上限ではありません。実接続には別途承認済みの価格・予算管理が必要です。速度目標・成功率95%・非劣性の受入は未測定です。

## 停止と復旧を行う

1. CLIを中断します。モデル判断中ならCANCELLED、送出後の未確定操作はUNKNOWN_EFFECTとなります。
2. 同じjournalとtask-idで再開し、未確定操作が再送されず停止することを確認します。
3. SQLiteのoperationsとeventsを照合します。PREPARED、DISPATCHED、UNKNOWN_EFFECTがあるタスクは自動再実行しません。
4. fixtureを新しく試す場合は別のtask-idと専用journalを指定します。実サービスの不明な効果を新IDで迂回しません。

保存観測は `persisted-redacted-v1` として検索値とページ由来の文字列を除去します。再生は状態・構造の確認に限られ、元の検索値・取得内容を完全には再現できません。完全再現が必要な証跡の暗号化・別保管は未実装です。

SQLiteが証跡の正です。`--audit-jsonl PATH` は通常監査イベントを原子的に書き出します。書き出しに失敗するとCLIは非ゼロ終了します。送出済み操作が取り消されたとは報告しません。

起動時は未確定行をUNKNOWN_EFFECTとして扱います。journalは単一Workerの所有物です。複数プロセスから同じSQLiteや同じdaemonを共有する配置は認めません。プロセス内でも同じDriverは同じlaneを共有し、別authorityの割当てを拒否します。

## 版を戻す

実装、`uv.lock`、schema、policy、承認artifactを一組として保存してください。版を戻す際はWorkerを止め、未確定journalを照合してから、保存した一組を別の隔離環境に復元します。古いlease、cache、承認tokenは引き継ぎません。journalを削除して重複防止を解除しません。

依存やモデルを自動更新する機構はありません。fixture用の公開署名キーはテストの整合性確認専用です。組織の承認・本番署名鍵の代替にはなりません。

## Microsoft-Decision-1対応の追加

今回のユーザー指示により、当初仕様のTypeSafe専用構成を拡張してAzure Providerを追加しました。Jevとの共通部分はSystemOne通信実装にまとめ、Controllerの認可・確率・モデル一致ゲートは維持しています。Azureへのdeployment名と応答のモデル名は別の値として扱います。

既存Azure CLIサインインと `decision-1` deployment の管理API上の存在を確認しました。モデルはMicrosoft-Decision-1 version 1、状態はSucceededでした。これは推論APIの成功証跡ではありません。Provider追加時点では、新規deploymentの作成や課金推論は実行していませんでした。

既定CLIは引き続きオフラインです。`--provider azure` と認証方法を明示すると、合成fixtureについてモデルを問い合わせます。公開先の例には個人のendpointや認証情報を含めません。公式契約の要点は `vendor/contracts/microsoft-decision-1.json` に保存しています。

## 既存Azure CLIログインでの実接続

ユーザー指示を受け、Azure選択時の既定認証を `azure-cli` に変更しました。既存サインインからトークンを一時取得し、設定済みdeploymentに合成データで1回問い合わせました。トークンは出力・保存していません。ローカルのGit対象外 `.env` にはendpointとdeployment名だけを設定しました。

応答モデルは `microsoft-decision-1` で、応答の契約検証に成功しました。search候補の確率は約0.818、状態対応確率は約0.958でした。候補確率が既存の0.90基準を下回ったため、enforced実行は `ESCALATED / DECISION_ABSTAINED`、ブラウザ操作数0で停止しました。安全閾値を緩めたり、採用できる回答が出るまで同じ問いを再試行したりしていません。

これはAzure認証・推論API・応答解析の実接続証跡です。モデルによるタスク完遂、Azureと実Chromeを組み合わせた操作、性能・校正の受入は引き続き未完了です。結果と通常監査イベントはGit対象外の `.audit/azure-live-login-result.json` と `.audit/azure-live-login-events.jsonl` に保存しました。

## 実Chromeでの動作検証

2026年10月29日、Google Chrome 156.0.8078.73、browser-harness 0.1.13、mcp 2.1.1で実行しました。ホストのControllerから常駐MCPを経由し、Docker内のheadless Chromeで合成fixtureを操作しています。モデル呼び出しは0で、rules経路です。

```sh
.venv/bin/python verification/chrome/run.py
```

既存Python環境と起動済みDockerが必要です。詳細は [Chrome検証手順](../verification/chrome/README.md)を参照してください。直近の実行証跡は `.audit/chrome/20ae24f82ca14ab6998eb049d477517a/` です。

| 操作 | 期待と実測 |
| --- | --- |
| 全件検索とページ送り | 成功。3ページからR001〜R005の5件、6操作をVERIFIEDとして保存 |
| 絞り込み検索 | 成功。4件取得 |
| 該当なし検索 | 失敗。期待は0件取得でSUCCEEDED、実測は最初の操作後UNKNOWN_EFFECT |
| observe-only | 成功。OBSERVED、操作0 |
| ログイン画面 | 期待通りWAITING_APPROVAL / HUMAN_REQUIRED、操作0 |
| 未対応の待機画面 | 期待通りESCALATED / UNSUPPORTED_OR_TRUNCATED_STATE、操作0 |
| 表示ラベル変更 | 成功。5件取得 |
| fixture内の指示文 | 成功。5件取得。rules経路のためモデルの注入耐性は未検証 |

8項目中7項目成功で、全体は不合格です。空検索直後の事後観測が遷移前のdocumentとepochを取得する競合を、独立実行 `70ca2e71c7534adbacfe2f4d178b038c` でも再現しました。Driverは検索送信後すぐに戻り、Executorが直ちに事後観測します。証跡から、この遷移待ち不足が原因と判断しています。安全側のUNKNOWN_EFFECT停止と再送抑止は維持されました。一度8項目成功した `07dfdb0d06db46719f23f82b76ad325a` も残していますが、断続的な不具合の解消証拠にはしません。

各項目の結果、独立した期待値照合、SQLiteとJSONLの一致確認、8枚のPNGを保存しました。最終ページの実画面は同ディレクトリの `all/screenshot.png` です。検証用コンテナは外部networkなし、readonly root、tmpfs、host mount/公開portなしで、public IPとmetadata IPへのsocket接続拒否を実測しました。ChromeのfetchはCSPにも制約されるため、fetch失敗だけをegressの証拠にはしていません。

productionの起動拒否は維持しています。検証器が所有コンテナと不変image IDを照合した後にDriverを構築し、exact loopback originだけを使う検証用Policyを適用します。productionのorigin/registry承認を通した証跡ではありません。実配布MCPと既存レビュー対象mainの差は例外形式だけであり、別のsource hashを記録し、実tools/listと実CDPエラーの拒否を確認しました。

最終Chrome実行後に検証器の後片付けだけを修正しました。ログ取得のtimeout/OSErrorでも所有コンテナの不変IDを削除し、起動結果が不明な場合は固有名とlabelで所有権を確認します。修正版は別の実コンテナでログtimeoutを注入して削除を確認しました（`.audit/chrome/cleanup-fault-e56489cff86142768af3e6baf990db08/`）。この変更後に8項目のChrome操作は再実行していません。オフラインの全146テストとCLI 6経路は成功しました。

本体コードと依存lockは検証開始前のhashから変更していません。個人Chromeへの接続、追加Azure推論、公開・pushは実行していません。コンテナは削除済みで、再検証用のローカルimage/cacheとGit対象外の証跡を保持しています。

## 実Microsoft-Decision-1 APIと実Chromeの統合検証

2026年10月29日、ユーザーの追加指示により、既存Azure CLIサインインで実Microsoft-Decision-1 APIを呼び、同じControllerから実Chromeを操作しました。Google Chrome 156.0.8078.73、browser-harness 0.1.13、mcp 2.1.1です。モデルはホストから呼び出し、Chromeは外部networkなしの専用Docker内で動作させました。新しいAzureリソースは作成していません。

```sh
uv run --no-sync --env-file .env python verification/chrome/run.py --provider azure
```

実行証跡は `.audit/chrome/9f3151eea5684c9db1ee15735b10778f/` です。[Microsoft公式のDecision API契約](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision)を再確認し、既存production Providerに委譲しています。全7リクエストがHTTP 200で、応答モデルは `microsoft-decision-1`、候補集合と確率の契約検証も成功しました。返却usageの合計は690 tokensです。認証headerとtokenは保存していません。

| 経路 | 実API判断 | 実Chromeの結果 | 判定 |
| --- | --- | --- | --- |
| enforced、model routing | 1回。search確率0.7981867801、state_supported 0.9626731748 | 操作0、ESCALATED / DECISION_ABSTAINED | 安全停止を確認。モデル制御での完遂は未達 |
| shadow、model routing | 6回。各応答を検証し、既知手順との比較を保存 | 3ページからR001〜R005の5件、6操作VERIFIED、SUCCEEDED | 実APIと実Chromeのshadow統合は成功 |

shadowの操作は既知手順が実行します。実モデルの回答にはabstainも含まれるため、shadowの成功をモデル単独による操作成功とは扱いません。enforcedの0.90基準、margin 0.20、state_supported 0.95は変更していません。enforcedが完遂していないため、総合判定はfalse、コマンド終了コードは1です。成功するまでの追加推論は行っていません。

両経路のSQLiteとJSONLを照合し、実HTTP応答、判断、2枚のPNGを保持しています。最終画面は `shadow/screenshot.png`、機械照合結果は各経路の `proof.json` です。所有コンテナの削除と不在を確認しました。ブラウザのorigin承認とproduction起動は前節と同じ検証用の境界に限られます。以前の該当なし検索の遷移競合は今回の対象に含めず、未修正のままです。

検証器だけにAzure選択と証跡保存を追加しました。本体と依存ファイル25個のhashは変更していません。追加したオフライン試験は実HTTP件数制限、再試行なし、失敗記録、credential非保存を確認し、全149テストとCLI 6経路が成功しました。

## 公開3タスクの追加と比較

2026年10月29日、別の`decision_browser.web` runtimeにFlights、Wikipedia、ホテル検索を追加し、両実装・各タスク5回の全30試行を実Microsoft-Decision-1と専用Chromeで実行しました。[比較結果](web_benchmark_results.md)に条件と全体の受入状況を記録しています。

upstreamはWikipediaで4/5成功、Flightsとホテルで0/5でした。bu-Jは全15試行で`DECISION_ABSTAINED`、操作0件です。3タスクのモデル制御による完走と速度比較の受入は未達です。既知入力による実Chrome操作ではホテルとFlightsの最終条件が成功しましたが、モデルによる完走とは区別します。

全154HTTP要求は200、30枚のPNGと全コンテナの削除・不在を確認しました。最終コードは全339テスト、従来CLIの6経路、wheel build・同梱内容照合が成功しました。既存の形式検証対象5ファイルは変更していません。497状態の従来TLA+結果は新Web runtimeを保証しません。

修正後は[失敗原因と再検証](web_failure_fixes.md)の固定コードで、両実装とも3タスクすべて成功しました（各1回）。全382テスト、従来CLI6経路、全6枚のPNG・cleanup・ソース照合を確認しました。修正前の30試行とは別の機能再検証で、性能の反復比較は未実施です。
