"""週次レポート + 月間報告書 + 名言検出"""

import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from google import genai

log = logging.getLogger("yagapon.reports")

WEEKLY_PROMPT = """あなたは矢上祭実行委員会のDiscordサーバーのアナリストです。
ナレッジベース（Discordの会話ログ）を検索して、{start}〜{end}の期間の活動を分析し、週次レポートを作成してください。

## 重要な指示
- 必ずナレッジベースを検索して実際の会話内容に基づいて書くこと
- 推測や一般的な内容ではなく、具体的な事実・発言を元にすること
- 名言は原文をそのまま引用すること

## フォーマット
### 📊 週次レポート ({start} ~ {end})

#### 主な活動・議論
- （具体的な議題名、チャンネル名、関わったメンバーを含めて3-5項目）

#### 決定事項
- （実際に決まったことを具体的に。なければ「特になし」）

#### 進捗・成果
- （完了したタスク、作成物、イベントなど。なければ「特になし」）

#### 注目の会話
- （盛り上がった議論やユニークなやり取りを要約。チャンネル名と参加者を添えて）

#### 来週に向けて
- （未解決の課題、予定、宿題など）

#### 📜 今週の名言
> 「発言内容をそのまま引用」
> — 発言者名（#チャンネル名）
"""

MONTHLY_PROMPT = """あなたは矢上祭実行委員会の月間報告書を作成するアナリストです。
以下の週次レポートを元に、{month}の月間報告書を作成してください。

## 重要な指示
- 週次レポートの内容を要約・統合し、月全体の流れがわかるようにすること
- メンバーの貢献は具体的なエピソードとともに記載すること
- 名言は各週のものから厳選すること

## フォーマット
### 📋 月間報告書 ({month})

#### 月間サマリー
（今月の活動を3-5行で概要。何に注力し、何を達成したか）

#### 主な成果
- （完了したプロジェクト、イベント、作成物など）

#### 活発だったトピック
- （よく議論された話題とその結論）

#### 課題・未解決事項
- （来月に持ち越す課題）

#### メンバーハイライト
- （特に活躍・貢献したメンバーと具体的なエピソード）

#### 📜 今月の名言ベスト3
1. > 「発言」— 発言者（#チャンネル名）
2. > 「発言」— 発言者（#チャンネル名）
3. > 「発言」— 発言者（#チャンネル名）

## 元データ（週次レポート）
{weekly_reports}
"""


async def generate_weekly_report(bot, guild_id: int) -> str:
    """1週間の会話をまとめて週次レポート生成"""
    corpus = bot.config.get_corpus(guild_id)
    if not corpus:
        return "コーパスが設定されていないぽん"

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)

    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))

    from google.genai import types

    prompt = WEEKLY_PROMPT.format(
        start=start.strftime("%m/%d"),
        end=end.strftime("%m/%d"),
    )
    # file_searchで期間を指定して検索させる
    prompt += (
        f"\n\n検索のヒント: {start.strftime('%Y-%m-%d')}〜{end.strftime('%Y-%m-%d')}の期間のドキュメントを"
        "重点的に検索してください。複数のチャンネルを横断的に調べ、"
        "主要な議論・決定事項・活動を網羅してください。"
    )

    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=(
                "あなたは矢上祭実行委員会のDiscord活動をまとめるレポーターです。"
                "ナレッジベースを徹底的に検索し、具体的な事実に基づいてレポートを書いてください。"
                "推測は禁止。見つからない情報は「記録なし」と書いてください。"
            ),
            tools=[
                types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=[corpus]
                    )
                )
            ],
        ),
    )
    return response.text or "レポート生成に失敗したぽん"


async def generate_monthly_report(bot, guild_id: int) -> str:
    """月間報告書を生成"""
    now = datetime.now(timezone.utc)
    month_str = now.strftime("%Y年%m月")

    # 4週分の週次レポートを生成
    weekly_reports = []
    for i in range(4):
        report = await generate_weekly_report(bot, guild_id)
        weekly_reports.append(f"### 第{4-i}週\n{report}")

    combined = "\n\n".join(weekly_reports)

    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=MONTHLY_PROMPT.format(
            month=month_str,
            weekly_reports=combined,
        ),
    )
    return response.text or "月間報告書の生成に失敗したぽん"


async def post_weekly_report(bot, guild_id: int, channel: discord.TextChannel):
    """週次レポートを生成してチャンネルに投稿"""
    report = await generate_weekly_report(bot, guild_id)

    embed = discord.Embed(
        title="📊 週次レポート",
        description=report[:4096],
        color=discord.Color.teal(),
        timestamp=datetime.now(timezone.utc),
    )
    await channel.send(embed=embed, silent=True)
