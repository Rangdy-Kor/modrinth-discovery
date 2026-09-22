from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from .models import Dependency, ModFile, Project, SearchIndex, SearchPage
from .diagnostics import get_logger

API_BASE = "https://api.modrinth.com/v2"
USER_AGENT = "Rangdy-Kor/modrinth-discovery/0.2.0 (personal desktop app)"
logger = get_logger("api")


class ModrinthError(RuntimeError):
    """Base class for errors safe to present to a user."""


class ModrinthNetworkError(ModrinthError):
    pass


class ModrinthResponseError(ModrinthError):
    pass


class CompatibleVersionNotFound(ModrinthError):
    pass


class ModrinthAPI:
    def __init__(self, *, timeout: float = 30.0, client: httpx.Client | None = None) -> None:
        self.timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=API_BASE,
            timeout=self.timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        logger.info("request.start method=%s url=%s", method, url)
        try:
            response = self.client.request(method, url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            logger.info("request.done method=%s url=%s status=%d", method, url, response.status_code)
            return response
        except httpx.TimeoutException as error:
            raise ModrinthNetworkError("Modrinth 응답 시간이 초과되었습니다.") from error
        except httpx.NetworkError as error:
            raise ModrinthNetworkError("Modrinth에 연결할 수 없습니다. 인터넷 연결을 확인해 주세요.") from error
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            detail = _error_detail(error.response)
            raise ModrinthResponseError(f"Modrinth API 요청이 실패했습니다 (HTTP {status}).{detail}") from error

    def search_projects(self, *, minecraft_version: str, loader: str,
                        index: SearchIndex | str = SearchIndex.DOWNLOADS,
                        limit: int = 50, offset: int = 0, query: str = "") -> SearchPage:
        facets = [["project_type:mod"], [f"versions:{minecraft_version}"],
                  [f"categories:{loader.lower()}"]]
        response = self._request("GET", "/search", params={
            "query": query, "facets": json.dumps(facets, separators=(",", ":")),
            "limit": min(max(limit, 1), 100), "offset": max(offset, 0), "index": str(index),
        })
        data = response.json()
        return SearchPage(
            projects=[_parse_search_hit(item) for item in data.get("hits", [])],
            offset=int(data.get("offset", offset)), limit=int(data.get("limit", 0)),
            total_hits=int(data.get("total_hits", 0)),
        )

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/project/{project_id}").json()

    def get_version(self, version_id: str) -> dict[str, Any]:
        return self._request("GET", f"/version/{version_id}").json()

    def get_versions_from_hashes(self, hashes: list[str], *, algorithm: str = "sha512") -> dict[str, dict[str, Any]]:
        result={}
        for start in range(0,len(hashes),100):
            result.update(self._request("POST", "/version_files", json={"hashes": hashes[start:start+100], "algorithm": algorithm}).json())
        return result

    def get_projects(self, project_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not project_ids:
            return {}
        result={}
        for start in range(0,len(project_ids),100):
            response = self._request("GET", "/projects", params={"ids": json.dumps(project_ids[start:start+100])})
            result.update({item["id"]: item for item in response.json()})
        return result

    def get_versions_by_ids(self, version_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not version_ids: return {}
        result={}
        for start in range(0,len(version_ids),100):
            response=self._request("GET","/versions",params={"ids":json.dumps(version_ids[start:start+100])})
            result.update({item["id"]:item for item in response.json()})
        return result

    def get_versions(self, project_id: str, *, minecraft_version: str,
                     loader: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/project/{project_id}/version", params={
            "game_versions": json.dumps([minecraft_version]),
            "loaders": json.dumps([loader.lower()]), "include_changelog": "false",
        })
        return list(response.json())

    def get_latest_file(self, project_id: str, *, minecraft_version: str,
                        loader: str) -> ModFile:
        versions = self.get_versions(project_id, minecraft_version=minecraft_version, loader=loader)
        if not versions:
            raise CompatibleVersionNotFound("선택한 Minecraft 버전과 로더에 맞는 버전이 없습니다.")
        releases = [v for v in versions if v.get("version_type") == "release"]
        version = (releases or versions)[0]
        return _parse_mod_file(version, project_id)

    def mod_file_from_version(self, version: dict[str, Any]) -> ModFile:
        return _parse_mod_file(version, version["project_id"])

    def get_bytes(self, url: str) -> bytes:
        return self._request("GET", url).content

    def iter_download(self, url: str, chunk_size: int = 128 * 1024) -> Iterator[bytes]:
        try:
            logger.info("download.start url=%s", url)
            with self.client.stream("GET", url, timeout=self.timeout) as response:
                response.raise_for_status()
                yield from response.iter_bytes(chunk_size)
            logger.info("download.done url=%s", url)
        except httpx.TimeoutException as error:
            raise ModrinthNetworkError("다운로드 시간이 초과되었습니다.") from error
        except httpx.NetworkError as error:
            raise ModrinthNetworkError("다운로드 연결이 끊어졌습니다.") from error
        except httpx.HTTPStatusError as error:
            raise ModrinthResponseError(f"파일 다운로드가 실패했습니다 (HTTP {error.response.status_code}).") from error

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def _parse_mod_file(version: dict[str, Any], project_id: str) -> ModFile:
    files = version.get("files") or []
    if not files:
        raise ModrinthResponseError("선택한 버전에 다운로드 파일이 없습니다.")
    selected = next((item for item in files if item.get("primary")), files[0])
    hashes = selected.get("hashes") or {}
    dependencies = tuple(Dependency(
        dependency_type=item.get("dependency_type", "unknown"), project_id=item.get("project_id"),
        version_id=item.get("version_id"), file_name=item.get("file_name"))
        for item in version.get("dependencies", []))
    return ModFile(project_id=project_id, version_id=version["id"],
        version_number=version.get("version_number", version["id"]),
        filename=selected["filename"], url=selected["url"],
        sha512=hashes.get("sha512"), sha1=hashes.get("sha1"),
        size=selected.get("size"), dependencies=dependencies)


def _parse_search_hit(item: dict[str, Any]) -> Project:
    slug = item["slug"]
    return Project(project_id=item["project_id"], slug=slug, title=item["title"],
        description=item.get("description", ""), author=item.get("author", ""),
        downloads=int(item.get("downloads", 0)), icon_url=item.get("icon_url"),
        project_url=f"https://modrinth.com/mod/{slug}",
        date_modified=item.get("date_modified", ""),
        environment=tuple(item.get("environment") or ()))


def _error_detail(response: httpx.Response) -> str:
    try:
        description = response.json().get("description")
    except (ValueError, AttributeError):
        description = None
    return f" {description}" if description else ""
