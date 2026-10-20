"""Before/after microbench for the MP server hot paths.

Run this same file from the pristine HEAD worktree and from the modified tree:

    py tests/mp_hotpath_benchmark.py --json out.json

No timing threshold and no device/FPS claim; medians of 7 timed rounds after
warmup, reported in nanoseconds per call.  Only production entry points are
called (CoreMixin.target_actor, PersistenceMixin.remember_player and
PersistenceMixin._snapshot_state).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nso_server.mixins.core import CoreMixin  # noqa: E402
from nso_server.mixins.persistence import PersistenceMixin  # noqa: E402
from nso_server.models import Player, Room  # noqa: E402


class Store(PersistenceMixin):
    def __init__(self) -> None:
        self.saved_rooms: dict[str, list[dict]] = {}
        self.saved_worlds: dict[str, dict] = {}
        self._saved_row_index: dict[tuple[str, str], int] = {}


def make_players(count: int, room_id: str = "room") -> list[Player]:
    players = []
    for index in range(count):
        player = Player(player_id=f"{index:08x}", name=f"Player {index}",
                        writer=None, room_id=room_id)
        player.actor_id = 1000 + index
        player.appearance = {"name": player.name, "level": 40,
                             "equipment_ids": list(range(16)),
                             "equipment_upgrades": [0] * 16,
                             "equipment_systems": [0] * 16}
        player.clone = {}
        players.append(player)
    return players


def make_room(players: list[Player]) -> Room:
    room = Room(room_id="room", owner_id=players[0].player_id,
                players={player.player_id: player for player in players})
    if hasattr(room, "actor_index"):
        room.actor_index = {player.actor_id: player for player in players}
    room.ensure_world()
    for index in range(63):
        mob_id = f"mob-{index + 2}"
        room.mobs[f"1:0:{mob_id}"] = {
            "mob_id": mob_id, "template": index + 2, "map": 1, "zone": 0,
            "x": 100 + index, "y": 200 + index, "hp": 100, "max_hp": 100,
            "alive": True}
    for index in range(64):
        drop_id = f"drop-{index}"
        room.drops[drop_id] = {
            "drop_id": drop_id, "template": index % 32, "map": 1, "zone": 0,
            "x": 100 + index, "y": 200 + index, "owner": players[0].player_id,
            "expires_at": 0}
    return room


def prepare_store(players: list[Player]) -> Store:
    store = Store()
    for player in players:
        store.remember_player(player)
    return store


def bench(name: str, work, rounds: int = 7, warmup: int = 5) -> dict:
    for _ in range(warmup):
        work()
    samples = []
    checksum = None
    count = None
    for _ in range(rounds):
        start = time.perf_counter_ns()
        result, count = work()
        samples.append((time.perf_counter_ns() - start) / count)
        if checksum is not None and result != checksum:
            raise SystemExit(f"{name}: unstable result")
        checksum = result
    return {"name": name, "operations_per_call": count,
            "ns_per_op": samples, "median_ns": statistics.median(samples),
            "checksum": checksum}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()
    players = make_players(12)
    room = make_room(players)
    store = prepare_store(players)
    store.rooms = {"room": room}
    rows = store.saved_rooms["room"]
    target = players[-1].actor_id

    def target_actor_work():
        loops = int(50000 * args.scale)
        total = 0
        for _ in range(loops):
            found = CoreMixin.target_actor(room, target)
            if found is not None:
                total += found.actor_id
        return total, loops

    def remember_work():
        loops = int(2000 * args.scale)
        for _ in range(loops):
            for player in players:
                store.remember_player(player)
        return len(rows), loops * len(players)

    def saved_row_work():
        loops = int(20000 * args.scale)
        total = 0
        for _ in range(loops):
            row = store.saved_player_row("room", players[-1].player_id)
            if row is not None:
                total += row["actor_id"]
        return total, loops

    def snapshot_work():
        loops = int(500 * args.scale)
        version = 0
        for _ in range(loops):
            version = store._snapshot_state()["version"]
        return version, loops

    benchmarks = [target_actor_work, remember_work, snapshot_work]
    if hasattr(type(store), "saved_player_row"):
        benchmarks.insert(2, saved_row_work)
    result = {"python": sys.version.split()[0],
              "rows": [bench(fn.__name__.removesuffix("_work"), fn)
                       for fn in benchmarks]}
    print("| hot path | ns/op |")
    print("|---|---:|")
    for row in result["rows"]:
        print(f"| {row['name']} | {row['median_ns']:.1f} |")
    if args.json:
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
