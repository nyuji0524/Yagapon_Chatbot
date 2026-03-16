"""週次レポート + 月間報告書 + 名言検出"""

import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from google import genai

log = logging.getLogger("yagapon.reports")

WEEKLY_PROMPT = """以下はDiscordサーバーの1週間分の会話ログです。
週次レポートを作成してください。

## フォーマット
### 📊 週次レポート ({start} ~ {end})

#### 主な活動・議論
- (箇条書き、3-5項目)

#### 決定事項
- (あれば)

#### 進捗・成果
- (あれば)

#### 来週に向けて
- (未解決の課題、予定など)

#### 📜 今週の名言
> 「発言内容」— 発言者名
(面白い・印象的な発言を1つ選んでください)

## 会話ログ
{logs}
"""

MONTHLY_PROMPT = """以下はDiscordサーバーの1ヶ月分の週次レポートです。
月間報告書を作成してください。

## フォーマット
### 📋 月間報告書 ({month})

#### 月間サマリー
(3-5行で概要)

#### 主な成果
- (箇条書き)

#### 課題・未解決事項
- (箇条書き)

#### 来月に向けて
- (箇条書き)

#### メンバー活動
(特に活躍した人がいれば)

#### 📜 今月の名言ベスト3
1. 「発言」— 発言者
2. 「発言」— 発言者
3. 「発言」— 発言者

## 週次レポート
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

    # コーパスから1週間分の内容を検索
    query = (
        f"{start.strftime('%Y-%m-%d')}から{end.strftime('%Y-%m-%d')}までの"
        "主な会話、議論、決定事項をすべて教えてください。"
    )

    from google.genai import types
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=WEEKLY_PROMPT.format(
            start=start.strftime("%m/%d"),
            end=end.strftime("%m/%d"),
            logs=f"(コーパス検索クエリ: {query})",
        ),
        config=types.GenerateContentConfig(
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
    await channel.send(embed=embed)
