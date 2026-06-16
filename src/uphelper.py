import json
import os
import re
import sys
from collections.abc import Mapping
from difflib import SequenceMatcher
from typing import Any, Callable, Optional, Union, cast

import aiofiles
import cli_ui

from cogs.redaction import Redaction
from src.bdinfo_comparator import compare_bdinfo, has_bdinfo_content
from src.cleanup import cleanup_manager
from src.console import console
from src.trackersetup import tracker_class_map

Meta = dict[str, Any]
DupeEntry = dict[str, Any]

class UploadHelper:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.default_config = cast(Mapping[str, Any], config.get('DEFAULT', {}))
        if not isinstance(self.default_config, dict):
            raise ValueError("'DEFAULT' config section must be a dict")
        self.tracker_class_map = cast(Mapping[str, Any], tracker_class_map)

    @staticmethod
    def _size_to_bytes(size_value: Any) -> Optional[float]:
        if size_value is None:
            return None

        if isinstance(size_value, (int, float)):
            return float(size_value)

        size_text = str(size_value).strip()
        if not size_text:
            return None

        match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*([kmgt]?i?b)?", size_text, flags=re.IGNORECASE)
        if not match:
            return None

        try:
            number = float(match.group(1).replace(',', '.'))
        except ValueError:
            return None

        unit = (match.group(2) or "b").lower()
        multipliers = {
            "b": 1,
            "kb": 1000,
            "mb": 1000 ** 2,
            "gb": 1000 ** 3,
            "tb": 1000 ** 4,
            "kib": 1024,
            "mib": 1024 ** 2,
            "gib": 1024 ** 3,
            "tib": 1024 ** 4,
        }
        return number * multipliers.get(unit, 1)

    @staticmethod
    def _format_size(size_value: Any) -> Optional[str]:
        size = UploadHelper._size_to_bytes(size_value)
        if size is None:
            return None

        gib = 1024 ** 3
        mib = 1024 ** 2
        if size < gib:
            return f"{size / mib:.2f} MiB"
        return f"{size / gib:.2f} GiB"

    @staticmethod
    def _format_source_size(size_bytes: Any) -> str:
        return UploadHelper._format_size(size_bytes) or "0.00 MiB"

    @staticmethod
    def _mi_value(value: Any) -> str:
        if value in (None, "", {}):
            return ""
        return str(value).strip()

    @staticmethod
    def _format_bitrate(value: Any) -> Optional[str]:
        value_text = UploadHelper._mi_value(value)
        if not value_text:
            return None

        try:
            bitrate = float(value_text)
        except ValueError:
            return value_text

        if bitrate >= 1_000_000:
            return f"{bitrate / 1_000_000:.2f} Mb/s"
        if bitrate >= 1_000:
            return f"{bitrate / 1_000:.0f} kb/s"
        return f"{bitrate:.0f} b/s"

    @staticmethod
    def _format_channels(value: Any) -> str:
        channels = UploadHelper._mi_value(value)
        if not channels:
            return ""
        try:
            channel_count = float(channels)
        except ValueError:
            return channels
        if channel_count.is_integer():
            return f"{int(channel_count)}ch"
        return f"{channel_count:g}ch"

    @staticmethod
    def _duration_to_seconds(value: Any) -> Optional[int]:
        value_text = UploadHelper._mi_value(value)
        if not value_text:
            return None

        try:
            duration = float(value_text)
            # MediaInfo JSON often stores Duration in milliseconds; UA video_duration is seconds.
            if duration > 10000:
                duration = duration / 1000
            return int(round(duration))
        except ValueError:
            pass

        hours_match = re.search(r'(\d+)\s*h', value_text, flags=re.IGNORECASE)
        minutes_match = re.search(r'(\d+)\s*min', value_text, flags=re.IGNORECASE)
        seconds_match = re.search(r'(\d+)\s*s', value_text, flags=re.IGNORECASE)
        if hours_match or minutes_match or seconds_match:
            hours = int(hours_match.group(1)) if hours_match else 0
            minutes = int(minutes_match.group(1)) if minutes_match else 0
            seconds = int(seconds_match.group(1)) if seconds_match else 0
            return hours * 3600 + minutes * 60 + seconds

        colon_match = re.search(r'(?:(\d+):)?(\d{1,2}):(\d{2})', value_text)
        if colon_match:
            hours = int(colon_match.group(1) or 0)
            minutes = int(colon_match.group(2))
            seconds = int(colon_match.group(3))
            return hours * 3600 + minutes * 60 + seconds

        return None

    @staticmethod
    def _format_duration(value: Any, *, value_is_minutes: bool = False) -> Optional[str]:
        if value_is_minutes:
            try:
                value = float(UploadHelper._mi_value(value) or 0) * 60
            except ValueError:
                return None
        seconds_total = UploadHelper._duration_to_seconds(value)
        if seconds_total is None:
            return None
        minutes, seconds = divmod(seconds_total, 60)
        return f"{minutes}m {seconds:02d}s"

    @staticmethod
    def _get_mediainfo_tracks(meta: Meta) -> list[dict[str, Any]]:
        mediainfo = cast(dict[str, Any], meta.get('mediainfo', {}))
        media = cast(dict[str, Any], mediainfo.get('media', {}))
        tracks = media.get('track', [])
        if isinstance(tracks, list):
            return [cast(dict[str, Any], track) for track in tracks if isinstance(track, dict)]
        return []

    @staticmethod
    def _is_english_language(value: Any) -> bool:
        language = UploadHelper._mi_value(value).lower().replace("_", "-")
        if not language:
            return False
        language_parts = [part.strip() for part in re.split(r"[,;/]", language) if part.strip()]
        if not language_parts:
            language_parts = [language]
        return any(
            part in {"en", "eng", "english"} or part.startswith("en-")
            for part in language_parts
        )

    @staticmethod
    def _needs_missing_english_sub_warning(meta: Meta) -> bool:
        tracks = UploadHelper._get_mediainfo_tracks(meta)
        audio_tracks = [track for track in tracks if track.get('@type') == 'Audio']
        subtitle_tracks = [track for track in tracks if track.get('@type') == 'Text']
        if not audio_tracks:
            return False

        has_english_audio = any(UploadHelper._is_english_language(track.get('Language')) for track in audio_tracks)
        has_english_subtitle = any(UploadHelper._is_english_language(track.get('Language')) for track in subtitle_tracks)
        return not has_english_audio and not has_english_subtitle

    @staticmethod
    def _needs_low_res_h265_warning(meta: Meta) -> bool:
        resolution_match = re.search(r'\b(\d{3,4})[pi]\b', str(meta.get('resolution', '')), flags=re.IGNORECASE)
        if not resolution_match or int(resolution_match.group(1)) > 1080:
            return False

        codec_parts = [
            meta.get('video_codec'),
            meta.get('video_encode'),
            meta.get('name'),
            meta.get('uuid'),
        ]
        video_tracks = [track for track in UploadHelper._get_mediainfo_tracks(meta) if track.get('@type') == 'Video']
        if video_tracks:
            video_track = video_tracks[0]
            codec_parts.extend([
                video_track.get('Format'),
                video_track.get('Format_Commercial_IfAny'),
                video_track.get('CodecID'),
            ])

        codec_text = " ".join(str(part) for part in codec_parts if part not in (None, "", {})).lower()
        return bool(re.search(r'\b(?:x265|h[ ._-]?265|hevc)\b', codec_text))

    @staticmethod
    def _format_confirm_media_lines(meta: Meta) -> list[str]:
        tracks = UploadHelper._get_mediainfo_tracks(meta)
        if not tracks:
            return []

        lines: list[str] = []
        video_tracks = [track for track in tracks if track.get('@type') == 'Video']
        audio_tracks = [track for track in tracks if track.get('@type') == 'Audio']
        subtitle_tracks = [track for track in tracks if track.get('@type') == 'Text']

        duration = None
        general_tracks = [track for track in tracks if track.get('@type') == 'General']
        if general_tracks:
            duration = UploadHelper._format_duration(general_tracks[0].get('Duration'))
        if duration is None and video_tracks:
            duration = UploadHelper._format_duration(video_tracks[0].get('Duration'))
        if duration is None:
            duration = UploadHelper._format_duration(meta.get('video_duration'), value_is_minutes=True)
        if duration:
            lines.append(f"[bold]Duration:[/bold] {duration}")

        if video_tracks:
            video_bitrate = UploadHelper._format_bitrate(video_tracks[0].get('BitRate'))
            if video_bitrate:
                lines.append(f"[bold]Video bitrate:[/bold] {video_bitrate}")

        for index, track in enumerate(audio_tracks, start=1):
            language = UploadHelper._mi_value(track.get('Language')) or "Unknown"
            codec = (
                UploadHelper._mi_value(track.get('Format_Commercial_IfAny'))
                or UploadHelper._mi_value(track.get('Format'))
            )
            channels = UploadHelper._format_channels(track.get('Channels'))
            bitrate = UploadHelper._format_bitrate(track.get('BitRate'))
            title = UploadHelper._mi_value(track.get('Title'))
            details = [part for part in (language, codec, channels, bitrate, title) if part]
            lines.append(f"[bold]Audio {index}:[/bold] {' / '.join(details)}")

        if subtitle_tracks:
            subtitles: list[str] = []
            seen_subtitles: set[str] = set()
            for track in subtitle_tracks:
                language = UploadHelper._mi_value(track.get('Language')) or "Unknown"
                flags = [
                    flag
                    for flag, key in (("Default", "Default"), ("Forced", "Forced"))
                    if UploadHelper._mi_value(track.get(key)).lower() == "yes"
                ]
                subtitle = language
                if flags:
                    subtitle = f"{subtitle} ({', '.join(flags)})"
                if subtitle not in seen_subtitles:
                    seen_subtitles.add(subtitle)
                    subtitles.append(subtitle)
            if subtitles:
                lines.append(f"[bold]Subtitles:[/bold] {', '.join(subtitles)}")

        return lines

    async def dupe_check(self, dupes: list[Union[DupeEntry, str]], meta: Meta, tracker_name: str) -> tuple[bool, Meta]:
        source_size = self._format_size(meta.get('source_size'))
        if source_size is None and meta.get('is_disc') != "BDMV":
            mediainfo = cast(dict[str, Any], meta.get('mediainfo', {}))
            tracks = cast(list[dict[str, Any]], mediainfo.get('media', {}).get('track', []))
            if tracks:
                source_size = self._format_size(tracks[0].get('FileSize'))

        def _format_dupe_size(size: str) -> str:
            if source_size is not None and size == source_size:
                return f"[bold red]{size}[/bold red]"
            return size

        def _print_low_res_h265_warning() -> None:
            if self._needs_low_res_h265_warning(meta):
                console.print("[bold red]Warning: 1080p or lower x265/H.265/HEVC encodes may be forbidden on some trackers.[/bold red]")

        def _format_dupe(entry: Union[DupeEntry, str]) -> str:
            if isinstance(entry, dict):
                name = str(entry.get('name', ''))
                size = self._format_size(entry.get('size'))
                link = entry.get('link')
                parts = [name]
                if size:
                    parts.append(f"Size: {_format_dupe_size(size)}")
                if link is not None and str(link):
                    parts.append(str(link))
                return " - ".join(part for part in parts if part)
            return str(entry)

        def _entry_resolution(entry: Union[DupeEntry, str]) -> int:
            text = ""
            if isinstance(entry, dict):
                text = " ".join(
                    str(value)
                    for value in (
                        entry.get('res'),
                        entry.get('resolution'),
                        entry.get('name'),
                    )
                    if value not in (None, "")
                )
            else:
                text = str(entry)

            match = re.search(r'\b(4320|2160|1440|1080|720|576|480)[pi]\b', text, flags=re.IGNORECASE)
            return int(match.group(1)) if match else -1

        def _resolution_sort_key(entry: Union[DupeEntry, str]) -> tuple[int, str]:
            text = ""
            if isinstance(entry, dict):
                text = str(entry.get('name') or '')
            else:
                text = str(entry)
            return (-_entry_resolution(entry), text.lower())

        def _print_other_uploads() -> None:
            other_uploads = [
                entry
                for entry in cast(list[Union[DupeEntry, str]], meta.get(f'{tracker_name}_other_uploads', []))
                if isinstance(entry, (dict, str))
            ]
            if not other_uploads:
                return
            other_uploads = sorted(other_uploads, key=_resolution_sort_key)

            console.print()
            console.print(f"[bold blue]Other uploads:[/bold blue] [yellow]{tracker_name}[/yellow]")
            target_resolution = _entry_resolution(str(meta.get('resolution', '')))
            lines = []
            for entry in other_uploads:
                line = _format_dupe(entry)
                if target_resolution != -1 and _entry_resolution(entry) == target_resolution:
                    lines.append(f"[bold orange1]{line}[/bold orange1]")
                else:
                    lines.append(f"[bold cyan]{line}[/bold cyan]")
            console.print("\n".join(lines))

        dupes_list: list[Union[DupeEntry, str]] = dupes
        upload: bool = False
        meta['were_trumping'] = False
        if not dupes_list:
            _print_other_uploads()
            if meta['debug']:
                console.print(f"[green]No dupes found at[/green] [yellow]{tracker_name}[/yellow]")
            return False,  meta
        else:
            tracker_class_factory = cast(Callable[..., Any], self.tracker_class_map[tracker_name])
            tracker_class = tracker_class_factory(config=self.config)
            try:
                tracker_rename = await tracker_class.get_name(meta)
            except Exception:
                try:
                    tracker_rename = await tracker_class.edit_name(meta)
                except Exception:
                    tracker_rename = None
            display_name: Optional[str] = None
            if tracker_rename is not None:
                if isinstance(tracker_rename, dict) and 'name' in tracker_rename:
                    tracker_rename_dict = cast(dict[str, Any], tracker_rename)
                    display_name = str(tracker_rename_dict.get('name', ''))
                elif isinstance(tracker_rename, str):
                    display_name = tracker_rename

            # Show naming change before dupe prompts so user knows what the final name will be
            if display_name is not None and display_name != "" and display_name != meta.get('name', ''):
                console.print(f"[bold yellow]{tracker_name} applies a naming change for this release: [green]{display_name}[/green][/bold yellow]")

            trumpable_text = None
            if meta.get('trumpable_id') or (meta.get('season_pack_contains_episode') and meta.get(f'{tracker_name}_matched_episode_ids', [])):
                trumpable_dupes = [
                    entry
                    for entry in dupes_list
                    if isinstance(entry, dict) and entry.get('trumpable')
                ]
                if trumpable_dupes:
                    trumpable_text = "\n".join(_format_dupe(d) for d in trumpable_dupes)
                    console.print("[bold red]Trumpable found![/bold red]")
                elif meta.get('season_pack_contains_episode') and meta.get(f'{tracker_name}_matched_episode_ids', []):
                    matched_episodes = cast(list[DupeEntry], meta.get(f'{tracker_name}_matched_episode_ids', []))
                    user_tag = str(meta.get('tag', '')).lstrip('-').lower()  # Remove leading dash for comparison

                    # Try to find a release with matching tag
                    selected_match = None
                    tag_matched = False
                    if user_tag:
                        for ep in matched_episodes:
                            ep_name = str(ep.get('name', '')).lower()
                            # Tag typically appears at end of name like "H.265-ETHEL"
                            if ep_name.endswith(user_tag) or f"-{user_tag}" in ep_name:
                                selected_match = ep
                                tag_matched = True
                                break

                    # Fall back to first match if no tag match found
                    if not selected_match:
                        selected_match = matched_episodes[0]

                    trumpable_text = _format_dupe(selected_match)
                    console.print("[bold red]Trumpable found based on episode matching![/bold red]")

                    if user_tag and not tag_matched:
                        console.print(f"[yellow]Note: No release found with matching tag '{meta.get('tag')}'. Selected release may be from a different group.[/yellow]")

            if (not meta['unattended'] or (meta['unattended'] and meta.get('unattended_confirm', False))) and not meta.get('ask_dupe', False):
                dupe_text = "\n".join(_format_dupe(d) for d in dupes_list)

                if trumpable_text and (meta.get('trumpable_id') or (meta.get('season_pack_contains_episode') and meta.get(f'{tracker_name}_matched_episode_ids', []))):
                    console.print(f"[bold cyan]{trumpable_text}[/bold cyan]")
                    console.print("[yellow]Please check the trumpable entries above to see if you want to upload[/yellow]")
                    console.print("[yellow]You will have the option to report the trumpable torrent if you upload.[/yellow]")
                    if meta.get('dupe', False) is False:
                        try:
                            upload = cli_ui.ask_yes_no("Are you trumping this release?", default=False)
                            if upload:
                                meta['we_asked'] = True
                                meta['were_trumping'] = True
                                if not meta.get(f'{tracker_name}_trumpable_id'):
                                    meta[f'{tracker_name}_trumpable_id'] = meta.get(f'{tracker_name}_matched_id', None)
                                if meta.get('filename_match', False) and meta.get('file_count_match', False):
                                    meta['trump_reason'] = 'exact_match'
                                else:
                                    meta['trump_reason'] = 'trumpable_release'
                                if meta['debug']:
                                    console.print(f"[bold green]Trump reason: {meta['trump_reason']} on {tracker_name}[/bold green]")
                            else:
                                # For season packs: individual episodes are only in dupes for trumping purposes.
                                # If user declines to trump, filter them out so they aren't shown as "potential dupes"
                                # (they wouldn't match season/episode anyway).
                                if meta.get('tv_pack') and meta.get('season_pack_contains_episode') and meta.get(f'{tracker_name}_matched_episode_ids', []):
                                    matched_ids = {ep.get('id') for ep in meta.get(f'{tracker_name}_matched_episode_ids', []) if ep.get('id')}
                                    dupes_list = [
                                        d for d in dupes_list
                                        if not (isinstance(d, dict) and d.get('id') in matched_ids)
                                    ]
                                    # Clear tracker-specific matched_episode_ids since we're not trumping
                                    meta[f'{tracker_name}_matched_episode_ids'] = []
                        except EOFError:
                            console.print("\n[red]Exiting on user request (Ctrl+C)[/red]")
                            await cleanup_manager.cleanup()
                            cleanup_manager.reset_terminal()
                            sys.exit(1)

                if not meta.get('were_trumping', False):
                    if meta.get('filename_match', False) and meta.get('file_count_match', False):
                        exact_match_text = str(meta["filename_match"])
                        exact_match_size = self._format_size(meta.get(f'{tracker_name}_matched_size'))
                        if exact_match_size and "Size:" not in exact_match_text:
                            exact_match_text = f"{exact_match_text} - Size: {_format_dupe_size(exact_match_size)}"
                        console.print(f'[bold red]Exact match found! - {exact_match_text}[/bold red]')
                        try:
                            if tracker_name in ["AITHER", "LST"]:
                                console.print(f"[yellow]{tracker_name} supports automatic trumping of exact matches, if the file is allowed to be trumped.[/yellow]")
                                upload = cli_ui.ask_yes_no("Are you trumping this exact match?", default=False)
                                if upload:
                                    meta['we_asked'] = True
                                    meta['were_trumping'] = True
                                    meta['trump_reason'] = 'exact_match'
                                    if not meta.get(f'{tracker_name}_trumpable_id'):
                                        meta[f'{tracker_name}_trumpable_id'] = meta.get(f'{tracker_name}_matched_id', None)
                            else:
                                _print_low_res_h265_warning()
                                upload = cli_ui.ask_yes_no(f"Upload to {tracker_name} anyway?", default=False)
                                meta['we_asked'] = True
                        except EOFError:
                            console.print("\n[red]Exiting on user request (Ctrl+C)[/red]")
                            await cleanup_manager.cleanup()
                            cleanup_manager.reset_terminal()
                            sys.exit(1)
                    elif dupes_list:
                        # Rebuild dupe_text in case dupes was filtered after trump decline
                        dupe_text = "\n".join(_format_dupe(d) for d in dupes_list)
                        if meta.get('season_pack_exists', False):
                            # Display only the matched season pack info from dupe_checking
                            season_pack_name = meta.get('season_pack_name', '')
                            season_pack_link = meta.get('season_pack_link')
                            season_pack_text = _format_dupe({
                                'name': season_pack_name,
                                'link': season_pack_link,
                                'size': meta.get('season_pack_size'),
                            })
                            console.print(f"[yellow]Note: A season pack exists on {tracker_name}[/yellow]")
                            console.print("[yellow]Ensure your upload is not part of that season pack, or is otherwise allowed.[/yellow]")
                            console.print()
                            console.print(f"[bold cyan]{season_pack_text}[/bold cyan]")
                        else:
                            console.print(f"[bold blue]Check if these are actually dupes from {tracker_name}:[/bold blue]")
                            console.print(f"[bold cyan]{dupe_text}[/bold cyan]")
                            _print_other_uploads()
                        if meta.get('dupe', False) is False:
                            try:
                                if meta.get('is_disc') == "BDMV":
                                    self.ask_bdinfo_comparison(meta, dupes_list, tracker_name)
                                _print_low_res_h265_warning()
                                upload = cli_ui.ask_yes_no(f"Upload to {tracker_name} anyway?", default=False)
                                meta['we_asked'] = True
                            except EOFError:
                                console.print("\n[red]Exiting on user request (Ctrl+C)[/red]")
                                await cleanup_manager.cleanup()
                                cleanup_manager.reset_terminal()
                                sys.exit(1)
                        else:
                            upload = True
                    else:
                        # dupes list was emptied after filtering (e.g., season pack declined trump, no other dupes)
                        upload = True

            else:
                upload = meta.get('dupe', False) is not False

            display_name = display_name if display_name is not None else str(meta.get('name', ''))
            display_name = str(display_name)

            if tracker_name in ["BHD"]:
                if meta['debug']:
                    console.print("[yellow]BHD cross seeding check[/yellow]")
                tracker_download_link = meta.get(f'{tracker_name}_matched_download')
                # Ensure display_name is a string before using 'in' operator
                if display_name:
                    edition = meta.get('edition', '')
                    region = meta.get('region', '')
                    if edition and edition in display_name:
                        display_name = display_name.replace(f"{edition} ", "")
                    if region and region in display_name:
                        display_name = display_name.replace(f"{region} ", "")
                for d in dupes_list:
                    if isinstance(d, dict):
                        entry_name = str(d.get('name', '')).lower()
                        similarity = SequenceMatcher(None, entry_name, display_name.lower().strip()).ratio()
                        if similarity > 0.9 and meta.get('size_match', False) and tracker_download_link:
                            meta[f'{tracker_name}_cross_seed'] = tracker_download_link
                            if meta['debug']:
                                console.print(f'[bold red]Cross-seed link saved for {tracker_name}: {Redaction.redact_private_info(tracker_download_link)}.[/bold red]')
                            break

            elif meta.get('filename_match', False) and meta.get('file_count_match', False):
                if meta['debug']:
                    console.print(f"[yellow]{tracker_name} filename and file count cross seeding check[/yellow]")
                tracker_download_link = meta.get(f'{tracker_name}_matched_download')
                for d in dupes_list:
                    if isinstance(d, dict) and tracker_download_link:
                        meta[f'{tracker_name}_cross_seed'] = tracker_download_link
                        if meta['debug']:
                            console.print(f'[bold red]Cross-seed link saved for {tracker_name}: {Redaction.redact_private_info(tracker_download_link)}.[/bold red]')
                        break

            elif meta.get('size_match', False):
                if meta['debug']:
                    console.print(f"[yellow]{tracker_name} size cross seeding check[/yellow]")
                tracker_download_link = meta.get(f'{tracker_name}_matched_download')
                for d in dupes_list:
                    if isinstance(d, dict):
                        entry_name = str(d.get('name', '')).lower()
                        similarity = SequenceMatcher(None, entry_name, display_name.lower().strip()).ratio()
                        if meta['debug']:
                            console.print(f"[debug] Comparing sizes with similarity {similarity:.4f}")
                        if similarity > 0.9 and tracker_download_link:
                            meta[f'{tracker_name}_cross_seed'] = tracker_download_link
                            if meta['debug']:
                                console.print(f'[bold red]Cross-seed link saved for {tracker_name}: {Redaction.redact_private_info(tracker_download_link)}.[/bold red]')
                            break

            if upload is False:
                return True, meta
            else:
                for each in dupes_list:
                    each_name = str(each.get('name')) if isinstance(each, dict) else str(each)
                    if each_name == meta['name']:
                        meta['name'] = f"{meta['name']} DUPE?"

                return False, meta

    def ask_bdinfo_comparison(self, meta: Meta, dupes: list[Union[DupeEntry, str]], tracker_name: str) -> None:
        """
        Check if any duplicate has BDInfo content and ask the user
        if they want to perform a comparison.
        """
        possible = any(
            isinstance(entry, dict) and has_bdinfo_content(entry)
            for entry in dupes
        )

        if not possible:
            return

        question = (
            "\033[1;35mFound BDInfo content in potential duplicates."
            "\033[0m Perform a comparison?"
        )
        if cli_ui.ask_yes_no(question, default=True):
            warnings: list[str] = []
            results: list[str] = []

            for entry in dupes:
                if not isinstance(entry, dict):
                    continue

                warning_message, results_message = compare_bdinfo(meta, entry, tracker_name)

                if warning_message:
                    warnings.append(warning_message)
                if results_message:
                    results.append(results_message)

            if warnings:
                console.print()
                console.print("\n\n".join(warnings), soft_wrap=True)

            if results:
                console.print()
                console.print("\n".join(results), soft_wrap=True)
                console.print()

    async def get_confirmation(self, meta: Meta) -> Union[bool, str]:
        confirm: Union[bool, str] = False
        if meta['debug'] is True:
            console.print("[bold red]DEBUG: True - Will not actually upload!")
            console.print(f"Prep material saved to {meta['base_dir']}/tmp/{meta['uuid']}")
        if meta.get('category') == 'BOOK' or meta.get('is_book'):
            console.print()
            console.print("[bold yellow]Book Info[/bold yellow]")
            console.print(f"[bold]Title:[/bold] {meta.get('title', '')}")
            console.print(f"[bold]Author:[/bold] {meta.get('author', '')}")
            if meta.get('narrator'):
                console.print(f"[bold]Narrator:[/bold] {meta.get('narrator')}")
            if meta.get('year'):
                console.print(f"[bold]Year:[/bold] {meta.get('year')}")
            if meta.get('edition'):
                console.print(f"[bold]Edition:[/bold] {meta.get('edition')}")
            if meta.get('book_language'):
                console.print(f"[bold]Language:[/bold] {meta.get('book_language')}")
            if meta.get('publisher'):
                console.print(f"[bold]Publisher:[/bold] {meta.get('publisher')}")
            if meta.get('isbn'):
                console.print(f"[bold]ISBN:[/bold] {meta.get('isbn')}")
            if meta.get('book_series'):
                series_info = str(meta.get('book_series'))
                if meta.get('book_number'):
                    series_info = f"{series_info} #{meta.get('book_number')}"
                console.print(f"[bold]Series:[/bold] {series_info}")
            release_flags = [
                flag for flag, enabled in (
                    ('Retail', meta.get('retail')),
                    ('Scan', meta.get('scan')),
                    ('OCR', meta.get('ocr')),
                    ('Abridged', meta.get('abridged')),
                    ('Unabridged', meta.get('unabridged')),
                ) if enabled
            ]
            if release_flags:
                console.print(f"[bold]Release:[/bold] {', '.join(release_flags)}")
            console.print(f"[bold]Category:[/bold] {'AUDIOBOOK' if meta.get('is_audiobook') else 'BOOK'}")
            console.print()
            info_parts = [str(part) for part in [
                meta.get('source', ''),
                meta.get('type', ''),
                meta.get('audiobook_duration_formatted', ''),
                f"{meta.get('audiobook_bitrate')} kb/s" if meta.get('audiobook_bitrate') else '',
            ] if part]
            if info_parts:
                console.print(' / '.join(info_parts))
                console.print()
            console.print(f"[bold]Name:[/bold] {meta['name']}")
            if meta.get('google_books_link') and meta.get('google_books_link_source') == 'api':
                console.print(f"[bold]Google Books:[/bold] {meta['google_books_link']}")
            if meta.get('open_library_link'):
                console.print(f"[bold]Open Library:[/bold] {meta['open_library_link']}")
            if meta.get('mam_link'):
                console.print(f"[bold]MyAnonamouse:[/bold] {meta['mam_link']}")
            console.print(f"[bold]Size:[/bold] {self._format_source_size(meta.get('source_size'))}")
            if meta.get('unattended', False) and not meta.get('unattended_confirm', False) and not meta.get('emby_debug', False):
                if meta['debug'] is True:
                    console.print("[bold yellow]Unattended mode is enabled, skipping confirmation.[/bold yellow]")
                return True
            confirm_input = console.input("[bold green]Is this correct?[/bold green] [yellow]y/N/skip[/yellow]: ").strip().lower()
            return "skip" if confirm_input in {"s", "skip"} else confirm_input == 'y'
        if meta.get('category') == 'MUSIC' or meta.get('is_music'):
            console.print()
            console.print("[bold yellow]Music Info[/bold yellow]")
            console.print(f"[bold]Artist:[/bold] {meta.get('artist', '')}")
            console.print(f"[bold]Album:[/bold] {meta.get('album', '')}")
            if meta.get('year'):
                console.print(f"[bold]Year:[/bold] {meta.get('year')}")
            if meta.get('genres'):
                console.print(f"[bold]Genre:[/bold] {meta.get('genres')}")
            console.print(f"[bold]Category:[/bold] MUSIC")
            console.print()
            info_parts = [str(part) for part in [
                meta.get('service', ''),
                meta.get('source', ''),
                meta.get('type', ''),
                f"{meta.get('bit_depth')}bit" if meta.get('bit_depth') and not meta.get('is_lossy') else '',
                meta.get('sampling_rate', '') if not meta.get('is_lossy') else meta.get('bitrate', ''),
            ] if part]
            if info_parts:
                console.print(' / '.join(info_parts))
                console.print()
            console.print(f"[bold]Name:[/bold] {meta['name']}")
            console.print(f"[bold]Tracks:[/bold] {meta.get('track_count', 0)}")
            if meta.get('mbid'):
                console.print(f"[bold]MusicBrainz:[/bold] https://musicbrainz.org/release/{meta['mbid']}")
            if meta.get('discogs_id'):
                console.print(f"[bold]Discogs:[/bold] https://www.discogs.com/release/{meta['discogs_id']}")
            if meta.get('deezer_info', {}).get('link'):
                console.print(f"[bold]Deezer:[/bold] {meta['deezer_info']['link']}")
            console.print(f"[bold]Size:[/bold] {self._format_source_size(meta.get('source_size'))}")
            if meta.get('unattended', False) and not meta.get('unattended_confirm', False) and not meta.get('emby_debug', False):
                if meta['debug'] is True:
                    console.print("[bold yellow]Unattended mode is enabled, skipping confirmation.[/bold yellow]")
                return True
            confirm_input = console.input("[bold green]Is this correct?[/bold green] [yellow]y/N/skip[/yellow]: ").strip().lower()
            return "skip" if confirm_input in {"s", "skip"} else confirm_input == 'y'
        if meta.get('category') == 'GAMES' or meta.get('is_game'):
            console.print()
            console.print("[bold yellow]Game Info[/bold yellow]")
            console.print(f"[bold]Title:[/bold] {meta.get('title', '')}")
            if meta.get('year'):
                console.print(f"[bold]Year:[/bold] {meta.get('year')}")
            if meta.get('platform'):
                console.print(f"[bold]Platform:[/bold] {meta.get('platform')}")
            if meta.get('game_subcategory'):
                subcategory = {
                    'full_game': 'Full Game',
                    'full_game_dlc': 'Full Game + DLC',
                    'dlc': 'DLC only',
                    'update': 'Update only',
                }.get(str(meta.get('game_subcategory')), str(meta.get('game_subcategory')))
                console.print(f"[bold]Release Type:[/bold] {subcategory}")
            if meta.get('game_version'):
                console.print(f"[bold]Version:[/bold] {meta.get('game_version')}")
            if meta.get('developer'):
                console.print(f"[bold]Developer:[/bold] {meta.get('developer')}")
            if meta.get('publisher'):
                console.print(f"[bold]Publisher:[/bold] {meta.get('publisher')}")
            if meta.get('genres'):
                console.print(f"[bold]Genre:[/bold] {meta.get('genres')}")
            console.print("[bold]Category:[/bold] GAMES")
            console.print()
            console.print(f"[bold]Name:[/bold] {meta['name']}")
            if meta.get('igdb_id'):
                console.print(f"[bold]IGDB:[/bold] https://www.igdb.com/games/{meta['igdb_id']}")
            if meta.get('steam_url'):
                console.print(f"[bold]Steam:[/bold] {meta['steam_url']}")
            console.print(f"[bold]Files:[/bold] {len(meta.get('filelist') or [])}")
            console.print(f"[bold]Size:[/bold] {self._format_source_size(meta.get('source_size'))}")
            if meta.get('unattended', False) and not meta.get('unattended_confirm', False) and not meta.get('emby_debug', False):
                if meta['debug'] is True:
                    console.print("[bold yellow]Unattended mode is enabled, skipping confirmation.[/bold yellow]")
                return True
            confirm_input = console.input("[bold green]Is this correct?[/bold green] [yellow]y/N/skip[/yellow]: ").strip().lower()
            return "skip" if confirm_input in {"s", "skip"} else confirm_input == 'y'
        console.print()
        console.print("[bold yellow]Database Info[/bold yellow]")
        console.print(f"[bold]Title:[/bold] {meta['title']} ({meta['year']})")
        console.print()
        if not meta.get('emby', False):
            console.print(f"[bold]Overview:[/bold] {meta['overview'][:100]}....")
            console.print()
            if meta.get('category') == 'TV' and not meta.get('tv_pack') and meta.get('auto_episode_title'):
                console.print(f"[bold]Episode Title:[/bold] {meta['auto_episode_title']}")
                console.print()
            if meta.get('category') == 'TV' and not meta.get('tv_pack') and meta.get('overview_meta'):
                console.print(f"[bold]Episode overview:[/bold] {meta['overview_meta']}")
                console.print()
            console.print(f"[bold]Genre:[/bold] {meta['genres']}")
            console.print()
            if str(meta.get('demographic', '')) != '':
                console.print(f"[bold]Demographic:[/bold] {meta['demographic']}")
                console.print()
        console.print(f"[bold]Category:[/bold] {meta['category']}")
        console.print()
        if meta.get('emby_debug', False):
            if int(meta.get('original_imdb', 0)) != 0:
                imdb = str(meta.get('original_imdb', 0)).zfill(7)
                console.print(f"[bold]IMDB:[/bold] https://www.imdb.com/title/tt{imdb}")
            if int(meta.get('original_tmdb', 0)) != 0:
                console.print(f"[bold]TMDB:[/bold] https://www.themoviedb.org/{meta['category'].lower()}/{meta['original_tmdb']}")
            if int(meta.get('original_tvdb', 0)) != 0:
                console.print(f"[bold]TVDB:[/bold] https://www.thetvdb.com/?id={meta['original_tvdb']}&tab=series")
            if int(meta.get('original_tvmaze', 0)) != 0:
                console.print(f"[bold]TVMaze:[/bold] https://www.tvmaze.com/shows/{meta['original_tvmaze']}")
            if int(meta.get('original_mal', 0)) != 0:
                console.print(f"[bold]MAL:[/bold] https://myanimelist.net/anime/{meta['original_mal']}")
        else:
            if int(meta.get('tmdb_id') or 0) != 0:
                console.print(f"[bold]TMDB:[/bold] https://www.themoviedb.org/{meta['category'].lower()}/{meta['tmdb_id']}")
            if int(meta.get('imdb_id') or 0) != 0:
                console.print(f"[bold]IMDB:[/bold] https://www.imdb.com/title/tt{meta['imdb']}")
            if int(meta.get('tvdb_id') or 0) != 0:
                console.print(f"[bold]TVDB:[/bold] https://www.thetvdb.com/?id={meta['tvdb_id']}&tab=series")
            if int(meta.get('tvmaze_id') or 0) != 0:
                console.print(f"[bold]TVMaze:[/bold] https://www.tvmaze.com/shows/{meta['tvmaze_id']}")
            if int(meta.get('mal_id') or 0) != 0:
                console.print(f"[bold]MAL:[/bold] https://myanimelist.net/anime/{meta['mal_id']}")
        console.print()
        if not meta.get('emby', False):
            if int(meta.get('freeleech', 0)) != 0:
                console.print(f"[bold]Freeleech:[/bold] {meta['freeleech']}")

            info_parts: list[str] = []
            info_parts.append(str(meta['source'] if meta['is_disc'] == 'DVD' else meta['resolution']))
            info_parts.append(str(meta['type']))
            if meta.get('tag', ''):
                info_parts.append(str(meta['tag'])[1:])
            if meta.get('region', ''):
                info_parts.append(str(meta['region']))
            if meta.get('distributor', ''):
                info_parts.append(str(meta['distributor']))
            console.print(' / '.join(info_parts))

            if meta.get('personalrelease', False) is True:
                console.print("[bold green]Personal Release![/bold green]")
            console.print()

        if meta.get('unattended', False) and not meta.get('unattended_confirm', False) and not meta.get('emby_debug', False):
            if meta['debug'] is True:
                console.print("[bold yellow]Unattended mode is enabled, skipping confirmation.[/bold yellow]")
            return True
        else:
            if not meta.get('emby', False):
                await self.get_missing(meta)
                ring_the_bell = "\a" if bool(self.default_config.get("sfx_on_prompt", True)) else ""
                if ring_the_bell:
                    console.print(ring_the_bell)

            if meta.get('is disc', False) is True:
                meta['keep_folder'] = False

            if meta.get('keep_folder') and meta['isdir']:
                kf_confirm = console.input("[bold yellow]You specified --keep-folder. Uploading in folders might not be allowed.[/bold yellow] [green]Proceed? y/N: [/green]").strip().lower()
                if kf_confirm != 'y':
                    console.print("[bold red]Aborting...[/bold red]")
                    exit()

            if not meta.get('emby', False):
                console.print(f"[bold]Name:[/bold] {meta['name']}")
                for media_line in self._format_confirm_media_lines(meta):
                    console.print(media_line)
                if self._needs_missing_english_sub_warning(meta):
                    console.print("[bold red]Warning: No English audio and no English subtitles found. This may be forbidden on some trackers.[/bold red]")
                console.print(f"[bold]Size:[/bold] {self._format_source_size(meta.get('source_size'))}")
                confirm_input = console.input("[bold green]Is this correct?[/bold green] [yellow]y/N/skip[/yellow]: ").strip().lower()
                confirm = "skip" if confirm_input in {"s", "skip"} else confirm_input == 'y'
            elif not meta.get('emby_debug', False):
                confirm_input = console.input("[bold green]Is this correct?[/bold green] [yellow]y/N/skip[/yellow]: ").strip().lower()
                confirm = "skip" if confirm_input in {"s", "skip"} else confirm_input == 'y'
        if meta.get('emby_debug', False):
            if meta.get('original_imdb', 0) != meta.get('imdb_id', 0):
                imdb = str(meta.get('imdb_id', 0)).zfill(7)
                console.print(f"[bold red]IMDB ID changed from {meta['original_imdb']} to {meta['imdb_id']}[/bold red]")
                console.print(f"[bold cyan]IMDB URL:[/bold cyan] [yellow]https://www.imdb.com/title/tt{imdb}[/yellow]")
            if meta.get('original_tmdb', 0) != meta.get('tmdb_id', 0):
                console.print(f"[bold red]TMDB ID changed from {meta['original_tmdb']} to {meta['tmdb_id']}[/bold red]")
                console.print(f"[bold cyan]TMDB URL:[/bold cyan] [yellow]https://www.themoviedb.org/{meta['category'].lower()}/{meta['tmdb_id']}[/yellow]")
            if meta.get('original_mal', 0) != meta.get('mal_id', 0):
                console.print(f"[bold red]MAL ID changed from {meta['original_mal']} to {meta['mal_id']}[/bold red]")
                console.print(f"[bold cyan]MAL URL:[/bold cyan] [yellow]https://myanimelist.net/anime/{meta['mal_id']}[/yellow]")
            if meta.get('original_tvmaze', 0) != meta.get('tvmaze_id', 0):
                console.print(f"[bold red]TVMaze ID changed from {meta['original_tvmaze']} to {meta['tvmaze_id']}[/bold red]")
                console.print(f"[bold cyan]TVMaze URL:[/bold cyan] [yellow]https://www.tvmaze.com/shows/{meta['tvmaze_id']}[/yellow]")
            if meta.get('original_tvdb', 0) != meta.get('tvdb_id', 0):
                console.print(f"[bold red]TVDB ID changed from {meta['original_tvdb']} to {meta['tvdb_id']}[/bold red]")
                console.print(f"[bold cyan]TVDB URL:[/bold cyan] [yellow]https://www.thetvdb.com/?id={meta['tvdb_id']}&tab=series[/yellow]")
            if meta.get('original_category', None) != meta.get('category', None):
                console.print(f"[bold red]Category changed from {meta['original_category']} to {meta['category']}[/bold red]")
            console.print(f"[bold cyan]Regex Title:[/bold cyan] [yellow]{meta.get('regex_title', 'N/A')}[/yellow], [bold cyan]Secondary Title:[/bold cyan] [yellow]{meta.get('regex_secondary_title', 'N/A')}[/yellow], [bold cyan]Year:[/bold cyan] [yellow]{meta.get('regex_year', 'N/A')}, [bold cyan]AKA:[/bold cyan] [yellow]{meta.get('aka', '')}[/yellow]")
            console.print()
            if meta.get('original_imdb', 0) == meta.get('imdb_id', 0) and meta.get('original_tmdb', 0) == meta.get('tmdb_id', 0) and meta.get('original_mal', 0) == meta.get('mal_id', 0) and meta.get('original_tvmaze', 0) == meta.get('tvmaze_id', 0) and meta.get('original_tvdb', 0) == meta.get('tvdb_id', 0) and meta.get('original_category', None) == meta.get('category', None):
                console.print("[bold yellow]Database ID's are correct![/bold yellow]")
                return True
            else:
                nfo_dir = os.path.join(f"{meta['base_dir']}/data")
                os.makedirs(nfo_dir, exist_ok=True)
                json_file_path = os.path.join(nfo_dir, "db_check.json")

                def imdb_url(imdb_id: Any) -> Optional[str]:
                    return f"https://www.imdb.com/title/tt{str(imdb_id).zfill(7)}" if imdb_id and str(imdb_id).isdigit() else None

                def tmdb_url(tmdb_id: Any, category: Any) -> Optional[str]:
                    return f"https://www.themoviedb.org/{str(category).lower()}/{tmdb_id}" if tmdb_id and category else None

                def tvdb_url(tvdb_id: Any) -> Optional[str]:
                    return f"https://www.thetvdb.com/?id={tvdb_id}&tab=series" if tvdb_id else None

                def tvmaze_url(tvmaze_id: Any) -> Optional[str]:
                    return f"https://www.tvmaze.com/shows/{tvmaze_id}" if tvmaze_id else None

                def mal_url(mal_id: Any) -> Optional[str]:
                    return f"https://myanimelist.net/anime/{mal_id}" if mal_id else None

                db_check_entry = {
                    "path": meta.get('path'),
                    "original": {
                        "imdb_id": meta.get('original_imdb', 'N/A'),
                        "imdb_url": imdb_url(meta.get('original_imdb')),
                        "tmdb_id": meta.get('original_tmdb', 'N/A'),
                        "tmdb_url": tmdb_url(meta.get('original_tmdb'), meta.get('original_category')),
                        "tvdb_id": meta.get('original_tvdb', 'N/A'),
                        "tvdb_url": tvdb_url(meta.get('original_tvdb')),
                        "tvmaze_id": meta.get('original_tvmaze', 'N/A'),
                        "tvmaze_url": tvmaze_url(meta.get('original_tvmaze')),
                        "mal_id": meta.get('original_mal', 'N/A'),
                        "mal_url": mal_url(meta.get('original_mal')),
                        "category": meta.get('original_category', 'N/A')
                    },
                    "changed": {
                        "imdb_id": meta.get('imdb_id', 'N/A'),
                        "imdb_url": imdb_url(meta.get('imdb_id')),
                        "tmdb_id": meta.get('tmdb_id', 'N/A'),
                        "tmdb_url": tmdb_url(meta.get('tmdb_id'), meta.get('category')),
                        "tvdb_id": meta.get('tvdb_id', 'N/A'),
                        "tvdb_url": tvdb_url(meta.get('tvdb_id')),
                        "tvmaze_id": meta.get('tvmaze_id', 'N/A'),
                        "tvmaze_url": tvmaze_url(meta.get('tvmaze_id')),
                        "mal_id": meta.get('mal_id', 'N/A'),
                        "mal_url": mal_url(meta.get('mal_id')),
                        "category": meta.get('category', 'N/A')
                    },
                    "tracker": meta.get('matched_tracker', 'N/A'),
                }

                # Append to JSON file (as a list of entries)
                db_data_list: list[dict[str, Any]] = []
                if os.path.exists(json_file_path):
                    async with aiofiles.open(json_file_path, encoding='utf-8') as f:
                        try:
                            file_contents = await f.read()
                            if file_contents:
                                parsed_data = json.loads(file_contents)
                                if isinstance(parsed_data, list):
                                    db_data_list = cast(list[dict[str, Any]], parsed_data)
                        except Exception:
                            db_data_list = []
                db_data_list.append(db_check_entry)

                async with aiofiles.open(json_file_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(db_data_list, indent=2, ensure_ascii=False))
                return True

        return confirm

    async def get_missing(self, meta: Meta) -> None:
        info_notes = {
            'edition': 'Special Edition/Release',
            'description': "Please include Remux/Encode Notes if possible",
            'service': "WEB Service e.g.(AMZN, NF)",
            'region': "Disc Region",
            'imdb': 'IMDb ID (tt1234567)',
            'distributor': "Disc Distributor e.g.(BFI, Criterion)"
        }
        if meta.get('imdb_id', 0) == 0:
            meta['imdb_id'] = 0
            potential_missing = cast(list[str], meta.get('potential_missing', []))
            if 'imdb_id' not in potential_missing:
                potential_missing.append('imdb_id')
                meta['potential_missing'] = potential_missing
        else:
            potential_missing = cast(list[str], meta.get('potential_missing', []))
        missing = [
            f"--{each} | {info_notes.get(each, '')}"
            for each in potential_missing
            if str(meta.get(each, '')).strip() in ["", "None", "0"]
        ]
        if missing:
            console.print("[bold yellow]Potentially missing information:[/bold yellow]")
            for each in missing:
                cli_ui.info(each)
