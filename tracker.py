#!/usr/bin/env python3
"""Clash of Clans Personal Progress Tracker.

Fetches one player's data through the official Clash of Clans API using the
RoyaleAPI proxy, compares it with state.json, reports changes by Gmail, and
saves the latest successful snapshot.

Required environment variables:
  PLAYER_TAG
  COC_API_TOKEN       Official Clash of Clans developer API token
  GMAIL_ADDRESS       Gmail sender/recipient
  GMAIL_APP_PASSWORD  Google App Password (not the normal account password)

Optional environment variables:
  STATE_FILE, CONFIG_FILE, REQUEST_TIMEOUT, API_RETRIES, API_RETRY_DELAY
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "config.json"))
API_BASE_URL = os.getenv(
    "COC_API_BASE_URL", "https://cocproxy.royaleapi.dev/v1/players/"
).rstrip("/") + "/"
REQUEST_TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT", "30")))
API_RETRIES = max(1, int(os.getenv("API_RETRIES", "3")))
API_RETRY_DELAY = max(1, int(os.getenv("API_RETRY_DELAY", "5")))
PLAYER_TAG = os.getenv("PLAYER_TAG", "").strip()
COC_API_TOKEN = os.getenv("COC_API_TOKEN", "").strip()
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")

DEFAULT_CONFIG: dict[str, Any] = {
    "email_on_upgrades": True,
    "email_on_maxed_items": True,
    "email_on_player_changes": True,
    "email_on_achievement_changes": True,
    "include_progress_summary": True,
    "include_maxed_summary": True,
    "include_player_summary": True,
    "include_achievement_summary": True,
}


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object from disk."""
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_config() -> dict[str, Any]:
    """Load optional config.json, falling back safely to defaults."""
    config = DEFAULT_CONFIG.copy()
    if not CONFIG_FILE.exists():
        return config
    try:
        loaded = load_json(CONFIG_FILE)
        config.update(loaded)
        print(f"Loaded configuration from {CONFIG_FILE}.")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"WARNING: Could not load {CONFIG_FILE}: {exc}")
        print("Using default configuration.")
    return config


CONFIG = load_config()


def validate_environment() -> None:
    """Fail early with a readable message when a required secret is absent."""
    required = {
        "PLAYER_TAG": PLAYER_TAG,
        "COC_API_TOKEN": COC_API_TOKEN,
        "GMAIL_ADDRESS": GMAIL_ADDRESS,
        "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        print(
            "ERROR: Missing required GitHub Actions secret/environment variable(s): "
            + ", ".join(missing)
        )
        print(
            "In GitHub, open Settings > Secrets and variables > Actions. "
            "Create any missing repository secrets and ensure tracker.yml passes "
            "them to the Run step."
        )
        raise SystemExit(1)


def normalize_player_tag(tag: str) -> str:
    """Normalize a player tag to #UPPERCASE form."""
    normalized = tag.strip().upper()
    if not normalized:
        raise ValueError("PLAYER_TAG is empty")
    if not normalized.startswith("#"):
        normalized = "#" + normalized
    return normalized


def fetch_player(player_tag: str) -> dict[str, Any]:
    """Fetch a player profile, retrying transient network/server errors."""
    normalized = normalize_player_tag(player_tag)
    url = API_BASE_URL + quote(normalized, safe="")
    headers = {
        "Accept": "application/json",
        "User-Agent": "coc-upgrade-tracker/2.0",
        "Authorization": f"Bearer {COC_API_TOKEN}",
    }
    transient_statuses = {408, 425, 429, 500, 502, 503, 504, 522, 524}
    last_error: Exception | None = None

    for attempt in range(1, API_RETRIES + 1):
        try:
            print(f"Fetching player data (attempt {attempt}/{API_RETRIES})...")
            response = requests.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT
            )

            if response.status_code in (401, 403):
                raise RuntimeError(
                    f"API authentication/access failed (HTTP {response.status_code}). "
                    "Verify COC_API_TOKEN and that the official API key allowlists "
                    "the RoyaleAPI proxy IP 45.79.218.79."
                )
            if response.status_code == 404:
                raise RuntimeError(
                    f"Player {normalized} was not found (HTTP 404). "
                    "Check the PLAYER_TAG secret."
                )
            if response.status_code in transient_statuses:
                raise RuntimeError(
                    f"Temporary API/proxy response: HTTP {response.status_code}"
                )

            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("API returned JSON that is not an object")
            if not data.get("tag"):
                # Some API errors are JSON objects; don't accidentally treat them
                # as player data and overwrite the saved state.
                raise RuntimeError(
                    "API response does not contain a player 'tag'. "
                    f"Response keys: {', '.join(map(str, data.keys()))[:300]}"
                )

            print(
                f"Successfully fetched {data.get('name', normalized)} "
                f"({data.get('tag', normalized)})."
            )
            return data

        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            print(f"Attempt {attempt}/{API_RETRIES} failed: {exc}")
            # Authentication and player-not-found errors are not helped by retries.
            if isinstance(exc, RuntimeError) and (
                "HTTP 401" in str(exc)
                or "HTTP 403" in str(exc)
                or "HTTP 404" in str(exc)
            ):
                break
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_DELAY)

    raise RuntimeError(
        f"Unable to fetch player data after {API_RETRIES} attempt(s): {last_error}"
    )


