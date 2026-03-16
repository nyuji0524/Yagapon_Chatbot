"""GitHub webhook - push通知 + コードレビュー + 自動デプロイ"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys

import discord
from fastapi import APIRouter, HTTPException, Request
from google import genai
from google.genai import types

log = logging.getLogger("yagapon.github")

router = APIRouter()

REVIEW_PROMPT = """以下のGitHub pushの変更内容をレビューしてください。日本語で回答。

## 要求
1. **変更概要** (3行以内)
2. **コードレビュー** (問題があれば指摘、なければ「問題なし」)
   - セキュリティリスク
   - バグの可能性
   - 改善提案

## 変更内容
リポジトリ: {repo}
ブランチ: {branch}
プッシュ者: {pusher}

### コミット
{commits}
"""


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
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

    # Geminiでレビュー生成
    prompt = REVIEW_PROMPT.format(
        repo=repo, branch=branch, pusher=pusher, commits=commits_text
    )

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
    embed.add_field(name="AIレビュー", value=review[:1024], inline=False)

    # 送信先チャンネル取得
    channel_id = bot.config.get_github_channel(guild_id)
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(embed=embed)

    # 自動デプロイ (git pull + 再起動)
    if branch == "main":
        asyncio.create_task(_auto_deploy(bot, channel_id))

    return {"status": "processed", "commits": len(data.get("commits", []))}


async def _auto_deploy(bot, notify_channel_id: int | None = None):
    """git pull して bot を再起動"""
    loop = asyncio.get_event_loop()

    def _pull():
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            capture_output=True, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        return result

    try:
        result = await loop.run_in_executor(None, _pull)
        if result.returncode != 0:
            log.error(f"git pull failed: {result.stderr}")
            return

        if "Already up to date" in result.stdout:
            log.info("No changes to deploy")
            return

        log.info(f"git pull success: {result.stdout.strip()}")

        # 通知
        if notify_channel_id and bot:
            channel = bot.get_channel(notify_channel_id)
            if channel:
                await channel.send("🔄 コードを更新したぽん！再起動するぽん...")

        # 少し待ってから再起動
        await asyncio.sleep(2)
        log.info("Restarting bot...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    except Exception as e:
        log.error(f"Auto-deploy error: {e}")
