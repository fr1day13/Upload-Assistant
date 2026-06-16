import datetime
import html
import os
import re
from pathlib import Path
from typing import Any, Optional

import aiofiles
import aiofiles.os
import cli_ui

from src.console import console
from src.igdb import IGDBClient


GAME_EXTENSIONS = {
    ".7z", ".bin", ".cia", ".cue", ".exe", ".gdi", ".iso", ".nsp", ".pkg", ".rar",
    ".rvz", ".tar", ".wad", ".wbfs", ".xci", ".zip",
}
NON_GAME_MEDIA_EXTENSIONS = {
    ".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flac", ".mp3", ".m4a",
    ".m4b", ".epub", ".pdf", ".mobi", ".azw3", ".cbz", ".cbr",
}
PC_MARKERS = {"pc", "windows", "win", "linux", "mac", "macos"}
PLAYSTATION_MARKERS = {"ps1", "ps2", "ps3", "ps4", "ps5", "playstation", "psx", "psp", "vita"}
XBOX_MARKERS = {"xbox", "x360", "xone", "xsx", "xbsx"}
NINTENDO_MARKERS = {"switch", "nsw", "wii", "wiiu", "3ds", "nds", "nsp", "xci"}


def is_game_path(path: str) -> bool:
    path_obj = Path(path)
    if path_obj.is_file():
        return path_obj.suffix.lower() in GAME_EXTENSIONS
    if not path_obj.is_dir():
        return False

    files = _collect_files(path_obj)
    if not files:
        return False
    suffixes = {file.suffix.lower() for file in files}
    if suffixes & NON_GAME_MEDIA_EXTENSIONS:
        return False
    return bool(suffixes & GAME_EXTENSIONS)


