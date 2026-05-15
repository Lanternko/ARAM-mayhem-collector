"""Champion portrait cache for the recommender GUI.

Fetches square champion icons from either the local LCU asset endpoint
(when League is running — fast, loopback) or Riot's public Data Dragon
CDN (works offline-from-League with internet).  Icons are saved to
`data/icons/<championId>.png` so subsequent runs skip the network round
trip entirely.

The GUI displays icons via tk.PhotoImage at ~36px; we subsample the
native 120px icons rather than introduce a Pillow dependency.

Thread safety:
  - get() may be called from the Tk main thread (synchronous fetches
    on cache miss).
  - prefetch_all() is intended to run from a daemon thread to warm the
    disk cache in the background.
  Both paths share self._photos / self._fail / self._alias, but writes
  to those happen on whichever thread first touches a given id, and
  reads happen after.  No race in practice because PhotoImage objects
  are only registered on the Tk thread.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import tkinter as tk


DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CHAMPS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json"
DDRAGON_ICON = "https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{alias}.png"
LCU_ICON = "/lol-game-data/assets/v1/champion-icons/{cid}.png"


class IconCache:
    """Lazy championId -> Tk PhotoImage cache, backed by disk + LCU/CDN."""

    DISPLAY_PX = 36  # GUI display size; LCU/CDN icons are 120px so we subsample(3)

    def __init__(self, cache_dir: Path, lcu_creds=None) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.lcu_creds = lcu_creds
        self._photos: dict[int, "tk.PhotoImage"] = {}
        self._fail: set[int] = set()
        self._meta_lock = threading.Lock()
        self._ddragon_version: str | None = None
        self._alias: dict[int, str] = {}

    # ----- public API -----

    def get(self, champion_id: int) -> "tk.PhotoImage | None":
        """Return a sized PhotoImage for champion_id, or None.

        Order of attempts:
          1. in-memory PhotoImage cache
          2. on-disk PNG cache
          3. LCU asset endpoint (if creds provided and reachable)
          4. Data Dragon CDN
        Failures are remembered so we don't retry on every redraw tick.
        """
        if champion_id in self._photos:
            return self._photos[champion_id]
        if champion_id in self._fail:
            return None

        path = self.cache_dir / f"{champion_id}.png"
        if not path.exists() and not self._fetch_one(champion_id, path):
            self._fail.add(champion_id)
            return None

        # Tk needs to be imported on whichever thread instantiates PhotoImage;
        # delay until we actually have an image to load.
        import tkinter as tk
        try:
            img = tk.PhotoImage(file=str(path))
            factor = max(1, img.width() // self.DISPLAY_PX)
            if factor > 1:
                img = img.subsample(factor, factor)
            self._photos[champion_id] = img
            return img
        except Exception:
            self._fail.add(champion_id)
            return None

    def prefetch_all(self) -> None:
        """Background-fetch every champion's icon to fill the disk cache.

        Intended for a daemon thread.  Idempotent — already-cached files
        are skipped.  Does NOT create PhotoImage objects (those have to
        be created on the Tk thread); just warms the on-disk PNG cache.
        """
        try:
            self._ensure_meta()
        except Exception:
            return

        for cid in list(self._alias.keys()):
            if cid in self._fail:
                continue
            path = self.cache_dir / f"{cid}.png"
            if not path.exists():
                self._fetch_one(cid, path)

    # ----- fetchers -----

    def _fetch_one(self, champion_id: int, path: Path) -> bool:
        """Try LCU first, then Data Dragon.  Returns True if path was written."""
        if self._fetch_lcu(champion_id, path):
            return True
        return self._fetch_ddragon(champion_id, path)

    def _fetch_lcu(self, champion_id: int, path: Path) -> bool:
        if not self.lcu_creds:
            return False
        try:
            # Local import avoids cyclic dependency between this module and lcu.client.
            from aram_nn.lcu.client import LCUClient
            with LCUClient(self.lcu_creds) as lcu:
                data = lcu.get_bytes(LCU_ICON.format(cid=champion_id))
                if data:
                    path.write_bytes(data)
                    return True
        except Exception:
            pass
        return False

    def _fetch_ddragon(self, champion_id: int, path: Path) -> bool:
        try:
            self._ensure_meta()
            alias = self._alias.get(champion_id)
            if not alias:
                return False
            url = DDRAGON_ICON.format(ver=self._ddragon_version, alias=alias)
            r = httpx.get(url, timeout=5.0)
            if r.status_code == 200:
                path.write_bytes(r.content)
                return True
        except Exception:
            pass
        return False

    def _ensure_meta(self) -> None:
        """Fetch Data Dragon version + championId->alias map once."""
        with self._meta_lock:
            if self._ddragon_version and self._alias:
                return
            v = httpx.get(DDRAGON_VERSIONS, timeout=5.0).json()
            self._ddragon_version = v[0]
            data = httpx.get(
                DDRAGON_CHAMPS.format(ver=self._ddragon_version),
                timeout=5.0,
            ).json()
            for _, info in data["data"].items():
                # championId is the "key" field as a string; "id" is the alias.
                self._alias[int(info["key"])] = info["id"]
