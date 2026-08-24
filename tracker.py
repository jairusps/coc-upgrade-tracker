#!/usr/bin/env python3
"""
Clash of Clans Personal Progress Tracker

Data source:
    ClashKing public proxy:
    https://proxy.clashk.ing/v1/players/{encoded_player_tag}

The tracker:
- fetches one Clash of Clans player
- keeps a previous snapshot in state.json
- detects level increases
- tracks heroes, hero equipment, troops, spells, and siege machines
- separates Home Village and Builder Base items where the API supplies village
- detects newly maxed items
- reports player/TH/league/clan changes
- reports achievement progress changes
- sends Gmail alerts
- saves state after a successful API fetch
- works in GitHub Actions without a Clash of Clans developer API token

No Clash of Clans developer credentials are required.
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

API_BASE_URL = "https://proxy.clashk.ing/v1/players/"
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
API_RETRIES = int(os.getenv("API_RETRIES", "3"))
API_RETRY_DELAY = int(os.getenv("API_RETRY_DELAY", "5"))

PLAYER_TAG = os.getenv("PLAYER_TAG", "").strip()
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()


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


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object from disk."""
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")

    return data


def load_config() -> dict[str, Any]:
    """
    Load optional config.json.

    A missing config.json is normal. Environment variables and defaults are
    enough for the application to run.
    """
    config = DEFAULT_CONFIG.copy()

    if not CONFIG_FILE.exists():
        return config

    try:
        loaded = load_json(CONFIG_FILE)
        config.update(loaded)
        print(f"Loaded configuration from {CONFIG_FILE}.")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"WARNING: {CONFIG_FILE} could not be loaded: {exc}")
        print("Using default configuration.")

    return config


CONFIG = load_config()


