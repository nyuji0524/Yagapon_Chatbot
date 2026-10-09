"""CLI entry point for GitHub Actions."""

import os

from reviewer.github import GitHubClient
from reviewer.review import run_review


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    pull_number = int(os.environ["PR_NUMBER"])
    model = os.environ.get("YAGAPON_REVIEW_MODEL", "gemini-3.8-flash")
    thinking_level = os.environ.get("YAGAPON_REVIEW_THINKING", "medium")
    github = GitHubClient(os.environ["GITHUB_TOKEN"])
    result = run_review(github, repository, pull_number, model, thinking_level)
    print(f"YagaPon review: {result}")


if __name__ == "__main__":
    main()
