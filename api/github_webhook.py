"""GitHub webhook - push通知 + コードレビュー（コード全体コンテキスト付き）"""

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

重要: コード差分だけでなく、提供されたリポジトリのコード全体も参照して、
変更が既存コードと整合しているか（メソッドの存在確認、型の一致、インポートの正確性など）も確認してください。

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

# コンテキストとして取得するファイル拡張子
CODE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml", ".cfg", ".txt", ".md"}
# 取得しないディレクトリ
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build"}
# 1ファイルの最大サイズ（文字数）
MAX_FILE_SIZE = 5000
# コンテキスト全体の最大サイズ
MAX_CONTEXT_SIZE = 50000


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.HMAC(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


async def _fetch_repo_tree(session: aiohttp.ClientSession, repo: str, sha: str) -> list[str]:
    """GitHubのTree APIでファイル一覧を取得"""
    url = f"https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1"
    try:
        async with session.get(
            url,
            headers={"User-Agent": "YagaPon-Bot", "Accept": "application/vnd.github.v3+json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                paths = []
                for item in data.get("tree", []):
                    if item["type"] != "blob":
                        continue
                    path = item["path"]
                    # フィルタ: コードファイルのみ、スキップディレクトリ除外
                    parts = path.split("/")
                    if any(d in SKIP_DIRS for d in parts):
                        continue
                    ext = os.path.splitext(path)[1]
                    if ext in CODE_EXTENSIONS:
                        paths.append(path)
                return paths
    except Exception as e:
        log.warning(f"Failed to fetch repo tree: {e}")
    return []


async def _fetch_file_content(session: aiohttp.ClientSession, repo: str, path: str, ref: str) -> str | None:
    """GitHubからファイル内容を取得"""
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    try:
        async with session.get(
            url,
            headers={"User-Agent": "YagaPon-Bot"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                text = await resp.text()
                if len(text) > MAX_FILE_SIZE:
                    return text[:MAX_FILE_SIZE] + f"\n... (以降省略、全{len(text)}文字)"
                return text
    except Exception:
        pass
    return None


async def _build_code_context(session: aiohttp.ClientSession, repo: str, sha: str, changed_files: set[str]) -> str:
    """変更ファイルと関連ファイルのコード全体を取得"""
    all_files = await _fetch_repo_tree(session, repo, sha)
    if not all_files:
        return ""

    # 優先順位: 1.変更されたファイル 2.変更ファイルと同じディレクトリのファイル 3.その他重要ファイル
    priority_files = []
    related_files = []
    other_files = []

    changed_dirs = {os.path.dirname(f) for f in changed_files}

    for path in all_files:
        if path in changed_files:
            priority_files.append(path)
        elif os.path.dirname(path) in changed_dirs:
            related_files.append(path)
        elif path in ("requirements.txt", "main.py", "setup.py", "pyproject.toml", "package.json"):
            related_files.append(path)
        else:
            other_files.append(path)

    # コンテキスト構築（サイズ制限内で可能な限り多く取得）
    context_parts = []
    total_size = 0

    for file_list in [priority_files, related_files, other_files]:
        for path in file_list:
            if total_size >= MAX_CONTEXT_SIZE:
                break
            content = await _fetch_file_content(session, repo, path, sha)
            if content:
                entry = f"\n=== {path} ===\n{content}\n"
                total_size += len(entry)
                context_parts.append(entry)

    if context_parts:
        return "\n## リポジトリのコード（push後の最新状態）\n" + "".join(context_parts)
    return ""


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

    # バックグラウンドでレビュー処理（GitHubのタイムアウト回避）
    import asyncio
    asyncio.create_task(_process_review(bot, guild_id, data))

    return {"status": "accepted", "commits": len(data.get("commits", []))}


async def _process_review(bot, guild_id: int, data: dict):
    """バックグラウンドでコードレビューを実行"""
    try:
        repo = data["repository"]["full_name"]
        branch = data["ref"].split("/")[-1]
        pusher = data["pusher"]["name"]
        repo_url = data["repository"]["html_url"]
        after_sha = data.get("after", "")

        # 変更ファイル一覧を収集
        changed_files = set()
        commits_text = ""
        for c in data.get("commits", []):
            files = c.get("added", []) + c.get("modified", []) + c.get("removed", [])
            changed_files.update(files)
            commits_text += (
                f"- [{c['id'][:7]}] {c['message']}\n"
                f"  Author: {c['author']['name']}\n"
                f"  Files: {', '.join(files[:10])}\n"
            )

        async with aiohttp.ClientSession() as session:
            # コード差分を取得
            diff_text = ""
            try:
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
                if len(diff_text) > 15000:
                    diff_text = diff_text[:15000] + "\n... (差分が長いため省略)"
            except Exception as e:
                log.warning(f"Failed to fetch diff: {e}")

            # リポジトリのコード全体をコンテキストとして取得
            code_context = ""
            if after_sha:
                code_context = await _build_code_context(session, repo, after_sha, changed_files)

        # Geminiでレビュー生成
        prompt = REVIEW_PROMPT.format(
            repo=repo, branch=branch, pusher=pusher, commits=commits_text
        )
        if diff_text:
            prompt += f"\n\n### コード差分\n```diff\n{diff_text}\n```"
        if code_context:
            prompt += code_context

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

    except Exception as e:
        log.error(f"Review processing error: {e}")