def normalize_game_version(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.lower().startswith("v") else f"v{text}"


def detect_game_version(*values: Any) -> str:
    for value in values:
        text = str(value or "")
        for pattern in (
            r"(?i)\b(?:update|patch|version|ver|build)\s*[-_.:]?\s*v?(\d+(?:[.\-_]\d+)+|\d+)\b",
            r"(?i)\bv(\d+(?:[.\-_]\d+)+|\d+)\b",
        ):
            match = re.search(pattern, text)
            if match:
                return normalize_game_version(match.group(1).replace("_", ".").replace("-", "."))
    return ""


def detect_game_subcategory(path: str, nfo_text: str = "") -> str:
    text = f"{path} {nfo_text}".lower()
    if re.search(r"\b(update|patch)\s*(only|standalone)?\b", text):
        return "update"
    if re.search(r"\bdlc\s*(only|pack|standalone)\b", text):
        return "dlc"
    if "dlc" in text and re.search(r"\b(complete|deluxe|ultimate|goty|game of the year|all dlc|with dlc)\b", text):
        return "full_game_dlc"
    return "full_game"


def game_type_from_platform(platform: Any) -> str:
    text = str(platform or "").lower()
    tokens = set(re.sub(r"[^a-z0-9]+", " ", text).split())
    if tokens & PLAYSTATION_MARKERS or "playstation" in text:
        return "PLAYSTATION"
    if tokens & XBOX_MARKERS:
        return "XBOX"
    if tokens & NINTENDO_MARKERS or "nintendo" in text:
        return "NINTENDO"
    if tokens & PC_MARKERS:
        return "PC"
    return "OTHER"


class GameProcessor:
    def __init__(self, config: dict[str, Any], base_dir: str) -> None:
        self.config = config
        self.base_dir = base_dir

    async def process(self, meta: dict[str, Any]) -> dict[str, Any]:
        root = Path(str(meta["path"]))
        filelist = _collect_files(root)
        if not filelist:
            raise ValueError("No game files found")

        nfo_path, nfo_text = await self._read_nfo(root, filelist)
        source_size = sum(file.stat().st_size for file in filelist if file.exists())
        input_name = root.stem if root.is_file() else root.name

        meta.update({
            "category": "GAMES",
            "is_game": True,
            "is_disc": None,
            "bdinfo": None,
            "mediainfo": {},
            "filelist": [os.fspath(file) for file in filelist],
            "video": os.fspath(_primary_game_file(filelist)),
            "source_size": source_size,
            "resolution": "OTHER",
            "audio": "",
            "subtitle": "",
            "genres": "",
            "keywords": "",
            "overview": "",
            "year": str(meta.get("manual_year") or meta.get("year") or ""),
            "search_year": str(meta.get("manual_year") or meta.get("year") or ""),
            "source": str(meta.get("manual_source") or meta.get("source") or "GAME"),
            "game_input_name": input_name,
            "tmdb": 0,
            "imdb": 0,
            "mal_id": 0,
            "tvdb_id": 0,
            "stream": 0,
            "sd": 0,
        })

        if nfo_path:
            meta["game_nfo_file"] = os.fspath(nfo_path)
            meta["nfo"] = True
        if nfo_text:
            meta["game_nfo_content"] = nfo_text

        if not meta.get("title"):
            meta["title"] = _clean_game_title(input_name)

        manual_version = meta.get("game_version")
        version = normalize_game_version(manual_version) if manual_version else detect_game_version(input_name, nfo_text)
        if version:
            meta["game_version"] = version

        if not meta.get("game_subcategory"):
            meta["game_subcategory"] = detect_game_subcategory(input_name, nfo_text)

        await self._apply_igdb(meta, input_name)

        if not meta.get("platform"):
            meta["platform"] = self._detect_platform(input_name, meta.get("available_platforms", []))
        meta["type"] = game_type_from_platform(meta.get("platform"))

        if not meta.get("year"):
            meta["year"] = ""
            meta["search_year"] = ""

        await self._write_description(meta)
        return meta

    async def _apply_igdb(self, meta: dict[str, Any], input_name: str) -> None:
        igdb = IGDBClient(self.config, self.base_dir)
        if not igdb.configured:
            console.print("[yellow]IGDB credentials not configured; game metadata lookup skipped.[/yellow]")
            return

        selected: Optional[dict[str, Any]] = None
        igdb_id = _first_value(meta.get("igdb_manual"), meta.get("igdb"), meta.get("igdb_id"))
        steam_id = _steam_id_from_text(_first_value(meta.get("steam_manual"), input_name, meta.get("game_nfo_content")))

        if igdb_id:
            selected = await igdb.game_by_id(str(igdb_id))
        if selected is None and steam_id:
            selected = await igdb.game_by_steam_id(steam_id)
        if selected is None:
            results = await igdb.search_games(str(meta.get("title") or input_name))
            selected = await self._select_igdb_result(meta, results)

        if not selected:
            return

        meta["igdb_id"] = selected.get("id", 0)
        if selected.get("name") and not meta.get("manual_title"):
            meta["title"] = selected["name"]

        release_date = selected.get("first_release_date")
        if release_date and not meta.get("manual_year"):
            dt = datetime.datetime.fromtimestamp(int(release_date), datetime.timezone.utc)
            meta["year"] = str(dt.year)
            meta["search_year"] = str(dt.year)
            meta["igdb_release_date"] = dt.strftime("%Y-%m-%d")

        overview = selected.get("summary") or selected.get("storyline") or ""
        if overview:
            meta["overview"] = _clean_text(overview)

        genres = [genre.get("name") for genre in selected.get("genres", []) if isinstance(genre, dict) and genre.get("name")]
        if genres:
            meta["genres"] = ", ".join(genres)
            meta["keywords"] = ", ".join(genres)

        platforms = [platform.get("name") for platform in selected.get("platforms", []) if isinstance(platform, dict) and platform.get("name")]
        if platforms:
            meta["available_platforms"] = platforms
            if not meta.get("platform") and len(platforms) == 1:
                meta["platform"] = platforms[0]

        developers, publishers = [], []
        for company_info in selected.get("involved_companies", []):
            if not isinstance(company_info, dict):
                continue
            company = company_info.get("company") if isinstance(company_info.get("company"), dict) else {}
            name = company.get("name")
            if name and company_info.get("developer"):
                developers.append(name)
            if name and company_info.get("publisher"):
                publishers.append(name)
        if developers:
            meta["developer"] = ", ".join(dict.fromkeys(developers))
        if publishers:
            meta["publisher"] = ", ".join(dict.fromkeys(publishers))

        cover = _igdb_image_url(selected.get("cover"), "t_cover_big")
        artwork = _igdb_image_url((selected.get("artworks") or [None])[0], "t_1080p")
        if cover:
            meta["poster"] = cover
            meta["cover_file"] = cover
        if artwork:
            meta["game_artwork"] = artwork

        screenshots = []
        for screenshot in selected.get("screenshots", [])[:6]:
            url = _igdb_image_url(screenshot, "t_1080p")
            if url:
                screenshots.append({"img_url": url, "raw_url": url, "web_url": url})
        if screenshots:
            meta["image_list"] = screenshots

        steam_url = _steam_url_from_game(selected)
        if steam_url:
            meta["steam_url"] = steam_url

    async def _select_igdb_result(self, meta: dict[str, Any], results: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not results:
            return None
        if len(results) == 1 or meta.get("unattended"):
            return results[0]

        choices = []
        for result in results:
            year = ""
            if result.get("first_release_date"):
                year = f" ({datetime.datetime.fromtimestamp(int(result['first_release_date']), datetime.timezone.utc).year})"
            platforms = ", ".join(platform.get("name") for platform in result.get("platforms", []) if isinstance(platform, dict) and platform.get("name"))
            choices.append(f"{result.get('name')}{year}{f' [{platforms}]' if platforms else ''}")
        choices.append("Skip IGDB match")
        try:
            choice = cli_ui.ask_choice("Select the correct game from IGDB:", choices=choices, sort=False)
        except EOFError:
            return None
        if choice == "Skip IGDB match":
            return None
        return results[choices.index(choice)]

    def _detect_platform(self, input_name: str, platforms: list[str]) -> str:
        lowered = input_name.lower()
        for platform in platforms:
            if str(platform).lower() in lowered:
                return str(platform)
        if any(marker in lowered for marker in NINTENDO_MARKERS):
            return "Nintendo Switch" if "switch" in lowered or "nsw" in lowered else "Nintendo"
        if any(marker in lowered for marker in PLAYSTATION_MARKERS):
            return "PlayStation"
        if any(marker in lowered for marker in XBOX_MARKERS):
            return "Xbox"
        return "PC"

    async def _read_nfo(self, root: Path, files: list[Path]) -> tuple[Optional[Path], str]:
        candidates = [file for file in files if file.suffix.lower() == ".nfo"]
        if root.is_file():
            candidates.extend(root.parent.glob("*.nfo"))
        elif root.is_dir():
            candidates.extend(root.glob("*.nfo"))
        nfo_path = next((path for path in candidates if path.exists()), None)
        if not nfo_path:
            return None, ""
        for encoding in ("utf-8", "latin-1", "cp437"):
            try:
                async with aiofiles.open(nfo_path, encoding=encoding) as nfo_file:
                    return nfo_path, await nfo_file.read()
            except UnicodeDecodeError:
                continue
            except Exception:
                break
        return nfo_path, ""

    async def _write_description(self, meta: dict[str, Any]) -> None:
        lines: list[str] = []
        hero = meta.get("game_artwork") or meta.get("poster")
        if hero:
            lines.extend([f"[center][img]{hero}[/img][/center]", ""])

        info_lines = [
            ("Title", meta.get("title")),
            ("Year", meta.get("year")),
            ("Release Date", meta.get("igdb_release_date")),
            ("Platform", meta.get("platform")),
            ("Release Type", _subcategory_label(meta.get("game_subcategory"))),
            ("Version", meta.get("game_version")),
            ("Developer", meta.get("developer")),
            ("Publisher", meta.get("publisher")),
            ("Genres", meta.get("genres")),
            ("IGDB", f"https://www.igdb.com/games/{meta.get('igdb_id')}" if meta.get("igdb_id") else ""),
            ("Steam", meta.get("steam_url")),
        ]
        info = [f"{label}: {value}" for label, value in info_lines if value]
        if info:
            lines.extend(["[code][b]Game Info[/b]", *info, "[/code]", ""])

        if meta.get("overview"):
            lines.extend(["[b]Summary[/b]", str(meta["overview"]), ""])

        if meta.get("game_nfo_content"):
            lines.append(f"[spoiler=NFO][code]{meta['game_nfo_content']}[/code][/spoiler]")

        tmp_dir = Path(meta["base_dir"]) / "tmp" / meta["uuid"]
        await aiofiles.os.makedirs(tmp_dir, exist_ok=True)
        async with aiofiles.open(tmp_dir / "DESCRIPTION.txt", "w", encoding="utf-8", newline="") as desc_file:
            await desc_file.write("\n".join(lines).strip() + "\n")


def _collect_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(
        [path for path in root.rglob("*") if path.is_file() and not path.name.startswith("._")],
        key=lambda path: os.fspath(path).lower(),
    )


def _primary_game_file(files: list[Path]) -> Path:
    priority = (".exe", ".iso", ".xci", ".nsp", ".pkg", ".rar", ".zip", ".7z")
    for suffix in priority:
        matching = [file for file in files if file.suffix.lower() == suffix]
        if matching:
            return max(matching, key=lambda file: file.stat().st_size)
    return max(files, key=lambda file: file.stat().st_size)


def _clean_game_title(value: str) -> str:
    text = re.sub(r"\[[^\]]+\]|\([^\)]*\b(?:update|dlc|patch|repack|multi)\b[^\)]*\)", " ", value, flags=re.IGNORECASE)
    text = re.sub(r"(?i)\b(?:update|patch|build|version|dlc|incl(?:uding)?|complete|deluxe|gog|steam|rip|repack)\b.*", " ", text)
    text = re.sub(r"[-_.]+", " ", text)
    text = re.sub(r"\b(?:PC|PS[1-5]|XBOX|X360|XONE|XSX|NSW|SWITCH)\b", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _igdb_image_url(value: Any, size: str) -> str:
    if not isinstance(value, dict):
        return ""
    url = str(value.get("url") or "")
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    return re.sub(r"/t_[^/]+/", f"/{size}/", url)


def _steam_url_from_game(game: dict[str, Any]) -> str:
    for website in game.get("websites", []):
        if isinstance(website, dict) and website.get("type") == 13 and website.get("url"):
            return str(website["url"])
    for external in game.get("external_games", []):
        if isinstance(external, dict) and external.get("external_game_source") == 1:
            if external.get("url"):
                return str(external["url"])
            if external.get("uid"):
                return f"https://store.steampowered.com/app/{external['uid']}"
    return ""


def _steam_id_from_text(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"store\.steampowered\.com/app/(\d+)|\bsteam(?:_manual|id)?[:=\s]+(\d{3,})\b", text, re.IGNORECASE)
    return next((group for group in match.groups() if group), "") if match else ""


def _first_value(*values: Any) -> str:
    for value in values:
        if isinstance(value, list):
            value = value[0] if value else ""
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _subcategory_label(value: Any) -> str:
    return {
        "full_game": "Full Game",
        "full_game_dlc": "Full Game + DLC",
        "dlc": "DLC only",
        "update": "Update only",
    }.get(str(value or ""), str(value or ""))
