"""HTTP clients for the LCU REST API (auth required) and Live Client Data API (no auth).

LCU:         https://127.0.0.1:{port}/  — basic auth riot:{token}, self-signed cert
Live Client: https://127.0.0.1:2999/   — no auth, self-signed cert (game process)
"""
from __future__ import annotations

from typing import Any

import httpx

from .process import LCUCredentials

# verify=False is intentional: LCU and Live Client use self-signed certs on loopback.
# httpx (unlike requests/urllib3) does NOT emit a Python warning for verify=False,
# so no warning suppression is needed here.


class LCUClient:
    """Thin httpx wrapper around the LCU REST API."""

    def __init__(self, creds: LCUCredentials):
        self._client = httpx.Client(
            base_url=f"https://127.0.0.1:{creds.port}",
            auth=("riot", creds.token),
            verify=False,
            timeout=5.0,
        )

    def get(self, path: str, **params: Any) -> Any:
        """GET path, return parsed JSON or None on any error / non-200."""
        try:
            r = self._client.get(path, params=params or None)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def get_bytes(self, path: str, **params: Any) -> bytes | None:
        """GET path, return raw response bytes (for binary assets like PNGs).

        The default `.get()` decodes as JSON which fails on images; use this
        for endpoints like /lol-game-data/assets/v1/champion-icons/<id>.png.
        """
        try:
            r = self._client.get(path, params=params or None)
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
        return None

    def post(self, path: str, payload: Any = None, **params: Any) -> Any:
        """POST path with JSON payload, return parsed JSON or None on any error / non-200."""
        try:
            r = self._client.post(path, params=params or None, json=payload)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LCUClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ---------- LCU endpoints ----------

def get_gameflow_phase(client: LCUClient) -> str:
    """Return the current gameflow phase string, e.g. 'None', 'Lobby', 'InProgress'."""
    result = client.get("/lol-gameflow/v1/phase")
    return result if isinstance(result, str) else "None"


def get_current_summoner(client: LCUClient) -> dict | None:
    """Return current summoner dict; keys include 'puuid', 'gameName', 'displayName'."""
    return client.get("/lol-summoner/v1/current-summoner")


def get_summoner_by_id(client: LCUClient, summoner_id: int | str) -> dict | None:
    """Return a summoner profile by numeric LCU summoner/account id when available."""
    return client.get(f"/lol-summoner/v1/summoners/{summoner_id}")


def get_friends(client: LCUClient) -> list[dict]:
    """Return the current friend list from the League client."""
    data = client.get("/lol-chat/v1/friends")
    return data if isinstance(data, list) else []


def get_suggested_players(client: LCUClient) -> list[dict]:
    """Return lobby suggested-player payloads when available.

    The endpoint is only active while the user is in a lobby; otherwise the LCU
    returns a non-200 RPC error and this helper falls back to an empty list.
    """
    data = client.get("/lol-suggested-players/v1/suggested-players")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("players", "suggestedPlayers", "suggestions"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def get_summoner_by_puuid_cached(client: LCUClient, puuid: str) -> dict | None:
    """Return cached summoner profile data for a known puuid, if available."""
    return client.get(f"/lol-summoner/v1/summoners-by-puuid-cached/{puuid}")


def lookup_summoners_by_riot_ids(client: LCUClient, summoner_names: list[str]) -> list[dict]:
    """Resolve Riot IDs like 'GameName#TagLine' to LCU summoner payloads.

    The LCU uses a 36-char UUID-style puuid, which differs from Riot's public API puuid.
    This endpoint bridges from Riot alias -> local LCU summoner identity.
    """
    if not summoner_names:
        return []
    data = client.post("/lol-summoner/v2/summoners/names", payload=summoner_names)
    return data if isinstance(data, list) else []


def get_match_history(client: LCUClient, puuid: str, begin: int = 0, end: int = 20) -> list[dict]:
    """Return a list of recent game dicts from the LCU match history.

    The LCU keeps roughly the last 20 games per puuid.  The response structure varies
    slightly between client versions; we handle both the nested dict and flat-list forms.
    """
    data = client.get(
        f"/lol-match-history/v1/products/lol/{puuid}/matches",
        begIndex=begin,
        endIndex=end,
    )
    if not data:
        return []
    # Newer clients: {"games": {"games": [...]}}
    if isinstance(data, dict):
        inner = data.get("games")
        if isinstance(inner, dict):
            return inner.get("games") or []
        if isinstance(inner, list):
            return inner
        # Some versions return games at the top level
        if "gameId" in data:
            return [data]
    # Older flat-list form
    if isinstance(data, list):
        return data
    return []


def get_gameflow_session(client: LCUClient) -> dict | None:
    """Return the full gameflow session dict.  Contains gameData.gameId during InProgress."""
    return client.get("/lol-gameflow/v1/session")


def get_champ_select_session(client: LCUClient) -> dict | None:
    """Return the current champ-select session payload, or None outside ChampSelect.

    Useful keys (subject to client version):
      localPlayerCellId : int  — which cell the local summoner occupies
      myTeam[]          : list of dict with cellId / championId / puuid / summonerId
      theirTeam[]       : opponent cells; in ARAM these are typically present but with
                          championId=0 because the opposing team is hidden until game start
      benchEnabled      : bool — true in ARAM, false in standard SR draft
      benchChampions[]  : list of {championId} currently sitting on the reroll bench
      actions[][]       : pick/ban action groups
    """
    return client.get("/lol-champ-select/v1/session")


def get_league_ladders(client: LCUClient, puuid: str) -> list[dict]:
    """Return ranked ladder slices around the given puuid."""
    data = client.get(f"/lol-ranked/v1/league-ladders/{puuid}")
    return data if isinstance(data, list) else []


def get_apex_league(client: LCUClient, queue_type: str, tier: str) -> dict | None:
    """Return an apex ladder payload such as Challenger / Grandmaster / Master."""
    data = client.get(f"/lol-ranked/v1/apex-leagues/{queue_type}/{tier}")
    return data if isinstance(data, dict) else None


def get_champion_summary(client: LCUClient) -> list[dict]:
    """Return champion summary list from LCU static data.

    Each entry has 'id' (int championId) and 'name' / 'alias' (strings).
    Used to map Live Client championName strings → integer championIds.
    """
    data = client.get("/lol-game-data/assets/v1/champion-summary.json")
    return data if isinstance(data, list) else []


def get_eog_stats(client: LCUClient) -> dict | None:
    """Return end-of-game stats block (only available during the EoG screen)."""
    return client.get("/lol-end-of-game/v1/eog-stats-block")


def get_game_detail(client: LCUClient, game_id: str | int) -> dict | None:
    """Return full game detail with all 10 participants.

    Unlike the match-history list endpoint (which only returns the local player),
    this endpoint returns all participants with championId, teamId, and stats.win.
    Available for any game in the client's local history (~last 20 games).
    """
    return client.get(f"/lol-match-history/v1/games/{game_id}")


# ---------- Live Client Data (port 2999, no auth) ----------

_LIVE_CLIENT_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"


def get_live_game_data() -> dict | None:
    """Fetch all-game-data from the in-process Live Client Data API.

    Returns None if the game is not in progress (API not available) or on any error.
    allPlayers[*].team is "ORDER" (blue) or "CHAOS" (red).
    """
    try:
        r = httpx.get(_LIVE_CLIENT_URL, verify=False, timeout=3.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None
