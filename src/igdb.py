import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import aiofiles
import aiofiles.os
import httpx

from src.console import console


class IGDBClient:
    def __init__(self, config: dict[str, Any], base_dir: str) -> None:
        default_config = config.get("DEFAULT", {})
        self.client_id = str(
            default_config.get("igdb_client_id")
            or default_config.get("twitch_client_id")
            or os.environ.get("IGDB_CLIENT_ID")
            or os.environ.get("TWITCH_CLIENT_ID")
            or ""
        ).strip()
        self.client_secret = str(
            default_config.get("igdb_client_secret")
            or default_config.get("twitch_client_secret")
            or os.environ.get("IGDB_CLIENT_SECRET")
            or os.environ.get("TWITCH_CLIENT_SECRET")
            or ""
        ).strip()
        self.cache_dir = Path(base_dir) / "data" / "igdb_cache"
        self.token_file = self.cache_dir / "token.json"

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _token(self) -> Optional[str]:
        cached = await self._read_json(self.token_file)
        if isinstance(cached, dict) and float(cached.get("expires_at", 0) or 0) > time.time() + 300:
            token = cached.get("access_token")
            return str(token) if token else None

        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post("https://id.twitch.tv/oauth2/token", params=params)
                response.raise_for_status()
            data = response.json()
        except Exception as e:
            console.print(f"[yellow]IGDB: OAuth lookup failed: {e}[/yellow]")
            return None

        token = data.get("access_token")
        if not token:
            return None
        await self._write_json(
            self.token_file,
            {
                "access_token": token,
                "expires_at": time.time() + int(data.get("expires_in") or 3600),
            },
        )
        return str(token)

    async def search_games(self, title: str) -> list[dict[str, Any]]:
        cache_key = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "empty"
        cache_file = self.cache_dir / "search" / f"{cache_key}.json"
        cached = await self._read_json(cache_file)
        if isinstance(cached, list):
            return [item for item in cached if isinstance(item, dict)]

        query = (
            f'search "{title.replace(chr(34), " ")}"; '
            "fields id,name,summary,storyline,first_release_date,rating,rating_count,"
            "cover.url,artworks.url,screenshots.url,genres.name,platforms.name,"
            "involved_companies.company.name,involved_companies.developer,involved_companies.publisher,"
            "websites.url,websites.type,external_games.url,external_games.external_game_source,external_games.uid;"
            "limit 8;"
        )
        data = await self._query(query)
        if isinstance(data, list):
            await self._write_json(cache_file, data)
            return [item for item in data if isinstance(item, dict)]
        return []

    async def game_by_id(self, igdb_id: str | int) -> Optional[dict[str, Any]]:
        igdb_id_text = str(igdb_id).strip()
        if not igdb_id_text.isdigit():
            return None
        cache_file = self.cache_dir / "games" / f"{igdb_id_text}.json"
        cached = await self._read_json(cache_file)
        if isinstance(cached, dict):
            return cached

        query = (
            f"where id = {igdb_id_text}; "
            "fields id,name,summary,storyline,first_release_date,rating,rating_count,"
            "cover.url,artworks.url,screenshots.url,genres.name,platforms.name,"
            "involved_companies.company.name,involved_companies.developer,involved_companies.publisher,"
            "websites.url,websites.type,external_games.url,external_games.external_game_source,external_games.uid;"
            "limit 1;"
        )
        data = await self._query(query)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            await self._write_json(cache_file, data[0])
            return data[0]
        return None

    async def game_by_steam_id(self, steam_id: str | int) -> Optional[dict[str, Any]]:
        steam_id_text = str(steam_id).strip()
        if not steam_id_text.isdigit():
            return None
        cache_file = self.cache_dir / "steam" / f"{steam_id_text}.json"
        cached = await self._read_json(cache_file)
        if isinstance(cached, dict):
            return cached

        query = (
            f'where external_games.external_game_source = 1 & external_games.uid = "{steam_id_text}"; '
            "fields id,name,summary,storyline,first_release_date,rating,rating_count,"
            "cover.url,artworks.url,screenshots.url,genres.name,platforms.name,"
            "involved_companies.company.name,involved_companies.developer,involved_companies.publisher,"
            "websites.url,websites.type,external_games.url,external_games.external_game_source,external_games.uid;"
            "limit 1;"
        )
        data = await self._query(query)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            await self._write_json(cache_file, data[0])
            return data[0]
        return None

    async def _query(self, query: str) -> Any:
        if not self.configured:
            return None
        token = await self._token()
        if not token:
            return None

        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "text/plain",
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post("https://api.igdb.com/v4/games", headers=headers, content=query)
                response.raise_for_status()
            return response.json()
        except Exception as e:
            console.print(f"[yellow]IGDB: API lookup failed: {e}[/yellow]")
            return None

    async def _read_json(self, path: Path) -> Any:
        try:
            async with aiofiles.open(path, encoding="utf-8") as file:
                return json.loads(await file.read())
        except Exception:
            return None

    async def _write_json(self, path: Path, data: Any) -> None:
        try:
            await aiofiles.os.makedirs(path.parent, exist_ok=True)
            async with aiofiles.open(path, "w", encoding="utf-8") as file:
                await file.write(json.dumps(data, indent=4))
        except Exception:
            return