def validate_environment() -> None:
    missing: list[str] = []

    if not PLAYER_TAG:
        missing.append("PLAYER_TAG")
    if not GMAIL_ADDRESS:
        missing.append("GMAIL_ADDRESS")
    if not GMAIL_APP_PASSWORD:
        missing.append("GMAIL_APP_PASSWORD")

    if missing:
        print(
            "ERROR: Missing required environment variable(s): "
            + ", ".join(missing)
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Tag / API helpers
# ---------------------------------------------------------------------------

def normalize_player_tag(tag: str) -> str:
    tag = tag.strip().upper()

    if not tag:
        raise ValueError("PLAYER_TAG is empty.")

    if not tag.startswith("#"):
        tag = "#" + tag

    return tag


def fetch_player(player_tag: str) -> dict[str, Any]:
    """
    Fetch player JSON from the currently used ClashKing proxy endpoint.

    The user's verified endpoint format is:
        /v1/players/%23TAG
    """
    normalized = normalize_player_tag(player_tag)
    encoded_tag = quote(normalized, safe="")
    url = API_BASE_URL + encoded_tag

    headers = {
        "Accept": "application/json",
        "User-Agent": "coc-upgrade-tracker/1.0",
    }

    last_error: Exception | None = None

    for attempt in range(1, API_RETRIES + 1):
        try:
            print(f"Fetching player data (attempt {attempt}/{API_RETRIES})...")
            response = requests.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 404:
                raise RuntimeError(
                    f"Player {normalized} was not found by ClashKing "
                    f"(HTTP 404). Verify PLAYER_TAG and the proxy endpoint."
                )

            if response.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(
                    f"ClashKing temporary HTTP error: {response.status_code}"
                )

            response.raise_for_status()

            data = response.json()

            if not isinstance(data, dict):
                raise RuntimeError("ClashKing returned a non-object JSON response.")

            # The supplied successful response contains these fields.
            if "tag" not in data:
                raise RuntimeError(
                    "ClashKing returned JSON without a player 'tag' field."
                )

            print(
                f"Successfully fetched {data.get('name', normalized)} "
                f"({data.get('tag', normalized)})."
            )
            return data

        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            print(f"Attempt {attempt}/{API_RETRIES} failed: {exc}")

            if attempt < API_RETRIES:
                time.sleep(API_RETRY_DELAY)

    raise RuntimeError(
        f"Unable to obtain player data after {API_RETRIES} attempts: "
        f"{last_error}"
    )


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None

    try:
        return load_json(STATE_FILE)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(
            f"WARNING: Could not load {STATE_FILE}: {exc}. "
            "The current data will become a new baseline."
        )
        return None


def save_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.write("\n")

    temporary.replace(STATE_FILE)


def item_key(item: dict[str, Any]) -> str:
    village = item.get("village", "home")
    name = item.get("name", "Unknown")
    return f"{village}|{name}"


def make_item_map(items: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    if not isinstance(items, list):
        return result

    for item in items:
        if not isinstance(item, dict):
            continue
        if "name" not in item or "level" not in item:
            continue

        result[item_key(item)] = item

    return result


def build_state(player: dict[str, Any]) -> dict[str, Any]:
    """
    Store only the data needed for comparison/reporting.

    We intentionally use fields that are present in the player's supplied
    successful JSON response.
    """
    categories = {
        "troops": player.get("troops", []),
        "heroes": player.get("heroes", []),
        "heroEquipment": player.get("heroEquipment", []),
        "spells": player.get("spells", []),
    }

    # In the supplied response, siege machines are represented in the
    # "troops" array with names such as Wall Wrecker, Battle Blimp, etc.
    # They are identified separately below using this explicit known list.
    siege_names = {
        "Wall Wrecker",
        "Battle Blimp",
        "Stone Slammer",
        "Siege Barracks",
        "Log Launcher",
        "Flame Flinger",
        "Battle Drill",
        "Troop Launcher",
        "Furnace",
    }

    troops = []
    siege_machines = []

    for item in player.get("troops", []):
        if not isinstance(item, dict):
            continue

        if item.get("name") in siege_names:
            siege_machines.append(item)
        else:
            troops.append(item)

    achievements = []
    for achievement in player.get("achievements", []):
        if not isinstance(achievement, dict):
            continue

        achievements.append(
            {
                "name": achievement.get("name"),
                "stars": achievement.get("stars"),
                "value": achievement.get("value"),
                "target": achievement.get("target"),
                "completionInfo": achievement.get("completionInfo"),
                "village": achievement.get("village"),
            }
        )

    clan = player.get("clan")
    clan_summary = None

    if isinstance(clan, dict):
        clan_summary = {
            "tag": clan.get("tag"),
            "name": clan.get("name"),
            "clanLevel": clan.get("clanLevel"),
        }

    league = player.get("leagueTier")
    league_summary = None

    if isinstance(league, dict):
        league_summary = {
            "id": league.get("id"),
            "name": league.get("name"),
        }

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
            "heroes": categories["heroes"],
            "heroEquipment": categories["heroEquipment"],
            "spells": categories["spells"],
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
    old_state: dict[str, Any],
    new_state: dict[str, Any],
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
                # New item appearing in the API is not treated as an upgrade.
                continue

            old_level = old_item.get("level")
            new_level = new_item.get("level")

            if not isinstance(old_level, int) or not isinstance(new_level, int):
                continue

            if new_level > old_level:
                entry = {
                    "category": label,
                    "name": new_item.get("name"),
                    "village": new_item.get("village", "home"),
                    "oldLevel": old_level,
                    "newLevel": new_level,
                    "maxLevel": new_item.get("maxLevel"),
                }
                upgrades.append(entry)

                old_max = old_item.get("maxLevel")
                new_max = new_item.get("maxLevel")

                if (
                    isinstance(new_max, int)
                    and new_level >= new_max
                    and (not isinstance(old_max, int) or old_level < old_max)
                ):
                    newly_maxed.append(entry)

    return upgrades, newly_maxed


def detect_player_changes(
    old_state: dict[str, Any],
    new_state: dict[str, Any],
) -> list[str]:
    changes: list[str] = []

    old = old_state.get("player", {})
    new = new_state.get("player", {})

    fields = [
        ("name", "Name"),
        ("townHallLevel", "Town Hall"),
        ("builderHallLevel", "Builder Hall"),
        ("leagueTier", "League"),
        ("builderBaseLeague", "Builder Base League"),
        ("role", "Clan role"),
        ("warPreference", "War preference"),
        ("clan", "Clan"),
    ]

    for field, label in fields:
        old_value = old.get(field)
        new_value = new.get(field)

        if old_value != new_value:
            if field == "leagueTier":
                old_value = (
                    old_value.get("name")
                    if isinstance(old_value, dict)
                    else old_value
                )
                new_value = (
                    new_value.get("name")
                    if isinstance(new_value, dict)
                    else new_value
                )

            elif field == "builderBaseLeague":
                old_value = (
                    old_value.get("name")
                    if isinstance(old_value, dict)
                    else old_value
                )
                new_value = (
                    new_value.get("name")
                    if isinstance(new_value, dict)
                    else new_value
                )

            elif field == "clan":
                old_value = (
                    old_value.get("name")
                    if isinstance(old_value, dict)
                    else old_value
                )
                new_value = (
                    new_value.get("name")
                    if isinstance(new_value, dict)
                    else new_value
                )

            changes.append(f"{label}: {old_value} → {new_value}")

    numeric_fields = [
        ("trophies", "Trophies"),
        ("bestTrophies", "Best trophies"),
        ("builderBaseTrophies", "Builder Base trophies"),
        ("bestBuilderBaseTrophies", "Best Builder Base trophies"),
        ("warStars", "War stars"),
        ("donations", "Donations"),
        ("donationsReceived", "Donations received"),
        ("clanCapitalContributions", "Clan Capital contributions"),
    ]

    for field, label in numeric_fields:
        old_value = old.get(field)
        new_value = new.get(field)

        if old_value != new_value and old_value is not None and new_value is not None:
            changes.append(f"{label}: {old_value:,} → {new_value:,}")

    return changes


def detect_achievement_changes(
    old_state: dict[str, Any],
    new_state: dict[str, Any],
) -> list[str]:
    old_map = {
        (a.get("village"), a.get("name")): a
        for a in old_state.get("achievements", [])
        if isinstance(a, dict)
    }

    changes: list[str] = []

    for achievement in new_state.get("achievements", []):
        if not isinstance(achievement, dict):
            continue

        key = (achievement.get("village"), achievement.get("name"))
        old = old_map.get(key)

        if old is None:
            continue

        old_value = old.get("value")
        new_value = achievement.get("value")

        if (
            isinstance(old_value, int)
            and isinstance(new_value, int)
            and new_value > old_value
        ):
            target = achievement.get("target")
            changes.append(
                f"{achievement.get('name')}: "
                f"{old_value:,} → {new_value:,}"
                + (f" / {target:,}" if isinstance(target, int) else "")
            )

    return changes


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

def progress_lines(state: dict[str, Any]) -> list[str]:
    lines: list[str] = []

    for category, label in CATEGORY_LABELS.items():
        items = state.get("categories", {}).get(category, [])

        home = [
            item for item in items
            if item.get("village") == "home"
        ]
        builder = [
            item for item in items
            if item.get("village") == "builderBase"
        ]

        for group_name, group in (("Home", home), ("Builder Base", builder)):
            if not group:
                continue

            total = len(group)
            maxed = sum(
                1
                for item in group
                if isinstance(item.get("maxLevel"), int)
                and isinstance(item.get("level"), int)
                and item["level"] >= item["maxLevel"]
            )

            lines.append(
                f"{label} - {group_name}: {maxed}/{total} maxed"
            )

    return lines


def maxed_lines(state: dict[str, Any]) -> list[str]:
    lines: list[str] = []

    for category, label in CATEGORY_LABELS.items():
        for item in state.get("categories", {}).get(category, []):
            level = item.get("level")
            maximum = item.get("maxLevel")

            if (
                isinstance(level, int)
                and isinstance(maximum, int)
                and level >= maximum
            ):
                lines.append(
                    f"{label}: {item.get('name')} "
                    f"(Level {level}/{maximum})"
                )

    return lines


def player_summary_lines(state: dict[str, Any]) -> list[str]:
    player = state.get("player", {})

    clan = player.get("clan")
    clan_name = clan.get("name") if isinstance(clan, dict) else "No clan"

    league = player.get("leagueTier")
    league_name = league.get("name") if isinstance(league, dict) else "Unknown"

    builder_league = player.get("builderBaseLeague")
    builder_league_name = (
        builder_league.get("name")
        if isinstance(builder_league, dict)
        else "Unknown"
    )

    return [
        f"Player: {player.get('name', 'Unknown')}",
        f"Tag: {player.get('tag', 'Unknown')}",
        f"Town Hall: {player.get('townHallLevel', 'Unknown')}",
        f"Builder Hall: {player.get('builderHallLevel', 'Unknown')}",
        f"Trophies: {player.get('trophies', 0):,}",
        f"Best trophies: {player.get('bestTrophies', 0):,}",
        f"Builder Base trophies: {player.get('builderBaseTrophies', 0):,}",
        f"Best Builder Base trophies: {player.get('bestBuilderBaseTrophies', 0):,}",
        f"League: {league_name}",
        f"Builder Base League: {builder_league_name}",
        f"Clan: {clan_name}",
        f"War stars: {player.get('warStars', 0):,}",
        f"Donations: {player.get('donations', 0):,}",
        f"Donations received: {player.get('donationsReceived', 0):,}",
        f"Clan Capital contributions: {player.get('clanCapitalContributions', 0):,}",
    ]


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
    player = state.get("player", {})
    player_name = player.get("name", "Chief")

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

    lines = [
        "Greetings, Chief!",
        "",
        f"Clash of Clans progress update for {player_name}.",
        "",
    ]

    if upgrades:
        lines.append("UPGRADES COMPLETED")
        lines.append("=" * 24)

        for upgrade in upgrades:
            village = upgrade.get("village", "home")
            village_label = (
                "Builder Base"
                if village == "builderBase"
                else "Home Village"
            )

            maximum = upgrade.get("maxLevel")
            max_text = (
                f"/{maximum}"
                if isinstance(maximum, int)
                else ""
            )

            lines.append(
                f"- [{village_label}] {upgrade['category']} "
                f"{upgrade['name']}: "
                f"{upgrade['oldLevel']} → {upgrade['newLevel']}{max_text}"
            )

        lines.append("")

    if newly_maxed and CONFIG["email_on_maxed_items"]:
        lines.append("NEWLY MAXED")
        lines.append("=" * 24)

        for item in newly_maxed:
            lines.append(
                f"- {item['category']}: {item['name']} "
                f"(Level {item['newLevel']}/{item['maxLevel']})"
            )

        lines.append("")

    if player_changes and CONFIG["email_on_player_changes"]:
        lines.append("PLAYER / ACCOUNT CHANGES")
        lines.append("=" * 24)

        for change in player_changes:
            lines.append(f"- {change}")

        lines.append("")

    if achievement_changes and CONFIG["email_on_achievement_changes"]:
        lines.append("ACHIEVEMENT PROGRESS")
        lines.append("=" * 24)

        for change in achievement_changes:
            lines.append(f"- {change}")

        lines.append("")

    if CONFIG["include_player_summary"]:
        lines.append("PLAYER SUMMARY")
        lines.append("=" * 24)
        lines.extend(f"- {line}" for line in player_summary_lines(state))
        lines.append("")

    if CONFIG["include_progress_summary"]:
        lines.append("UPGRADE PROGRESS")
        lines.append("=" * 24)
        lines.extend(f"- {line}" for line in progress_lines(state))
        lines.append("")

    if CONFIG["include_maxed_summary"]:
        lines.append("MAXED ITEMS")
        lines.append("=" * 24)
        lines.extend(f"- {line}" for line in maxed_lines(state))
        lines.append("")

    if CONFIG["include_achievement_summary"]:
        achievements = state.get("achievements", [])
        completed = sum(
            1
            for achievement in achievements
            if achievement.get("stars") == 3
        )

        lines.append("ACHIEVEMENT SUMMARY")
        lines.append("=" * 24)
        lines.append(
            f"- 3-star achievements: {completed}/{len(achievements)}"
        )
        lines.append("")

    lines.extend(
        [
            "This message was generated automatically by your "
            "Clash of Clans Personal Tracker.",
            "",
            f"Checked: {datetime.now(timezone.utc).isoformat()}",
        ]
    )

    return subject, "\n".join(lines)


def should_send_email(
    upgrades: list[dict[str, Any]],
    newly_maxed: list[dict[str, Any]],
    player_changes: list[str],
    achievement_changes: list[str],
) -> bool:
    if upgrades and CONFIG["email_on_upgrades"]:
        return True

    if newly_maxed and CONFIG["email_on_maxed_items"]:
        return True

    if player_changes and CONFIG["email_on_player_changes"]:
        return True

    if achievement_changes and CONFIG["email_on_achievement_changes"]:
        return True

    return False


def send_email(subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = GMAIL_ADDRESS
    message["To"] = GMAIL_ADDRESS
    message.set_content(body)

    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        context=context,
        timeout=30,
    ) as server:
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

    player_tag = normalize_player_tag(PLAYER_TAG)

    print(f"Player: {player_tag}")
    print("Data source: ClashKing public proxy")
    print()

    try:
        player = fetch_player(player_tag)
    except Exception as exc:
        print(f"ERROR fetching player: {exc}")
        return 1

    new_state = build_state(player)
    old_state = load_state()

    # First successful run = baseline.
    if old_state is None:
        save_state(new_state)

        print()
        print("No previous state found.")
        print("Current player data has been saved as the baseline.")
        print("No upgrade email will be sent on the first run.")
        print(f"Created: {STATE_FILE}")

        return 0

    upgrades, newly_maxed = detect_level_changes(old_state, new_state)
    player_changes = detect_player_changes(old_state, new_state)
    achievement_changes = detect_achievement_changes(old_state, new_state)

    print()
    print(f"Upgrade changes detected: {len(upgrades)}")
    print(f"Newly maxed items: {len(newly_maxed)}")
    print(f"Player changes: {len(player_changes)}")
    print(f"Achievement changes: {len(achievement_changes)}")

    # Save only after a successful API fetch and comparison.
    # This prevents a failed API request from destroying the last good state.
    save_state(new_state)

    if should_send_email(
        upgrades,
        newly_maxed,
        player_changes,
        achievement_changes,
    ):
        subject, body = build_email(
            new_state,
            upgrades,
            newly_maxed,
            player_changes,
            achievement_changes,
        )

        try:
            send_email(subject, body)
            print("Gmail notification sent successfully.")
        except smtplib.SMTPAuthenticationError as exc:
            print(f"ERROR: Gmail authentication failed: {exc}")
            return 1
        except smtplib.SMTPException as exc:
            print(f"ERROR: Gmail SMTP error: {exc}")
            return 1

    else:
        print("No configured notification-worthy changes.")

    print("State saved successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