# ---------------------------------------------------------------------------
# State helpers. The output format intentionally matches the existing
# state.json structure so that existing snapshots remain usable.
# ---------------------------------------------------------------------------
def load_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None
    try:
        return load_json(STATE_FILE)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(
            f"WARNING: Could not load {STATE_FILE}: {exc}. "
            "The current data will be used as a new baseline."
        )
        return None


def save_state(state: dict[str, Any]) -> None:
    """Write atomically so an interrupted run cannot leave partial JSON."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.write("\n")
    temporary.replace(STATE_FILE)


def item_key(item: dict[str, Any]) -> str:
    return f"{item.get('village', 'home')}|{item.get('name', 'Unknown')}"


def make_item_map(items: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if isinstance(item, dict) and "name" in item and "level" in item:
            result[item_key(item)] = item
    return result


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def build_state(player: dict[str, Any]) -> dict[str, Any]:
    """Convert the raw API player response to the established state format."""
    raw_troops = _list_of_dicts(player.get("troops"))
    raw_heroes = _list_of_dicts(player.get("heroes"))
    raw_spells = _list_of_dicts(player.get("spells"))

    siege_names = {
        "Wall Wrecker", "Battle Blimp", "Stone Slammer", "Siege Barracks",
        "Log Launcher", "Flame Flinger", "Battle Drill", "Troop Launcher",
        "Furnace",
    }
    troops: list[dict[str, Any]] = []
    siege_machines: list[dict[str, Any]] = []
    for item in raw_troops:
        if item.get("name") in siege_names:
            siege_machines.append(item)
        else:
            troops.append(item)

    # The API/provider schema may supply equipment as a top-level collection or
    # nested inside each hero. Prefer the top-level array to avoid duplicates.
    raw_equipment = player.get("heroEquipment")
    if isinstance(raw_equipment, list):
        hero_equipment = _list_of_dicts(raw_equipment)
    else:
        hero_equipment = []
        for hero in raw_heroes:
            equipment = _list_of_dicts(hero.get("equipment"))
            for item in equipment:
                normalized_item = dict(item)
                normalized_item.setdefault("village", hero.get("village", "home"))
                hero_equipment.append(normalized_item)

    achievements: list[dict[str, Any]] = []
    for achievement in _list_of_dicts(player.get("achievements")):
        achievements.append({
            "name": achievement.get("name"),
            "stars": achievement.get("stars"),
            "value": achievement.get("value"),
            "target": achievement.get("target"),
            "completionInfo": achievement.get("completionInfo"),
            "village": achievement.get("village"),
        })

    clan = player.get("clan")
    clan_summary = None
    if isinstance(clan, dict):
        clan_summary = {
            "tag": clan.get("tag"),
            "name": clan.get("name"),
            "clanLevel": clan.get("clanLevel"),
        }

    league = player.get("leagueTier")
    if not isinstance(league, dict):
        league = player.get("league")
    league_summary = None
    if isinstance(league, dict):
        league_summary = {"id": league.get("id"), "name": league.get("name")}

    builder_league = player.get("builderBaseLeague")
    builder_league_summary = None
    if isinstance(builder_league, dict):
        builder_league_summary = {
            "id": builder_league.get("id"),
            "name": builder_league.get("name"),
        }

    return {
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "player": {
            "tag": player.get("tag"),
            "name": player.get("name"),
            "townHallLevel": player.get("townHallLevel"),
            "expLevel": player.get("expLevel"),
            "trophies": player.get("trophies"),
            "bestTrophies": player.get("bestTrophies"),
            "warStars": player.get("warStars"),
            "builderHallLevel": player.get("builderHallLevel"),
            "builderBaseTrophies": player.get("builderBaseTrophies"),
            "bestBuilderBaseTrophies": player.get("bestBuilderBaseTrophies"),
            "role": player.get("role"),
            "warPreference": player.get("warPreference"),
            "donations": player.get("donations"),
            "donationsReceived": player.get("donationsReceived"),
            "clanCapitalContributions": player.get("clanCapitalContributions"),
            "clan": clan_summary,
            "leagueTier": league_summary,
            "builderBaseLeague": builder_league_summary,
        },
        "categories": {
            "troops": troops,
            "heroes": raw_heroes,
            "heroEquipment": hero_equipment,
            "spells": raw_spells,
            "siegeMachines": siege_machines,
        },
        "achievements": achievements,
    }


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------
CATEGORY_LABELS = {
    "troops": "Troop",
    "heroes": "Hero",
    "heroEquipment": "Hero Equipment",
    "spells": "Spell",
    "siegeMachines": "Siege Machine",
}


def detect_level_changes(
    old_state: dict[str, Any], new_state: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    upgrades: list[dict[str, Any]] = []
    newly_maxed: list[dict[str, Any]] = []
    old_categories = old_state.get("categories", {})
    new_categories = new_state.get("categories", {})

    for category, label in CATEGORY_LABELS.items():
        old_items = make_item_map(old_categories.get(category, []))
        new_items = make_item_map(new_categories.get(category, []))
        for key, new_item in new_items.items():
            old_item = old_items.get(key)
            if old_item is None:
                # A newly introduced API item is not assumed to be an upgrade.
                continue
            old_level, new_level = old_item.get("level"), new_item.get("level")
            if not isinstance(old_level, int) or not isinstance(new_level, int):
                continue
            if new_level <= old_level:
                continue

            entry = {
                "category": label,
                "name": new_item.get("name"),
                "village": new_item.get("village", "home"),
                "oldLevel": old_level,
                "newLevel": new_level,
                "maxLevel": new_item.get("maxLevel"),
            }
            upgrades.append(entry)
            old_max, new_max = old_item.get("maxLevel"), new_item.get("maxLevel")
            if (
                isinstance(new_max, int)
                and new_level >= new_max
                and (not isinstance(old_max, int) or old_level < old_max)
            ):
                newly_maxed.append(entry)
    return upgrades, newly_maxed


def _display_value(field: str, value: Any) -> Any:
    if field in {"leagueTier", "builderBaseLeague", "clan"} and isinstance(value, dict):
        return value.get("name")
    return value


def detect_player_changes(
    old_state: dict[str, Any], new_state: dict[str, Any]
) -> list[str]:
    changes: list[str] = []
    old, new = old_state.get("player", {}), new_state.get("player", {})
    fields = [
        ("name", "Name"), ("townHallLevel", "Town Hall"),
        ("builderHallLevel", "Builder Hall"), ("leagueTier", "League"),
        ("builderBaseLeague", "Builder Base League"), ("role", "Clan role"),
        ("warPreference", "War preference"), ("clan", "Clan"),
    ]
    for field, label in fields:
        old_value, new_value = old.get(field), new.get(field)
        old_display, new_display = _display_value(field, old_value), _display_value(field, new_value)
        if old_display != new_display:
            changes.append(f"{label}: {old_display} → {new_display}")

    numeric_fields = [
        ("trophies", "Trophies"), ("bestTrophies", "Best trophies"),
        ("builderBaseTrophies", "Builder Base trophies"),
        ("bestBuilderBaseTrophies", "Best Builder Base trophies"),
        ("warStars", "War stars"), ("donations", "Donations"),
        ("donationsReceived", "Donations received"),
        ("clanCapitalContributions", "Clan Capital contributions"),
    ]
    for field, label in numeric_fields:
        old_value, new_value = old.get(field), new.get(field)
        if old_value != new_value and old_value is not None and new_value is not None:
            if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
                changes.append(f"{label}: {old_value:,} → {new_value:,}")
            else:
                changes.append(f"{label}: {old_value} → {new_value}")
    return changes


def detect_achievement_changes(
    old_state: dict[str, Any], new_state: dict[str, Any]
) -> list[str]:
    old_map = {
        (a.get("village"), a.get("name")): a
        for a in old_state.get("achievements", []) if isinstance(a, dict)
    }
    changes: list[str] = []
    for achievement in new_state.get("achievements", []):
        if not isinstance(achievement, dict):
            continue
        old = old_map.get((achievement.get("village"), achievement.get("name")))
        if old is None:
            continue
        old_value, new_value = old.get("value"), achievement.get("value")
        if isinstance(old_value, int) and isinstance(new_value, int) and new_value > old_value:
            target = achievement.get("target")
            target_text = f" / {target:,}" if isinstance(target, int) else ""
            changes.append(
                f"{achievement.get('name')}: {old_value:,} → {new_value:,}{target_text}"
            )
    return changes


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------
def progress_lines(state: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    categories = state.get("categories", {})
    for category, label in CATEGORY_LABELS.items():
        items = _list_of_dicts(categories.get(category, []))
        for group_name, village in (("Home", "home"), ("Builder Base", "builderBase")):
            group = [item for item in items if item.get("village", "home") == village]
            if not group:
                continue
            maxed = sum(
                1 for item in group
                if isinstance(item.get("level"), int)
                and isinstance(item.get("maxLevel"), int)
                and item["level"] >= item["maxLevel"]
            )
            lines.append(f"{label} - {group_name}: {maxed}/{len(group)} maxed")
    return lines


def maxed_lines(state: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    categories = state.get("categories", {})
    for category, label in CATEGORY_LABELS.items():
        for item in _list_of_dicts(categories.get(category, [])):
            level, maximum = item.get("level"), item.get("maxLevel")
            if isinstance(level, int) and isinstance(maximum, int) and level >= maximum:
                lines.append(f"{label}: {item.get('name')} (Level {level}/{maximum})")
    return lines


def _format_number(value: Any) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else str(value if value is not None else "Unknown")


def player_summary_lines(state: dict[str, Any]) -> list[str]:
    player = state.get("player", {})
    clan = player.get("clan")
    league = player.get("leagueTier")
    builder_league = player.get("builderBaseLeague")
    clan_name = clan.get("name") if isinstance(clan, dict) else "No clan"
    league_name = league.get("name") if isinstance(league, dict) else "Unknown"
    builder_league_name = builder_league.get("name") if isinstance(builder_league, dict) else "Unknown"
    rows = [
        ("Player", player.get("name", "Unknown")),
        ("Tag", player.get("tag", "Unknown")),
        ("Town Hall", player.get("townHallLevel", "Unknown")),
        ("Builder Hall", player.get("builderHallLevel", "Unknown")),
        ("Trophies", player.get("trophies", 0)),
        ("Best trophies", player.get("bestTrophies", 0)),
        ("Builder Base trophies", player.get("builderBaseTrophies", 0)),
        ("Best Builder Base trophies", player.get("bestBuilderBaseTrophies", 0)),
        ("League", league_name),
        ("Builder Base League", builder_league_name),
        ("Clan", clan_name),
        ("War stars", player.get("warStars", 0)),
        ("Donations", player.get("donations", 0)),
        ("Donations received", player.get("donationsReceived", 0)),
        ("Clan Capital contributions", player.get("clanCapitalContributions", 0)),
    ]
    return [f"{label}: {_format_number(value)}" for label, value in rows]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def build_email(
    state: dict[str, Any],
    upgrades: list[dict[str, Any]],
    newly_maxed: list[dict[str, Any]],
    player_changes: list[str],
    achievement_changes: list[str],
) -> tuple[str, str]:
    player_name = state.get("player", {}).get("name", "Chief")
    subject_parts: list[str] = []
    if upgrades:
        subject_parts.append(f"{len(upgrades)} upgrade(s)")
    if newly_maxed:
        subject_parts.append(f"{len(newly_maxed)} maxed")
    if player_changes:
        subject_parts.append(f"{len(player_changes)} player change(s)")
    if achievement_changes:
        subject_parts.append(f"{len(achievement_changes)} achievement change(s)")
    if not subject_parts:
        subject_parts.append("Progress update")
    subject = f"CoC Tracker: {', '.join(subject_parts)} - {player_name}"

    lines = ["Greetings, Chief!", "", f"Clash of Clans progress update for {player_name}.", ""]
    if upgrades and CONFIG.get("email_on_upgrades", True):
        lines.extend(["UPGRADES COMPLETED", "=" * 24])
        for item in upgrades:
            village = "Builder Base" if item.get("village") == "builderBase" else "Home Village"
            maximum = item.get("maxLevel")
            max_text = f"/{maximum}" if isinstance(maximum, int) else ""
            lines.append(
                f"- [{village}] {item.get('category')} {item.get('name')}: "
                f"{item.get('oldLevel')} → {item.get('newLevel')}{max_text}"
            )
        lines.append("")
    if newly_maxed and CONFIG.get("email_on_maxed_items", True):
        lines.extend(["NEWLY MAXED", "=" * 24])
        for item in newly_maxed:
            lines.append(
                f"- {item.get('category')}: {item.get('name')} "
                f"(Level {item.get('newLevel')}/{item.get('maxLevel')})"
            )
        lines.append("")
    if player_changes and CONFIG.get("email_on_player_changes", True):
        lines.extend(["PLAYER / ACCOUNT CHANGES", "=" * 24])
        lines.extend(f"- {change}" for change in player_changes)
        lines.append("")
    if achievement_changes and CONFIG.get("email_on_achievement_changes", True):
        lines.extend(["ACHIEVEMENT PROGRESS", "=" * 24])
        lines.extend(f"- {change}" for change in achievement_changes)
        lines.append("")
    if CONFIG.get("include_player_summary", True):
        lines.extend(["PLAYER SUMMARY", "=" * 24])
        lines.extend(f"- {line}" for line in player_summary_lines(state))
        lines.append("")
    if CONFIG.get("include_progress_summary", True):
        lines.extend(["UPGRADE PROGRESS", "=" * 24])
        lines.extend(f"- {line}" for line in progress_lines(state))
        lines.append("")
    if CONFIG.get("include_maxed_summary", True):
        lines.extend(["MAXED ITEMS", "=" * 24])
        lines.extend(f"- {line}" for line in maxed_lines(state))
        lines.append("")
    if CONFIG.get("include_achievement_summary", True):
        achievements = _list_of_dicts(state.get("achievements", []))
        completed = sum(1 for a in achievements if a.get("stars") == 3)
        lines.extend([
            "ACHIEVEMENT SUMMARY", "=" * 24,
            f"- 3-star achievements: {completed}/{len(achievements)}", "",
        ])
    lines.extend([
        "This message was generated automatically by your Clash of Clans Personal Tracker.",
        "", f"Checked: {datetime.now(timezone.utc).isoformat()}",
    ])
    return subject, "\n".join(lines)


def should_send_email(
    upgrades: list[dict[str, Any]],
    newly_maxed: list[dict[str, Any]],
    player_changes: list[str],
    achievement_changes: list[str],
) -> bool:
    return bool(
        (upgrades and CONFIG.get("email_on_upgrades", True))
        or (newly_maxed and CONFIG.get("email_on_maxed_items", True))
        or (player_changes and CONFIG.get("email_on_player_changes", True))
        or (achievement_changes and CONFIG.get("email_on_achievement_changes", True))
    )


def send_email(subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = GMAIL_ADDRESS
    message["To"] = GMAIL_ADDRESS
    message.set_content(body)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(message)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 62)
    print("CLASH OF CLANS PERSONAL PROGRESS TRACKER")
    print("=" * 62)
    validate_environment()

    try:
        player_tag = normalize_player_tag(PLAYER_TAG)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Player: {player_tag}")
    print(f"Data source: {API_BASE_URL}")
    print()
    try:
        player = fetch_player(player_tag)
    except Exception as exc:
        print(f"ERROR fetching player: {exc}")
        return 1

    new_state = build_state(player)
    old_state = load_state()
    if old_state is None:
        save_state(new_state)
        print("No previous state found. Saved current data as the baseline.")
        print("No upgrade email is sent on the baseline run.")
        return 0

    upgrades, newly_maxed = detect_level_changes(old_state, new_state)
    player_changes = detect_player_changes(old_state, new_state)
    achievement_changes = detect_achievement_changes(old_state, new_state)

    print(f"Upgrade changes detected: {len(upgrades)}")
    print(f"Newly maxed items: {len(newly_maxed)}")
    print(f"Player changes: {len(player_changes)}")
    print(f"Achievement changes: {len(achievement_changes)}")

    # Match the established workflow behavior: only successful API data is saved.
    # Save before mailing so an SMTP outage does not repeatedly email the same diff.
    save_state(new_state)

    if should_send_email(upgrades, newly_maxed, player_changes, achievement_changes):
        subject, body = build_email(
            new_state, upgrades, newly_maxed, player_changes, achievement_changes
        )
        try:
            send_email(subject, body)
            print("Gmail notification sent successfully.")
        except smtplib.SMTPAuthenticationError:
            print(
                "ERROR: Gmail authentication failed. Check GMAIL_ADDRESS and "
                "use a Google App Password in GMAIL_APP_PASSWORD."
            )
            return 1
        except (smtplib.SMTPException, OSError) as exc:
            print(f"ERROR: Gmail could not send the notification: {exc}")
            return 1
    else:
        print("No configured notification-worthy changes.")

    print(f"State saved successfully to {STATE_FILE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
