# Google DriveからKnowledge draftを生成する

共有ドライブを原本、Knowledgeリポジトリを確認済み構造化知識として扱う。同期器はDrive APIを読み取り専用で利用し、原文をGitへ複製せず、出典情報と未確認の要約をdraft Markdownとして生成する。

## 前提

- 個人のMy Driveではなく、用途ごとの共有ドライブを使う。
- サービスアカウントへ対象共有ドライブの閲覧権限だけを付与する。
- GCEではDrive読取専用のサービスアカウントをVMへ付与する。ローカル実行時だけ、必要に応じて`GOOGLE_SERVICE_ACCOUNT_JSON`をSecret Manager等からファイルとして供給する。
- 他局は共有ドライブと同期状態を分離する。
- `GOOGLE_DRIVE_FOLDER_ID`を指定した場合、現状はそのフォルダの直下だけを対象とする。

## 初回

通常起動では現在のchange tokenだけを保存し、既存文書を勝手に全投入しない。既存文書も取り込む場合だけ、範囲を確認して`--full`を実行する。

```bash
docker compose --profile tools run --rm knowledge-sync --full
```

生成先は既定で`/data/knowledge-drafts`。内容を確認してKnowledgeリポジトリの`sources/drive/inbox/`へ移し、PRでレビューする。

## 差分同期

```bash
docker compose --profile tools run --rm knowledge-sync
```

Driveのpage tokenとファイルのSHA-256を保存し、変更されたファイルだけを再生成する。原本が削除された場合、Knowledge文書を自動削除せず、同期状態にremovedを記録する。

状態を更新せず対象件数だけ確認する場合は`docker compose --profile tools run --rm knowledge-sync --dry-run`を使う。

## 対応形式

- Google Docs：plain textへexport
- Google Sheets：CSVへexport
- Google Slides：plain textへexport可能な場合
- text/plain、Markdown、CSV、JSON

PDF、画像、動画などは現段階では自動要約せずスキップする。OCRやPDF解析は、閲覧範囲と費用を確認した後に別処理として追加する。

## PR自動化をまだ有効にしない理由

Knowledgeリポジトリの`AGENTS.md`はAIによるpushを禁止している。現在の同期器はローカルdraft生成までに限定する。サービスBotによるPR作成を許可する場合は、対象パス、CODEOWNERS、必要な承認数、Botの権限、Secret管理を先に明文化する。
