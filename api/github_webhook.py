"""GitHub webhook - push通知 + コードレビュー"""

import hashlib
import hmac
import json
import logging
import os

import aiohttp
import discord
from fastapi import APIRouter, HTTPException, Request
from google import genai
from google.genai import types

log = logging.getLogger("yagapon.github")

router = APIRouter()

REVIEW_PROMPT = """あなたは経験豊富なソフトウェアエンジニアです。以下のGitHub pushをレビューしてください。
日本語で、簡潔かつ具体的に回答してください。

## 出力フォーマット
### 変更概要
（何を・なぜ変えたか、2-3行）

### 変更内容
（全ての変更点をファイルごとに箇条書きで列挙。漏れなく記載すること）
- `ファイル名`: 変更内容
- `ファイル名`: 変更内容

### 良い点
- （コードの良いところがあれば。なければ省略）

### 指摘事項
- 🟥 **Critical**: （セキュリティリスク、データ損失、本番障害の可能性）
- 🟧 **Major**: （バグの可能性、エッジケース未対応、ロジックエラー）
- 🟨 **Minor**: （コードスタイル、命名、パフォーマンス改善の提案）
- 🟩 **Trivial**: （typo、コメント、フォーマットなど）
（問題がなければ「✅ 特に指摘事項なし」）

## Push情報
リポジトリ: {repo}
ブランチ: {branch}
プッシュ者: {pusher}

### コミット
{commits}
"""


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.HMAC(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


@router.post("/webhook/github/{guild_id}")
async def github_webhook(guild_id: int, request: Request):
    bot = request.app.state.bot
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    # 署名検証
    payload = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if secret and not _verify_signature(payload, signature, secret):
        raise HTTPException(403, "Invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    if event != "push":
        return {"status": "ignored", "event": event}

    data = json.loads(payload)

    # pushデータ抽出
    repo = data["repository"]["full_name"]
    branch = data["ref"].split("/")[-1]
    pusher = data["pusher"]["name"]
    repo_url = data["repository"]["html_url"]

    commits_text = ""
    for c in data.get("commits", []):
        files = c.get("added", []) + c.get("modified", []) + c.get("removed", [])
        commits_text += (
            f"- [{c['id'][:7]}] {c['message']}\n"
            f"  Author: {c['author']['name']}\n"
            f"  Files: {', '.join(files[:10])}\n"
        )

    # GitHub APIで各コミットのpatchを取得
    diff_text = ""
    try:
        async with aiohttp.ClientSession() as session:
            for c in data.get("commits", [])[:5]:
                patch_url = f"https://github.com/{repo}/commit/{c['id']}.diff"
                async with session.get(
                    patch_url,
                    headers={"User-Agent": "YagaPon-Bot"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        patch = await resp.text()
                        diff_text += f"\n--- Commit {c['id'][:7]}: {c['message'][:60]} ---\n{patch}\n"
            # 長すぎる場合は切り詰め
            if len(diff_text) > 15000:
                diff_text = diff_text[:15000] + "\n... (差分が長いため省略)"
    except Exception as e:
        log.warning(f"Failed to fetch diff: {e}")

    # Geminiでレビュー生成
    prompt = REVIEW_PROMPT.format(
        repo=repo, branch=branch, pusher=pusher, commits=commits_text
    )
    if diff_text:
        prompt += f"\n\n### コード差分\n```diff\n{diff_text}\n```"

    try:
        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        review = response.text
    except Exception as e:
        log.error(f"Gemini review error: {e}")
        review = "(レビュー生成に失敗しました)"

    # Discord Embed作成
    embed = discord.Embed(
        title=f"Push: {repo}",
        url=f"{repo_url}/compare/{data.get('before', '')[:7]}...{data.get('after', '')[:7]}",
        color=discord.Color.purple(),
    )
    embed.add_field(name="ブランチ", value=branch, inline=True)
    embed.add_field(name="プッシュ者", value=pusher, inline=True)
    embed.add_field(
        name="コミット",
        value="\n".join(
            f"`{c['id'][:7]}` {c['message'][:60]}"
            for c in data.get("commits", [])[:5]
        ) or "なし",
        inline=False,
    )
    # レビューが短ければEmbedに、長ければ別メッセージで送信
    if len(review) <= 1024:
        embed.add_field(name="AIレビュー", value=review, inline=False)

    # 送信先チャンネル取得
    channel_id = bot.config.get_github_channel(guild_id)
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(embed=embed, silent=True)
            if len(review) > 1024:
                await bot.send_split_message(channel, review)

    return {"status": "processed", "commits": len(data.get("commits", []))}
