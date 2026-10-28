# bu-J

A Python controller for fixed browser workflows using Microsoft-Decision-1. Includes an offline demo and isolated Chrome tasks for Google Flights, Wikipedia, and a hotel-search fixture.

Microsoft-Decision-1で定型のブラウザ操作を行うPython製Controllerです。

## Driver setup / Driverの導入

The Driver runtime executes browser actions. Requires Python 3.12, uv, and Docker for real Chrome. Run from the repository root.

Driverはブラウザ操作を実行する本体です。Python 3.12、uv、実Chrome用のDockerを用意し、リポジトリ直下で実行します。

```sh
uv sync --locked --extra web --python 3.12
uv run decision-browser --help
```

Run the offline demo. No Azure account or browser is required; the command returns four synthetic records.

オフラインデモを実行します。Azureアカウントやブラウザは不要で、合成データ4件を返します。

```sh
uv run decision-browser --provider mock --mode enforced --routing rules --query a
```

Build the isolated Chrome image. It includes Google Chrome and Browser Harness; no host browser connection is required.

専用Chrome imageを作成します。Google ChromeとBrowser Harnessはimage内に導入され、普段使いのブラウザには接続しません。

```sh
docker build --platform linux/amd64 -f verification/chrome/Dockerfile -t bu-j-chrome-verifier:local .
```

### Azure setup / Azureの設定

Use an existing Microsoft-Decision-1 deployment with inference access. Sign in with Azure CLI, then create `.env` from [.env.example](.env.example) if needed and set `AZURE_ENDPOINT` and `AZURE_DEPLOYMENT_NAME`.

推論権限のある既存のMicrosoft-Decision-1 deploymentを使います。Azure CLIでログインし、未設定なら[.env.example](.env.example)から`.env`を作り、`AZURE_ENDPOINT`と`AZURE_DEPLOYMENT_NAME`を設定します。

```sh
az login
uv run --env-file .env decision-browser --provider azure --mode enforced --max-cost-units 10000
```

This calls the paid Azure API with the synthetic browser. Real Chrome tasks also require Docker and a text-generation deployment; follow the [Chrome setup guide](docs/web_benchmark.md#試験を実行する).

このコマンドは合成ブラウザで有料のAzure APIを呼びます。実ChromeのタスクにはDockerと文字列生成用deploymentも必要です。[Chrome導入手順](docs/web_benchmark.md#試験を実行する)を参照してください。

## Skill setup / Skillの導入

The Skill teaches your agent how to use the Driver. Requires Node.js 22.20+ with npx; install the Driver above separately.

SkillはDriverの使い方をエージェントへ伝えます。Node.js 22.20以降とnpxが必要です。Driver本体は上の手順で別途導入します。

Install for your user account, select the target agent, then start a new agent session in the bu-J checkout and ask it to use the `bu-j` skill.

ユーザー単位で導入して対象エージェントを選び、bu-Jのcheckoutで新しいセッションを開いて「bu-j Skillを使って」と依頼します。

```sh
npx skills add geekjapan/bu-J --skill bu-j --global
npx skills list --global
```

For a project-only install, omit `--global` and run in that project. The Skill does not install Python dependencies, Chrome, or Azure credentials.

特定プロジェクトだけで使う場合は、その直下で`--global`を省いて実行します。SkillだけではPython依存、Chrome、Azure認証は導入されません。
