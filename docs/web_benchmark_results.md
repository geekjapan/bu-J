# 実API・実Chromeの比較結果

2026年10月29日（JST）に30試行を完了しました。**速度比較は判定不能です。** bu-Jは15試行すべてで操作前に棄権し、モデル制御による完走の受入は未達です。短い棄権時間を高速化として扱いません。

これは修正前の記録です。[失敗原因と修正後の検証](web_failure_fixes.md)は別に記録しています。

## 成功率と成功時の時間

両実装で各タスク5回ずつ実行しました。upstreamは固定版のAgent loopに共通のAzure・ブラウザadapterを接続した比較対象です。

| タスク | upstream成功 | upstream成功時の中央値・範囲 | bu-J成功 | bu-J成功時の時間 |
| --- | --- | --- | --- | --- |
| Google Flights | 0/5 | 算出不可 | 0/5 | 算出不可 |
| Wikipedia | 4/5 | 3.937秒、3.679–4.088秒（成功4件） | 0/5 | 算出不可 |
| ホテル検索 | 0/5 | 算出不可 | 0/5 | 算出不可 |

停止理由は次のとおりです。

- upstream Flightsはカレンダー操作中にBLOCKEDを選び、指定日の結果一覧に到達しませんでした。
- upstream Wikipediaは4回、目的の記事URLと本文の独立検証に成功しました。1回はSearch実行後の`StalePage`で停止しました。
- upstream ホテルは5回とも検索確定を飛ばしてCasa Floraを開き、DONEを選びました。検索条件の独立検証が不合格でした。
- bu-Jは全15回`DECISION_ABSTAINED`でした。state supportはFlightsで0.500–0.562、Wikipediaで0.500–0.562、ホテルで0.407–0.438と、基準0.95を下回りました。journalの操作行は全試行で0件です。operation・targetの確率0.90、margin 0.20も維持しています。

## 公開測定との関係

[固定版の公開資料](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/docs/performance.md)には、Flights録画7.073秒、Wikipedia smoke 2.798秒、ホテルsmoke 1.896秒が記載されています。これらは今回の5回反復の中央値ではありません。

[公開されたmatched Flights比較](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/docs/full-speed-measurement.json)は、旧実装の中央値9.450秒から最適化版7.092秒への変化で、各3回成功した結果です。今回のupstream側は最適化版の固定Agent loopです。公開資料内の旧実装との再測定ではありません。

今回の条件は以下で共通化しました。

| 条件 | 今回 |
| --- | --- |
| upstream commit | `1231850a0bf1a0c0341fe408ef1668dbbfdfac46` |
| 判断モデル | Azure `microsoft-decision-1` |
| 文字列生成 | Azure `gpt-4.1-mini-2025-04-14` |
| Flights | Zurich→London、2027-09-20、片道、1人、Economy |
| ブラウザ | 実Google Chrome `156.0.8078.73`、headless、1120×780 |
| profile | 各試行で新規の専用profile・コンテナ |
| 順序 | タスク内でAB、BAを交互、各側5回 |
| 計時 | 初期観測後の最初の判断からfreshなDONEまで。起動・独立最終検証・画像取得は除外 |

公開測定はTypeSafe jev-1.13.0、Mercury、Chrome 152の既存profile、Flightsは2026-09-20です。今回の両実装には同じサイト・control制限、Azure応答検証、singleton target用の「該当なし」を適用しました。共通adapterと追加の独立検証も公開測定との差です。歴史的な結果との速度比は算出しません。[実行手順と境界](web_benchmark.md)に詳細を記載しています。

## 実際に実行したこと

本測定の実HTTP要求は判断134回、文字列生成20回、全154回がHTTP 200でした。応答が報告した合計は281,827 tokensです。料金は計算していません。30試行すべてで画像と独立最終検証を保存し、専用コンテナの削除・不在を確認しました。30個のChromeは同じplatform imageを使用しました。

成功したWikipediaでは、計時区間の合計15.641秒に対しモデル・helperのHTTP待ちが10.223秒、約65%でした。通信待ちが大きいことは観測できましたが、Chromeを含むCPU profileは取得していません。開始時は14 core、1分load average 4.55で、他のアプリも稼働していました。成功数不足、未校正の採用基準、異なる過去条件から、実装一般の速さやモデル能力の優劣は結論できません。

測定とは別に、既知入力による実Chromeの操作確認を行いました。ホテルは5操作と最終5条件、Flightsは20操作で指定日の便一覧まで到達し最終10条件が成功しました。航空便の選択・予約は行っていません。この操作確認はモデルによる完走の証明ではありません。

## 証跡と未達条件

本測定はGit対象外の`.audit/web-benchmark/3b92754556cc467cba16584db85f94f9/`にあります。

- `conditions.json`: 入力、環境、全ソースのSHA256。
- `runs.json`、`summary.json`: 全30試行と成功時だけの集計。
- `evidence-validation.json`: 実HTTP、resolved model、画像、journal、cleanup、source hashの照合結果。
- 試行別の`result.json`、`result.png`、bu-Jの`journal.sqlite3`、Chromeの起動・削除記録。

開発中の失敗・中断試験は別のrun IDに残し、本表に混ぜていません。中断された子プロセスの使用量が不明な試験もあるため、本測定の154要求・tokensを開発作業全体の利用量とは扱いません。中断したsuiteは再開していません。

helper接続では、承認された`openai-aist`に対する現在のログインの`Cognitive Services OpenAI User`だけを付与しました。Azureが返したOpenAI専用endpointへの修正後、実生成が成功しました。他リソースへの権限追加やdeployment作成は行っていません。

最終ソースで全339テスト、既存CLIの6経路、wheel buildと同梱内容の照合が成功しました。独立レビューも通過しています。従来TLA+は497状態で成功し、対象5ソースのhashは変わっていません。その保証はfixture経路に限られ、新Web runtimeの形式証明ではありません。

残る受入条件は、Microsoft-Decision-1の新しい質問・状態表現に対する採用基準の校正と、3タスクのモデル制御による完走です。棄権基準を下げて成功件数を作る調整は行っていません。remote CI・公開・pushは実行していません。
