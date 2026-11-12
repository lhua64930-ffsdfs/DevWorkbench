from typing import Any

import httpx

from app.core.config import settings
from app.core.production import validate_outbound_url


class GitHubClient:
    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        self.token = token if settings.app_env.lower() == "production" else token or settings.github_token
        self.base_url = validate_outbound_url((base_url or settings.github_api_base_url).rstrip("/"))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=settings.app_env.lower() != "production") as client:
            response = await client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
            response.raise_for_status()
            return response.json()

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        from urllib.parse import urljoin
        url, headers = f"{self.base_url}{path}", self._headers()
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            for _ in range(5):
                response = await client.get(validate_outbound_url(url), headers=headers, params=params)
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = validate_outbound_url(urljoin(url, response.headers["location"]))
                    headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
                    params = None
                    continue
                response.raise_for_status()
                return response.content
        raise RuntimeError("Too many GitHub content redirects")

    async def post(self, path: str, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=settings.app_env.lower() != "production") as client:
            response = await client.post(f"{self.base_url}{path}", headers=self._headers(), json=payload)
            response.raise_for_status()
            return response.json()

    async def patch(self, path: str, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=settings.app_env.lower() != "production") as client:
            response = await client.patch(f"{self.base_url}{path}", headers=self._headers(), json=payload)
            response.raise_for_status()
            return response.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self.get(f"/repos/{owner}/{repo}")

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/comments", {"body": body})

    async def create_issue(self, owner: str, repo: str, title: str, body: str) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues", {"title": title, "body": body})

    async def close_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        return await self.patch(f"/repos/{owner}/{repo}/issues/{issue_number}", {"state": "closed"})

    async def add_issue_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/labels", {"labels": labels})
