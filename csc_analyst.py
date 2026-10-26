#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
 CSC ANALYST - interactive scouting console for CS Confederation (CSC) teams
 ===========================================================================

 Type a CSC team name (or paste its playcsc.com team page), pull the team's league demos
 straight from CSC (no API key needed), and get:
   * FK / FD (opening kill / opening death) heatmaps
   * smoke, flash, molotov and HE landing maps (with throw-from spots)
   * positioning trends for early / mid / late round, per side
   * CT setups, T site hits & execute timings, economy patterns
   * anomaly detection and counter-play suggestions, like an analyst would write
   * player roles (AWPer / entry / support / lurker, IGL hint)
   * scouting reports as PDF and/or HTML (team or single player)
   * signature plays matched against your own library of pro (HLTV) demos,
     linked to the MetaStack videos you save for those rounds

 RUN IT
   python esea_analyst.py                     interactive console (menu + commands)
   python esea_analyst.py -x "parse; report"  run commands and exit (scriptable)
   python esea_analyst.py -r scout.rc         run a resource file of commands
   python esea_analyst.py -x "show 4" --json  machine-readable output

 IN THE CONSOLE
   Type a menu number, or a command. `help` lists everything.
     team Sirens   (or https://playcsc.com/stats/team/Sirens)   set the team
     download / import / parse                     get + parse demos
     show                                          numbered list of data views
     show 4   |  show smokes                       open a view
     search smoke window side:t                    search everything
     plot                                          heatmap of the last view/search
     use smokes -> set MAP de_mirage -> run        msfconsole-style
     anomalies | counter | report                  analyst output
     role | role set <player> IGL                  player roles
     pro dir D:\CS\pro -> pro get -> pro parse     pro reference library (HLTV demos you pick)
     pro link <#> <round> <metastack url>          link a pro round to its MetaStack video
     signature | signature report                  the team's plays vs the pro rounds

 Store demos on another drive:  set DEMO_DIR D:\CS\demos   (or start with -d D:\CS\demos)

 Requires Python 3.9+. Missing packages (pandas, numpy, matplotlib, demoparser2,
 scipy, zstandard) are offered for install on first run.
"""
from __future__ import annotations

import argparse
import base64
import cmd
import csv
import gzip
import html
import importlib
import importlib.util
import io
import itertools
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

VERSION = "1.0.0"
PARSE_VERSION = 2          # 2: positions sampled through the bomb timer (late round / post-plant)
READABLE_PARSE_VERSIONS = {1, 2}  # older caches still load; `parse` upgrades them when the demo file is there
TICKRATE = 64
SCRIPT_DIR = Path(__file__).resolve().parent
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

pd = np = plt = None  # loaded by load_core()


# =============================================================================
#  Dependencies
# =============================================================================
def missing_modules(mods):
    return [m for m in mods if importlib.util.find_spec(m) is None]


def pip_install(pkgs, assume_yes=False, why=""):
    pkgs = [p for p in pkgs if p]
    if not pkgs:
        return True
    print(f"Missing Python packages{(' for ' + why) if why else ''}: {', '.join(pkgs)}")
    if not assume_yes:
        if not sys.stdin.isatty():
            print(f"Install with:  {sys.executable} -m pip install {' '.join(pkgs)}")
            return False
        ans = input("Install them now? [Y/n] ").strip().lower()
        if ans not in ("", "y", "yes"):
            return False
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", *pkgs])
    importlib.invalidate_caches()
    return rc == 0 and not missing_modules(pkgs)


def load_core(assume_yes=False):
    global pd, np, plt
    miss = missing_modules(["pandas", "numpy", "matplotlib"])
    if miss:
        extra = [m for m in ("scipy",) if importlib.util.find_spec(m) is None]
        if os.name == "nt" and importlib.util.find_spec("readline") is None:
            extra.append("pyreadline3")  # tab completion + history in the Windows console
        if not pip_install(miss + extra, assume_yes, "analysis"):
            sys.exit("pandas, numpy and matplotlib are required.")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as _plt
    import numpy as _np
    import pandas as _pd
    pd, np, plt = _pd, _np, _plt
    pd.set_option("mode.chained_assignment", None) if hasattr(pd.options.mode, "chained_assignment") else None


def have(mod):
    return importlib.util.find_spec(mod) is not None


# =============================================================================
#  Console colours
# =============================================================================
class C:
    on = True
    codes = dict(r="31", g="32", y="33", b="34", m="35", c="36", w="97", d="2", bold="1")

    @classmethod
    def s(cls, text, *styles):
        if not cls.on or not styles:
            return str(text)
        return "".join(f"\033[{cls.codes[x]}m" for x in styles) + str(text) + "\033[0m"


def info(msg):
    print(C.s("[*] ", "c", "bold") + msg)


def good(msg):
    print(C.s("[+] ", "g", "bold") + msg)


def warn(msg):
    print(C.s("[!] ", "y", "bold") + msg)


def bad(msg):
    print(C.s("[-] ", "r", "bold") + msg)


# =============================================================================
#  Options / workspace
# =============================================================================
OPTION_DEFS = [
    # name, default, description, group
    ("TEAM_URL", "", "Team to scout: CSC team name or playcsc.com team page (`team` to switch or add one)", "Team"),
    ("CSC_SEASONS", "", "CSC seasons to download, e.g. 21 or 20,21 or all (blank = current season)", "Team"),
    ("MATCH_TYPES", "regular,playoff", "Which matches to pull: regular, playoff (comma separated)", "Team"),
    ("MAX_MATCHES", "60", "Max matches to pull", "Team"),
    ("CSC_API", "https://core.playcsc.com/graphql", "CSC league API (GraphQL)", "Team"),
    ("CSC_MEDIA", "https://core.playcsc.com", "Where CSC serves org logos (`logos`)", "Team"),
    ("PLAYERS", "", "Fallback: scouted players' names/SteamID64s, comma separated", "Team"),
    ("SORT_FROM", str(Path.home() / "Downloads"), "Folder `import` pulls manually downloaded demos from", "Team"),
    ("DEMO_DIR", "", r"Where demos are stored, e.g. D:\CS\demos (blank = <workspace>\demos)", "Team"),
    ("PARSE_MAP", "", "Only download/import/parse these maps, e.g. de_mirage (blank = all). `setmap`", "Team"),
    ("PRO_DIR", "", r"Where your pro (HLTV) demos are kept, e.g. D:\CS\pro (blank = <workspace>\pro). `pro dir`", "Pro library"),
    ("PRO_POOL", "", "Maps the pro library should cover, e.g. mirage,inferno,nuke (blank = the scouted team's maps)", "Pro library"),
    ("PRO_TARGET", "25", "How many pro demos to collect, spread evenly over PRO_POOL", "Pro library"),
    ("MAP", "", "Filter: map, e.g. de_mirage (blank = all)", "Filters"),
    ("SIDE", "", "Filter: T or CT (the scouted team's side)", "Filters"),
    ("PHASE", "", "Filter: early / mid / late", "Filters"),
    ("PLAYER", "", "Filter: one scouted player", "Filters"),
    ("ROSTER", "all", "Filter: all | main (main-roster players only) | lineup (only maps the main 5 played)", "Filters"),
    ("LAST_N", "", "Filter: only the N most recent matches", "Filters"),
    ("OPPONENT", "", "Filter: matches vs an opponent (substring)", "Filters"),
    ("SEASON", "", "Filter: CSC season number(s), e.g. 21 or 20,21 (blank = all)", "Filters"),
    ("PHASE_SPLITS", "25,60", "Seconds after freeze end: early <= a < mid <= b < late", "Analysis"),
    ("SAMPLE_SECONDS", "2", "Position sampling interval when parsing (lower = bigger cache)", "Analysis"),
    ("MIN_SAMPLES", "4", "Ignore patterns seen fewer times than this", "Analysis"),
    ("HEATMAP_STYLE", "both", "density | points | both", "Output"),
    ("RADARS", "auto", "auto = download the CS2 radar images when needed | off", "Output"),
    ("CALLOUT_NAMES", "pretty", "pretty = readable/your `rename` names | raw = Valve's area names", "Output"),
    ("PLAYER_NAMES", "faceit", "faceit = CSC names (one name per player) | ingame = in-game names", "Output"),
    ("OPEN_FILES", "true", "Open the report automatically when it's built", "Output"),
    ("REPORT_FORMAT", "both", "both (HTML opens right away, PDF saved next to it) | html | pdf", "Output"),
    ("REPORT_HIDE_DROPPED", "true", "Leave players dropped from the CSC roster out of reports (heatmaps, tables, summaries)", "Output"),
    ("REPORT_TEAM_LOGO", "true", "Put the scouted team's org logo in the top right corner of every report page", "Output"),
    ("REPORT_THEME", "midnight", "Report look: midnight, blackout, carbon, crimson, emerald, navy, light, print (`style`)", "Output"),
    ("OUTPUT", "table", "table | json | csv  (how views print)", "Output"),
    ("ROWS", "15", "Max rows per printed table", "Output"),
    ("COLOR", "true", "Coloured console output", "Output"),
]
SECRET_OPTS = set()
MAP_ALIASES = {"mirage": "de_mirage", "inferno": "de_inferno", "dust2": "de_dust2", "d2": "de_dust2",
               "nuke": "de_nuke", "ancient": "de_ancient", "anubis": "de_anubis", "vertigo": "de_vertigo",
               "overpass": "de_overpass", "train": "de_train", "cache": "de_cache", "office": "cs_office"}


def norm_map(v):
    v = MAP_ALIASES.get(v.strip().lower(), v.strip().lower())
    return v if v.startswith(("de_", "cs_")) else "de_" + v


class Config:
    def __init__(self, ws: Path):
        self.ws = ws
        self.path = ws / "config.json"
        self.v = {k: d for k, d, _, _ in OPTION_DEFS}
        self.transient = {}  # per-run overrides (e.g. --json), never saved
        self.labels = {}     # friendlier display values, e.g. team name for TEAM_URL
        if self.path.exists():
            try:
                self.v.update({k.upper(): str(val) for k, val in json.loads(self.path.read_text()).items()})
            except Exception:
                warn(f"Could not read {self.path}; using defaults.")
        for env, key in (("FACEIT_API_KEY", "API_KEY"), ("FACEIT_DOWNLOADS_TOKEN", "DOWNLOADS_TOKEN")):
            if not self.v.get(key) and os.environ.get(env):
                self.v[key] = os.environ[env]

    def save(self):
        self.ws.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.v, indent=2))

    def __getitem__(self, k):
        k = k.upper()
        return self.transient[k] if k in self.transient else self.v.get(k, "")

    def int(self, k, default=0):
        try:
            return int(float(self[k]))
        except (TypeError, ValueError):
            return default

    def float(self, k, default=0.0):
        try:
            return float(self[k])
        except (TypeError, ValueError):
            return default

    def bool(self, k):
        return str(self[k]).strip().lower() in ("1", "true", "yes", "on", "y")

    def set(self, k, val):
        k = k.upper()
        if k not in self.v:
            raise KeyError(f"Unknown option {k}. Type `show options`.")
        val = (val or "").strip().strip('"').strip("'")
        if k == "MAP" and val:
            val = norm_map(val)
        elif k in ("PARSE_MAP", "PRO_POOL"):
            val = ",".join(dict.fromkeys(norm_map(x) for x in re.split(r"[,\s]+", val) if x and x.lower() != "all"))
        elif k == "SIDE" and val:
            val = val.upper()
            if val not in ("T", "CT"):
                raise ValueError("SIDE must be T or CT")
        elif k == "PHASE" and val:
            val = val.lower()
            if val not in ("early", "mid", "late"):
                raise ValueError("PHASE must be early, mid or late")
        elif k == "SEASON":
            nums = [x for x in re.split(r"[,\s]+", val.upper().replace("S", " ")) if x]
            if any(not x.isdigit() for x in nums):
                raise ValueError("SEASON takes season numbers, e.g. 58 or 57,58")
            val = ",".join(dict.fromkeys(str(int(x)) for x in nums))
        elif k == "ROSTER":
            val = (val or "all").lower()
            val = {"mains": "main", "main5": "lineup", "full": "lineup", "any": "all", "": "all"}.get(val, val)
            if val not in ("all", "main", "lineup"):
                raise ValueError("ROSTER must be all, main or lineup")
        elif k == "OUTPUT":
            val = val.lower() or "table"
            if val not in ("table", "json", "csv"):
                raise ValueError("OUTPUT must be table, json or csv")
        elif k == "HEATMAP_STYLE":
            val = val.lower() or "both"
            if val not in ("density", "points", "both"):
                raise ValueError("HEATMAP_STYLE must be density, points or both")
        elif k == "PHASE_SPLITS":
            a, b = [float(x) for x in val.split(",")]
            if not 0 < a < b:
                raise ValueError("PHASE_SPLITS must look like 25,60")
        elif k in ("MAX_MATCHES", "HISTORY_DEPTH", "HISTORY_DAYS", "ROWS", "LAST_N", "MIN_SAMPLES") and val:
            int(val)
        elif k == "DEMO_DIR" and val:
            d = Path(os.path.expandvars(val)).expanduser()
            if not d.is_absolute():
                d = (Path.cwd() / d).resolve()
            try:
                d.mkdir(parents=True, exist_ok=True)
                probe = d / ".write_test"
                probe.write_text("ok")
                probe.unlink()
            except OSError as e:
                raise ValueError(f"Can't write to {d}: {e.strerror or e}")
            val = str(d)
        elif k == "SAMPLE_SECONDS":
            if float(val) <= 0:
                raise ValueError("SAMPLE_SECONDS must be > 0")
        self.v[k] = val
        self.save()
        return val

    def parse_maps(self):
        return {m for m in self["PARSE_MAP"].split(",") if m}

    def shown(self, k):
        k = k.upper()
        if self.labels.get(k):
            return self.labels[k]
        v = self[k]
        if k in SECRET_OPTS and v:
            return v[:4] + "*" * max(0, len(v) - 8) + v[-4:] if len(v) > 8 else "****"
        return v


class Workspace:
    """Base folder (settings, radars, saved teams, spot names) + one sub-folder per scouted team
    (match list, parsed data, vetos, roster, images, report). Demos live in <demo folder>/<team>/<map>."""
    TEAM_FILES = ("manifest.csv", "targets.json", "roster.json", "vetos.json", "team.json")
    LEGACY_DEMO_DIR = re.compile(r"^(de_|cs_|ar_|_unsorted$|_downloads$|unknown_map$)")

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.demo_base_default = root / "demos"
        self.demo_base = self.demo_base_default
        self.team_key = None
        self._migrate_legacy()
        name = self.current_team_name() or getattr(self, "_legacy_name", "")
        if name and not self.saved_teams():  # old workspace: register its team in the saved-teams list
            try:
                tj = json.loads((self.team_dir_for(name) / "team.json").read_text())
                (self.root / "teams.json").write_text(json.dumps([{
                    "name": name, "team_id": tj.get("team_id", ""),
                    "url": f"https://www.faceit.com/en/teams/{tj.get('team_id', '')}", "used": ""}], indent=2))
            except Exception:
                pass
        self.set_team(name)

    # ---- teams ------------------------------------------------------------------
    def saved_teams(self):
        try:
            t = json.loads((self.root / "teams.json").read_text())
            return t if isinstance(t, list) else []
        except Exception:
            return []

    def current_team_name(self):
        t = self.saved_teams()
        return t[0]["name"] if t else ""

    def tidy_team_data(self):
        """Move match-list rows (and their demos, parsed data and targets) that belong to another team into
        that team's folder. Fixes lists filled before teams had separate folders. Returns {team: rows moved}."""
        rows = self.manifest()
        if not rows:
            return {}
        mine, moved = [], defaultdict(list)
        for r in rows:
            t = (r.get("Team") or "").strip()
            if t and safe_name(t) != self.team_key:
                moved[t].append(r)
            else:
                mine.append(r)
        if not moved:
            return {}
        # rewrite this team's list
        with open(self.manifest_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.MANIFEST_FIELDS)
            w.writeheader()
            for r in mine:
                w.writerow({k: r.get(k, "") for k in self.MANIFEST_FIELDS})
        try:
            vetos = json.loads((self.team_root / "vetos.json").read_text())
        except Exception:
            vetos = {}
        targets = self.targets()
        for team, rs in moved.items():
            tdir = self.team_dir_for(team)
            (tdir / "cache").mkdir(parents=True, exist_ok=True)
            mp = tdir / "manifest.csv"
            existing = []
            if mp.exists():
                with open(mp, newline="", encoding="utf-8") as f:
                    existing = list(csv.DictReader(f))
            have = {r["FileName"] for r in existing}
            with open(mp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=self.MANIFEST_FIELDS)
                w.writeheader()
                for r in sorted(existing + [r for r in rs if r["FileName"] not in have],
                                key=lambda r: r.get("Date", ""), reverse=True):
                    w.writerow({k: r.get(k, "") for k in self.MANIFEST_FIELDS})
            other_targets = {}
            try:
                other_targets = json.loads((tdir / "targets.json").read_text())
            except Exception:
                pass
            for r in rs:
                vetos.pop(r["MatchId"], None)  # seen from the wrong side: refetched for the right team
                for d in self.demo_dirs():
                    for f in list(d.rglob("*.dem")):
                        if self.row_for_file(f, [r]) is r:
                            dst = (self.demo_base / safe_name(team) / safe_name(r["Map"]) / f.name)
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            if not dst.exists():
                                shutil.move(str(f), dst)
                            stem = demo_stem(f)
                            for c in (self.cache / f"{stem}.pkl", self.cache / f"{stem}.meta.json"):
                                if c.exists():
                                    shutil.move(str(c), tdir / "cache" / c.name)
                            if stem in targets:
                                other_targets[stem] = targets.pop(stem)
            (tdir / "targets.json").write_text(json.dumps(other_targets, indent=2))
        self.targets_path.write_text(json.dumps(targets, indent=2))
        (self.team_root / "vetos.json").write_text(json.dumps(vetos, indent=2))
        return {t: len(rs) for t, rs in moved.items()}

    def other_team_rows(self):
        """{team name: manifest rows} for every other saved team in this workspace."""
        out = {}
        for t in self.saved_teams():
            if safe_name(t["name"]) == self.team_key:
                continue
            mp = self.team_dir_for(t["name"]) / "manifest.csv"
            if mp.exists():
                with open(mp, newline="", encoding="utf-8") as f:
                    out[t["name"]] = list(csv.DictReader(f))
        return out

    def team_dir_for(self, name):
        return self.root / "teams" / safe_name(name or "_default")

    def set_team(self, name):
        """Switch every team-specific path to this team's folder (created on first use)."""
        key = safe_name(name or "_default")
        default = self.root / "teams" / "_default"
        target = self.root / "teams" / key
        if key != "_default" and default.exists() and not target.exists() and any(default.iterdir()):
            default.rename(target)  # data gathered before a team was set belongs to the first team
            for base in {self.demo_base, self.demo_base_default}:
                if (base / "_default").exists() and not (base / key).exists():
                    (base / "_default").rename(base / key)
        self.team_key, self.team_name = key, name
        self.team_root = target
        self.cache = target / "cache"
        self.out = target / "output"
        for d in (target, self.cache, self.out):
            d.mkdir(parents=True, exist_ok=True)
        self.manifest_path = target / "manifest.csv"
        self.targets_path = target / "targets.json"
        self._apply_demo_dirs()

    def set_demo_dir(self, path=""):
        """Point demo storage somewhere else (e.g. another drive). Blank = <workspace>/demos."""
        self.demo_base = Path(path) if path else self.demo_base_default
        self._apply_demo_dirs()

    def _apply_demo_dirs(self):
        for base in {self.demo_base, self.demo_base_default}:
            self._migrate_flat_demos(base)
        self.demos = self.demo_base / self.team_key
        self.default_demos = self.demo_base_default / self.team_key
        self.demos.mkdir(parents=True, exist_ok=True)
        self.downloads = self.demos / "_downloads"   # temp files stay on the same drive as the demos

    # ---- one-time move from the old single-team layout ----------------------------
    def _legacy_team(self):
        try:
            return json.loads((self.root / "team.json").read_text()).get("name", "")
        except Exception:
            return self.current_team_name()

    def _migrate_legacy(self):
        self._legacy_name = self._legacy_team()  # read before team.json moves
        flat = [self.root / f for f in self.TEAM_FILES if (self.root / f).exists()]
        dirs = [self.root / d for d in ("cache", "output") if (self.root / d).exists() and any((self.root / d).iterdir())]
        if not flat and not dirs:
            return
        dest = self.team_dir_for(self._legacy_name)
        dest.mkdir(parents=True, exist_ok=True)
        for f in flat:
            if not (dest / f.name).exists():
                shutil.move(str(f), dest / f.name)
        for d in dirs:
            (dest / d.name).mkdir(exist_ok=True)
            for item in d.iterdir():
                if not (dest / d.name / item.name).exists():
                    shutil.move(str(item), dest / d.name / item.name)
            try:
                d.rmdir()
            except OSError:
                pass

    def _migrate_flat_demos(self, base):
        """Old layout kept <demos>/<map>/...; move those into <demos>/<team>/<map>/."""
        if not base.exists():
            return
        olds = [d for d in base.iterdir() if d.is_dir() and self.LEGACY_DEMO_DIR.match(d.name)]
        if not olds:
            return
        team = safe_name(getattr(self, "_legacy_name", "") or self._legacy_team() or self.team_name or "_default")
        dest = base / team
        dest.mkdir(parents=True, exist_ok=True)
        for d in olds:
            if (dest / d.name).exists():
                for item in d.rglob("*"):
                    if item.is_file():
                        tgt = dest / d.name / item.relative_to(d)
                        tgt.parent.mkdir(parents=True, exist_ok=True)
                        if not tgt.exists():
                            shutil.move(str(item), tgt)
                shutil.rmtree(d, ignore_errors=True)
            else:
                shutil.move(str(d), dest / d.name)

    # manifest: one row per demo (map) of each league match
    MANIFEST_FIELDS = ["MatchId", "Date", "Team", "Opponent", "Map", "MapIndex", "Competition", "RoomUrl",
                       "ResourceUrl", "FileName", "TeamSteamIds", "TeamNicks", "TeamFaceitNicks"]

    def manifest(self):
        if not self.manifest_path.exists():
            return []
        with open(self.manifest_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def save_manifest(self, rows):
        old = {r["FileName"]: r for r in self.manifest()}
        for r in rows:
            old[r["FileName"]] = r
        with open(self.manifest_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.MANIFEST_FIELDS)
            w.writeheader()
            for r in sorted(old.values(), key=lambda r: r.get("Date", ""), reverse=True):
                w.writerow({k: r.get(k, "") for k in self.MANIFEST_FIELDS})

    def manifest_for(self, demo: Path, man=None):
        return self.row_for_file(demo, man)

    def row_for_file(self, f: Path, man=None):
        """Which league match/map a demo file is, whatever it's called (our name, an older name of ours,
        FACEIT's own 1-<match id>-1-<n> name, or anything containing the match id)."""
        man = self.manifest() if man is None else man
        stem, name = demo_stem(f), f.name
        for r in man:
            if demo_stem(Path(r["FileName"])) == stem:
                return r
        for r in man:
            if demo_stem(Path(r["ResourceUrl"])) == stem:
                return r
        cands = [r for r in man if r.get("MatchId") and r["MatchId"] in name]
        if cands:
            if len(cands) > 1:
                folder = f.parent.name
                byfolder = [r for r in cands if r["Map"] == folder]
                if len(byfolder) == 1:
                    return byfolder[0]
                mi = re.search(r"-(\d+)$", stem)
                if mi:
                    hit = [r for r in cands if r["MapIndex"] == mi.group(1)]
                    if hit:
                        return hit[0]
            return cands[0]
        for r in man:  # names written by older versions of this script (no match id)
            series = sum(x["MatchId"] == r["MatchId"] for x in man) > 1
            old = safe_name(f"{r['Date']}_vs_{r['Opponent']}_{r['Map']}{'_m' + r['MapIndex'] if series else ''}")
            if stem == old:
                return r
        return None

    def demo_dirs(self):
        dirs = [self.demos]
        if self.default_demos.resolve() != self.demos.resolve():
            dirs.append(self.default_demos)
        return [d for d in dirs if d.exists()]

    def all_copies(self):
        """(match_id, map) -> [demo files] - more than one entry means duplicate copies on disk."""
        man = self.manifest()
        out = defaultdict(list)
        for d in self.demo_dirs():
            for f in d.rglob("*.dem"):
                if "_downloads" in f.parts or not f.is_file() or f.stat().st_size < MIN_DEMO_BYTES:
                    continue
                r = self.row_for_file(f, man)
                if r:
                    out[(r["MatchId"], r["Map"])].append(f)
        return out

    def duplicate_files(self):
        """Extra copies to remove: keeps the one with this script's current name (else the newest)."""
        man = {(r["MatchId"], r["Map"]): demo_stem(Path(r["FileName"])) for r in self.manifest()}
        extra = []
        for key, files in self.all_copies().items():
            if len(files) < 2:
                continue
            keep = next((f for f in files if demo_stem(f) == man.get(key)), max(files, key=lambda f: f.stat().st_mtime))
            extra += [f for f in files if f != keep]
        return extra

    def have_index(self):
        """(match_id, map) -> ("file", path) | ("data", cache) for every league demo we already have."""
        man = self.manifest()
        idx = {}
        for c in self.cache.glob("*.pkl"):
            meta = cache_meta(c)
            if meta.get("match_id") and meta.get("map"):
                idx[(meta["match_id"], meta["map"])] = ("data", c)
        broken = {}
        for d in self.demo_dirs():
            for f in d.rglob("*.dem"):
                if "_downloads" in f.parts or not f.is_file():
                    continue
                r = self.row_for_file(f, man)
                if not r:
                    continue
                key = (r["MatchId"], r["Map"])
                if f.stat().st_size < MIN_DEMO_BYTES:  # tiny = broken/partial download: fetch it again
                    broken[key] = ("broken", f)
                elif idx.get(key, ("",))[0] != "file":
                    idx[key] = ("file", f)
        for key, v in broken.items():
            if idx.get(key, ("",))[0] != "file":
                idx[key] = v
        return idx

    def targets(self):
        try:
            return json.loads(self.targets_path.read_text())
        except Exception:
            return {}

    def save_target(self, stem, sids):
        t = self.targets()
        t[stem] = sorted(sids)
        self.targets_path.write_text(json.dumps(t, indent=2))

    def demo_files(self):
        return sorted(p for p in self.demos.rglob("*.dem") if p.is_file() and "_downloads" not in p.parts)


MIN_DEMO_BYTES = int(os.environ.get("ESEA_MIN_DEMO_BYTES", 512 * 1024))


def cache_meta(cache: Path):
    """Small sidecar with a parsed demo's match id/map (written at parse time; rebuilt from old caches)."""
    side = cache.with_suffix(".meta.json")
    try:
        return json.loads(side.read_text())
    except Exception:
        pass
    try:
        meta = pd.read_pickle(cache).get("meta", {})
        side.write_text(json.dumps(meta, default=str))
        return meta
    except Exception:
        return {}


def human_size(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def demo_stem(p: Path):
    n = p.name
    for ext in (".dem.gz", ".dem.zst", ".dem"):
        if n.lower().endswith(ext):
            return n[: -len(ext)]
    return p.stem


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s)).strip() or "unknown"


def open_path(path, cfg):
    if not cfg.bool("OPEN_FILES"):
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# =============================================================================
#  FACEIT API
# =============================================================================
class FaceitError(Exception):
    pass


LEAGUE_WORDS = ("regular season", "playoffs", "playoff", "relegation", "promotion")


def season_of(comp):
    """ESEA season number from a FACEIT competition name: 'S58 NA Open9-10 East C - Regular Season' -> 58."""
    m = re.search(r"\b(?:S|Season\s*)(\d{1,3})\b", str(comp or ""), re.I)
    return int(m.group(1)) if m else None


def season_set(val):
    return {int(x) for x in str(val or "").split(",") if x.strip().isdigit()}


def is_league(comp, filt):
    """Does a FACEIT competition name look like the league? 'auto' (or 'ESEA') recognises ESEA league names,
    e.g. 'S58 NA Open9-10 East C - Regular Season' or 'ESEA S55 Open NA'. Anything else: words separated by |,
    any of which must appear in the name. Blank = every match."""
    c = (comp or "").lower()
    f = (filt or "").strip().lower()
    if not f or f in ("all", "*", "any"):
        return True
    if f in ("auto", "esea", "league"):
        if "esea" in c:
            return True
        if re.match(r"^\s*s\d{1,3}\b", c) and any(w in c for w in LEAGUE_WORDS + ("open", "intermediate", "main",
                                                                                   "advanced", "challenger", "premier", "entry")):
            return True
        return any(w in c for w in LEAGUE_WORDS) and not any(x in c for x in ("queue", "10man", "hub", "pug"))
    return any(w.strip() and w.strip() in c for w in f.split("|"))


def my_faction(teams, team_id, team_name="", roster_ids=()):
    """Which faction ('faction1'/'faction2') is the scouted team: by id, then name, then roster overlap."""
    teams = teams or {}
    for f in ("faction1", "faction2"):
        if (teams.get(f) or {}).get("faction_id") == team_id:
            return f
    for f in ("faction1", "faction2"):
        if team_name and (teams.get(f) or {}).get("name", "").strip().lower() == team_name.strip().lower():
            return f
    roster_ids = set(roster_ids or ())
    if roster_ids:
        ov = {f: len({p.get("player_id") for p in (teams.get(f) or {}).get("roster") or []} & roster_ids)
              for f in ("faction1", "faction2")}
        best = max(ov, key=ov.get)
        if ov[best] >= 2:
            return best
    return None


class Faceit:
    BASE = "https://open.faceit.com/data/v4"
    DL = "https://open.faceit.com/download/v2/demos/download"

    def __init__(self, key, dl_token=""):
        if not key:
            raise FaceitError("API_KEY is not set. Get a free server-side key at developers.faceit.com, "
                              "then: set API_KEY <key>")
        self.key, self.dl_token = key, dl_token

    def _req(self, url, data=None, token=None):
        headers = {"Authorization": f"Bearer {token or self.key}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(data).encode()
        for attempt in range(5):
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(4 * (attempt + 1))
                    continue
                if e.code == 404:
                    return None
                if e.code in (401, 403):
                    raise FaceitError(f"FACEIT rejected the key/token (HTTP {e.code}) for {url.split('?')[0]}")
                if e.code >= 500 and attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise FaceitError(f"HTTP {e.code} from FACEIT for {url.split('?')[0]}")
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise FaceitError(f"Network error: {e.reason}")
        raise FaceitError("FACEIT kept rate-limiting; try again in a minute.")

    def get(self, path, **q):
        url = self.BASE + path + ("?" + urllib.parse.urlencode(q) if q else "")
        return self._req(url)

    def resolve_team(self, url_or_name, game="cs2"):
        m = UUID_RE.search(url_or_name or "")
        if m:
            team = self.get(f"/teams/{m.group(0)}")
            if not team:
                raise FaceitError(f"No FACEIT team with id {m.group(0)}")
            return team
        name = (url_or_name or "").rstrip("/").split("/")[-1]
        res = self.get("/search/teams", nickname=name, game=game, limit=20) or {}
        items = res.get("items", [])
        hit = next((t for t in items if t.get("name", "").lower() == name.lower()), items[0] if items else None)
        if not hit:
            raise FaceitError(f"No FACEIT team found for '{name}'")
        return self.get(f"/teams/{hit['team_id']}")

    def league_matches(self, team, league_filter, depth, max_matches, game="cs2", log=print, days=365, seasons=()):
        """League matches of a team, found through its members' match histories.

        FACEIT's history endpoint only returns the last month unless `from` is given, so we ask for `days` back.
        A history entry counts as this team's when the team id matches, or failing that the team name, or 3+ of
        the roster played on the same side."""
        tid = team["team_id"]
        tname = (team.get("name") or "").strip().lower()
        roster = {m.get("user_id") for m in team.get("members", [])}
        since = max(0, int(time.time() - days * 86400))
        found = {}
        self.diag = dict(scanned=0, team=0, comps=Counter(), other_comps=Counter())
        seen = set()
        for mem in team.get("members", []):
            log(f"    scanning {mem.get('nickname')}'s history")
            offset = 0
            while offset < min(depth, 1000):
                lim = min(100, depth - offset)
                h = self.get(f"/players/{mem['user_id']}/history", game=game, offset=offset, limit=lim,
                             **{"from": since, "to": int(time.time())}) or {}
                items = h.get("items", [])
                for it in items:
                    mid = it.get("match_id")
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    self.diag["scanned"] += 1
                    comp = it.get("competition_name") or ""
                    if not self._is_team_match(it, tid, tname, roster):
                        self.diag["other_comps"][comp] += 1
                        continue
                    self.diag["team"] += 1
                    self.diag["comps"][comp] += 1
                    if not is_league(comp, league_filter):
                        continue
                    if seasons and season_of(comp) not in seasons:
                        continue
                    found[mid] = it
                if len(items) < lim:
                    break
                offset += lim
        ms = sorted(found.values(), key=lambda x: x.get("started_at") or 0, reverse=True)
        return ms[:max_matches]

    @staticmethod
    def _is_team_match(it, tid, tname, roster):
        tms = it.get("teams", {}) or {}
        for f in ("faction1", "faction2"):
            t = tms.get(f) or {}
            if t.get("team_id") == tid or t.get("faction_id") == tid:
                return True
            if tname and (t.get("nickname") or t.get("name") or "").strip().lower() == tname:
                return True
            players = {p.get("player_id") for p in t.get("players") or []}
            if len(players & roster) >= 3:
                return True
        return False

    def manifest_rows(self, match_id, team_id, team_name, game="cs2", roster_ids=()):
        m = self.get(f"/matches/{match_id}")
        if not m:
            return [], "match not found"
        demos = m.get("demo_url") or []
        if isinstance(demos, str):
            demos = [demos]
        if not demos:
            return [], "no demo listed (unfinished, forfeited or expired)"
        maps = []
        st = self.get(f"/matches/{match_id}/stats")
        if st and st.get("rounds"):
            rs = sorted(st["rounds"], key=lambda r: int(r.get("match_round") or 0))
            maps = [(r.get("round_stats") or {}).get("Map") for r in rs]
        if not any(maps):
            maps = ((m.get("voting") or {}).get("map") or {}).get("pick") or []
        teams = m.get("teams") or {}
        fac = my_faction(teams, team_id, team_name, roster_ids)
        mine = teams.get(fac) if fac else None
        opp = (teams.get("faction2" if fac == "faction1" else "faction1") or {}) if fac else {}
        roster = (mine or {}).get("roster") or []
        roster = [p for p in roster if p.get("game_player_id")]
        sids = [str(p.get("game_player_id")) for p in roster]
        nicks = [p.get("game_player_name") or p.get("nickname") or "" for p in roster]
        fnicks = [p.get("nickname") or "" for p in roster]
        started = m.get("started_at") or m.get("configured_at") or 0
        date = datetime.fromtimestamp(int(started)).strftime("%Y-%m-%d") if started else ""
        rows = []
        for i, url in enumerate(demos):
            mp = maps[i] if i < len(maps) and maps[i] else "unknown_map"
            ext = ".dem.zst" if url.endswith(".zst") else ".dem.gz" if url.endswith(".gz") else ".dem"
            suffix = f"_m{i + 1}" if len(demos) > 1 else ""
            oname = (opp or {}).get("name", "Unknown")
            rows.append(dict(
                MatchId=match_id, Date=date, Team=team_name, Opponent=oname, Map=mp, MapIndex=str(i + 1),
                Competition=m.get("competition_name", ""), RoomUrl=f"https://www.faceit.com/en/{game}/room/{match_id}",
                ResourceUrl=url, FileName=safe_name(f"{date}_vs_{oname}_{mp}{suffix}_{match_id.split('-')[-1][:6]}") + ext,
                TeamSteamIds=";".join(sids), TeamNicks=";".join(nicks), TeamFaceitNicks=";".join(fnicks)))
        return rows, ""

    def steam_id(self, user_id, game="cs2"):
        """A FACEIT player's SteamID64 (from their FACEIT profile)."""
        if not user_id:
            return ""
        try:
            p = self.get(f"/players/{user_id}") or {}
        except Exception:  # profile lookup is a nice-to-have; never block on it
            return ""
        g = (p.get("games") or {}).get(game) or {}
        return str(g.get("game_player_id") or p.get("steam_id_64") or "")

    SITE = "https://api.faceit.com"

    def site_json(self, path):
        """FACEIT's own site API (no key). Used for the veto history, which the Data API doesn't expose."""
        req = urllib.request.Request(self.SITE + path, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) esea-analyst", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            return None

    def veto(self, match_id, team_id, team_name="", roster_ids=()):
        """Veto record for one match, from the scouted team's point of view."""
        m = self.get(f"/matches/{match_id}") or {}
        teams = m.get("teams") or {}
        mine = my_faction(teams, team_id, team_name, roster_ids)
        opp = (teams.get("faction2" if mine == "faction1" else "faction1") or {}).get("name", "?") if mine else "?"
        vm = ((m.get("voting") or {}).get("map") or {})
        pool = [e.get("game_map_id") or e.get("class_name") or e.get("guid") for e in vm.get("entities") or []]
        picks = vm.get("pick") or []
        results = {}
        # the scouted side's id in this match (may differ from team_id when matched by name or roster)
        my_ids = {team_id, (teams.get(mine) or {}).get("faction_id")} - {None, ""} if mine else {team_id}
        st = self.get(f"/matches/{match_id}/stats") or {}
        for r in st.get("rounds") or []:
            rs = r.get("round_stats") or {}
            if rs.get("Map"):
                results[rs["Map"]] = dict(won=rs.get("Winner") in my_ids, score=rs.get("Score", ""))
        steps = []
        h = self.site_json(f"/democracy/v1/match/{match_id}/history")
        payload = (h or {}).get("payload", h) or {}
        for t in payload.get("tickets") or []:
            if t.get("entity_type") != "map":
                continue
            for e in sorted(t.get("entities") or [], key=lambda e: e.get("round") or 0):
                by = e.get("selected_by")
                steps.append(dict(map=e.get("guid"), action=e.get("status"), round=e.get("round"),
                                  by="us" if by == mine else "them" if by in ("faction1", "faction2") else "?",
                                  random=bool(e.get("random"))))
        started = m.get("started_at") or 0
        return dict(match_id=match_id, date=datetime.fromtimestamp(int(started)).strftime("%Y-%m-%d") if started else "",
                    opponent=opp, best_of=m.get("best_of") or len(picks) or 1, pool=[p for p in pool if p],
                    picks=picks, results=results, steps=steps, competition=m.get("competition_name", ""))

    def signed_url(self, resource_url):
        if not self.dl_token:
            raise FaceitError("No DOWNLOADS_TOKEN")
        r = self._req(self.DL, data={"resource_url": resource_url}, token=self.dl_token) or {}
        url = (r.get("payload") or {}).get("download_url") or r.get("download_url")
        if not url:
            raise FaceitError("Downloads API returned no download_url")
        return url


class CscError(Exception):
    pass


class Csc:
    """CS Confederation's public league API (GraphQL at core.playcsc.com): teams, rosters, matches, demo links.
    Demos are public .dem.zip files on CSC's demo storage - no key or token needed."""

    def __init__(self, api="https://core.playcsc.com/graphql"):
        self.api = api or "https://core.playcsc.com/graphql"

    def q(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        last = None
        for attempt in range(4):
            req = urllib.request.Request(self.api, data=body, headers={
                "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "csc-analyst/" + VERSION})
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    out = json.load(r)
                if out.get("errors") and not out.get("data"):
                    raise CscError("; ".join(e.get("message", "?") for e in out["errors"])[:300])
                return out.get("data") or {}
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code in (429, 502, 503, 504):
                    time.sleep(3 * (attempt + 1))
                    continue
                raise CscError(f"CSC API answered {last}")
            except urllib.error.URLError as e:
                last = str(e.reason)
                if "CERTIFICATE_VERIFY_FAILED" in last:
                    raise CscError("Your Python's HTTPS certificates are out of date. Run:  "
                                   "py -m pip install --upgrade certifi   then restart the script.")
                time.sleep(2)
        raise CscError(f"Couldn't reach the CSC API ({last})")

    @staticmethod
    def team_name_from(text):
        t = str(text or "").strip()
        m = re.search(r"playcsc\.com/.*?/team/([^/?#]+)", t, re.I)
        if m:
            return urllib.parse.unquote(m.group(1)).replace("+", " ")
        return t

    MEDIA_HOSTS = ("https://core.playcsc.com", "https://core.playcsc.com/media", "https://playcsc.com",
                   "https://cscdemos.nyc3.cdn.digitaloceanspaces.com", "https://stats.playcsc.com")

    def franchises(self, active=True):
        d = self.q("{ franchises(active: %s) { id name prefix logo { url } teams { name } } }" % ("true" if active else "false"))
        return d.get("franchises") or []

    @staticmethod
    def _is_image(b):
        return b[:4] == b"RIFF" and b[8:12] == b"WEBP" or b[:8] == b"\x89PNG\r\n\x1a\n" or b[:3] == b"\xff\xd8\xff" or b[:4] == b"GIF8"

    def fetch_image(self, path, hosts):
        """Download a CSC media file, trying each host until one returns a real image. Returns (bytes, host)."""
        if path.startswith("http"):
            hosts, path = [""], path
        for h in hosts:
            url = (h.rstrip("/") + "/" + path.lstrip("/")) if h else path
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "csc-analyst/" + VERSION, "Accept": "image/*"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    b = r.read(8 << 20)
                if self._is_image(b):
                    return b, h
            except Exception:
                continue
        return None, None

    def latest_season(self):
        d = self.q("{ latestActiveSeason { number } }")
        return int((d.get("latestActiveSeason") or {}).get("number") or 0)

    def all_seasons(self):
        d = self.q("{ seasons { number } }")
        return sorted({int(x["number"]) for x in d.get("seasons") or [] if x.get("number") is not None})

    def resolve_team(self, text):
        name = self.team_name_from(text)
        if not name:
            raise CscError("No team given. Use: team <CSC team name>")
        TQ = ("{ team(teamName: %s) { id name active tier { name } franchise { name } "
              "captain { name steam64Id } players { name steam64Id } } }")
        t = None
        try:
            t = self.q(TQ % json.dumps(name)).get("team")
        except CscError:
            t = None
        if not t:  # names are case-sensitive on CSC's side: find the exact spelling, then retry
            teams = self.q("{ teams(includeInactive: true) { id name active } }").get("teams") or []
            exact = [x["name"] for x in teams if x["name"].lower() == name.lower()]
            exact.sort(key=lambda n_: not next(x.get("active", True) for x in teams if x["name"] == n_))
            if exact:
                t = self.q(TQ % json.dumps(exact[0])).get("team")
            if not t:
                close = [x["name"] for x in teams if name.lower() in x["name"].lower()][:8]
                raise CscError(f"No CSC team called '{name}'." + (f" Did you mean: {', '.join(close)}?" if close else
                                                                  " `teams` lists them all."))
        members = [{"nickname": p.get("name") or "", "user_id": str(p.get("steam64Id") or p.get("name")),
                    "steamid": str(p.get("steam64Id") or "")} for p in t.get("players") or []]
        cap = (t.get("captain") or {}).get("name") or ""
        return {"team_id": str(t["id"]), "name": t["name"], "members": members, "leader_name": cap,
                "tier": (t.get("tier") or {}).get("name", ""), "franchise": (t.get("franchise") or {}).get("name", "")}

    MATCH_Q = """{ matches(season: %d, teamId: %s) {
        id isPlayoff isBo3 completedAt scheduledDate demoUrl
        home { id name players { name steam64Id } } away { id name players { name steam64Id } }
        maps { mapNumber mapName demoUrl } } }"""

    def manifest_rows(self, team, seasons, types=("regular", "playoff"), log=print):
        """One row per map demo the team played, newest first, in the same format the rest of the script uses."""
        rows = []
        for season in seasons:
            try:
                ms = self.q(self.MATCH_Q % (int(season), json.dumps(str(team["team_id"])))).get("matches") or []
            except CscError as e:
                log(f"season {season}: {e}")
                continue
            for m in ms:
                kind = "playoff" if m.get("isPlayoff") else "regular"
                if kind not in types or not m.get("completedAt"):
                    continue
                home, away = m.get("home") or {}, m.get("away") or {}
                mine, opp = (home, away) if str(home.get("id")) == team["team_id"] else (away, home)
                maps = [x for x in m.get("maps") or [] if x.get("demoUrl")]
                if not maps and m.get("demoUrl"):
                    mp = re.search(r"_(de_[a-z0-9]+|cs_[a-z0-9]+)", m["demoUrl"])
                    maps = [{"mapNumber": 1, "mapName": mp.group(1) if mp else "unknown_map", "demoUrl": m["demoUrl"]}]
                date = str(m.get("completedAt") or m.get("scheduledDate") or "")[:10]
                players = [p for p in mine.get("players") or [] if p.get("steam64Id")]
                for x in sorted(maps, key=lambda x: x.get("mapNumber") or 0):
                    mp = x.get("mapName") or "unknown_map"
                    n = x.get("mapNumber") or 1
                    oname = opp.get("name", "Unknown")
                    rows.append(dict(
                        MatchId=f"csc{m['id']}", Date=date, Team=team["name"], Opponent=oname, Map=mp, MapIndex=str(n),
                        Competition=f"S{season} {team.get('tier', '')} - {'Playoffs' if kind == 'playoff' else 'Regular Season'}",
                        RoomUrl="", ResourceUrl=x["demoUrl"],
                        FileName=safe_name(f"{date}_vs_{oname}_{mp}" + (f"_m{n}" if m.get("isBo3") else "") + f"_csc{m['id']}")
                        + (".dem.zip" if x["demoUrl"].lower().endswith(".zip") else ".dem"),
                        TeamSteamIds=";".join(str(p["steam64Id"]) for p in players),
                        TeamNicks=";".join(p.get("name") or "" for p in players),
                        TeamFaceitNicks=";".join(p.get("name") or "" for p in players)))
        rows.sort(key=lambda r: r["Date"], reverse=True)
        return rows


VETO_Q = """{ match(matchId: %s) { ... on Matches { id completedAt scheduledDate
    home { name } away { name } maps { mapName }
    lobby { deciderMap vetoActions { order actionType map team { name } } } } } }"""


def csc_veto(api: Csc, match_id, team_name):
    """Veto record for one CSC match (scouted team's point of view), same shape the veto tables expect."""
    mid = str(match_id).replace("csc", "")
    m = (api.q(VETO_Q % json.dumps(mid)) or {}).get("match") or {}
    home, away = (m.get("home") or {}).get("name", ""), (m.get("away") or {}).get("name", "")
    opp = away if home.lower() == team_name.lower() else home
    lobby = m.get("lobby") or {}
    steps = []
    for a in sorted(lobby.get("vetoActions") or [], key=lambda a: a.get("order") or 0):
        kind = str(a.get("actionType") or "").upper()
        if not a.get("map") or kind not in ("BAN_MAP", "PICK_MAP"):
            continue
        by = ((a.get("team") or {}).get("name") or "").lower()
        steps.append(dict(map=a["map"], action="drop" if kind == "BAN_MAP" else "pick", round=a.get("order"),
                          by="us" if by == team_name.lower() else "them" if by else "?", random=False))
    picks = [x.get("mapName") for x in m.get("maps") or [] if x.get("mapName")] or \
            ([lobby["deciderMap"]] if lobby.get("deciderMap") else [])
    pool = list(dict.fromkeys([st["map"] for st in steps] + picks))
    return dict(match_id=f"csc{mid}", date=str(m.get("completedAt") or m.get("scheduledDate") or "")[:10], opponent=opp,
                best_of=max(1, len(picks)), pool=pool, picks=picks, results={}, steps=steps, competition="")


def http_download(url, dest: Path, label=""):
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "csc-analyst/" + VERSION})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, last = 0, 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if sys.stdout.isatty() and (got - last > 4 << 20):
                last = got
                pct = f" {got * 100 // total}%" if total else ""
                print(f"\r    {label} {got >> 20} MB{pct}   ", end="", flush=True)
    if sys.stdout.isatty():
        print("\r" + " " * 60 + "\r", end="")


def decompress(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = src.name.lower()
    if n.endswith(".gz"):
        with gzip.open(src, "rb") as f, open(dst, "wb") as o:
            shutil.copyfileobj(f, o, 1 << 20)
    elif n.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(src) as z:
            dems = [i for i in z.infolist() if i.filename.lower().endswith(".dem")]
            if not dems:
                raise RuntimeError(f"{src.name} has no .dem inside")
            with z.open(max(dems, key=lambda i: i.file_size)) as f, open(dst, "wb") as o:
                shutil.copyfileobj(f, o, 1 << 20)
    elif n.endswith(".zst"):
        if not have("zstandard") and not pip_install(["zstandard"], why=".zst demos"):
            raise RuntimeError("zstandard is needed to unpack .dem.zst demos")
        import zstandard
        with open(src, "rb") as f, open(dst, "wb") as o:
            zstandard.ZstdDecompressor().copy_stream(f, o)
    else:
        shutil.copyfile(src, dst)


# =============================================================================
#  Demo parsing  (demoparser2 -> tidy tables)
# =============================================================================
def pick(df, *names):
    for n in names:
        if n in df.columns:
            return n
    return None


def colv(df, *names, default=None):
    n = pick(df, *names)
    if n is None:
        return pd.Series([default] * len(df), index=df.index, dtype=object)
    return df[n]


def norm_side(v):
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except Exception:
        pass
    s = str(v).strip().lower()
    if s in ("3", "3.0") or s.startswith("ct") or "counter" in s:
        return "CT"
    if s in ("2", "2.0") or s == "t" or s.startswith("terror") or s == "t_side":
        return "T"
    return None


def sid_str(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    try:
        if isinstance(v, float):
            if math.isnan(v):
                return ""
            return str(int(v))
        return str(int(v))
    except Exception:
        return str(v)


def nade_kind(s):
    s = str(s).lower()
    if "smoke" in s:
        return "smoke"
    if "flash" in s:
        return "flash"
    if "molotov" in s or "incend" in s or "inferno" in s or "incgrenade" in s:
        return "molotov"
    if "decoy" in s:
        return "decoy"
    if "he" in s or "frag" in s:
        return "he"
    return None


class DemoParseError(Exception):
    pass


class GoHome(Exception):
    """Raised when the user types `home` at a question prompt."""


def parse_demo(path: Path, sample_s: float, target_sids: set, target_names: set, choose_team=None, log=print):
    """Parse one CS2 demo into normalised pandas tables (all coordinates in game units)."""
    from demoparser2 import DemoParser
    check_demo_file(path)
    p = DemoParser(str(path))
    try:
        header = p.parse_header() or {}
    except Exception:
        header = {}
    map_name = str(header.get("map_name") or path.parent.name or "unknown_map")

    def ev(name, player=(), other=()):
        try:
            df = p.parse_event(name, player=list(player), other=list(other))
        except Exception:
            try:
                df = p.parse_event(name)
            except Exception:
                return pd.DataFrame()
        if df is None:
            return pd.DataFrame()
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)
        return df

    # ---- rounds -------------------------------------------------------------
    start = ev("round_announce_match_start")
    match_start = int(start["tick"].max()) if len(start) and "tick" in start else 0
    rend = ev("round_end", other=["is_warmup_period"])
    rounds = []
    if len(rend) and "tick" in rend:
        rend = rend[rend["tick"] > match_start]
        if "is_warmup_period" in rend:
            rend = rend[~rend["is_warmup_period"].fillna(False).astype(bool)]
        wcol = pick(rend, "winner", "winner_side")
        if wcol:
            rend = rend[rend[wcol].map(norm_side).notna()]
        rend = rend.sort_values("tick").drop_duplicates("tick")
        fe = ev("round_freeze_end")
        fe_ticks = np.sort(fe["tick"].to_numpy()) if len(fe) and "tick" in fe else np.array([], dtype=int)
        prev = match_start
        for i, row in enumerate(rend.to_dict("records")):
            endt = int(row["tick"])
            cand = fe_ticks[(fe_ticks > prev) & (fe_ticks < endt)]
            freeze = int(cand.min()) if len(cand) else prev
            rounds.append(dict(round=i + 1, start_tick=prev, freeze_end=freeze, end_tick=endt,
                               winner_side=norm_side(row.get(wcol)) if wcol else None,
                               reason=str(row.get("reason", ""))))
            prev = endt
    if len(rounds) < 3:  # round events missing from this demo: rebuild rounds from the game's scoreboard state
        rounds = rounds_from_game_state(p, match_start, log)
    if not rounds:
        raise DemoParseError("no rounds found (neither round events nor scoreboard state). "
                             "Run `inspect` on this demo and send me the output.")
    R = pd.DataFrame(rounds)
    ends = R["end_tick"].to_numpy()
    starts = R["start_tick"].to_numpy()

    def round_of(ticks):
        t = np.asarray(ticks, dtype="int64")
        idx = np.searchsorted(ends, t, side="left")
        ok = (idx < len(ends))
        idx2 = np.where(ok, idx, 0)
        ok &= t > starts[idx2]
        return np.where(ok, idx2 + 1, 0)

    # ---- sampled positions -------------------------------------------------
    step = max(1, int(sample_s * TICKRATE))
    wanted = []
    for r in rounds:
        last = min(r["end_tick"], r["freeze_end"] + 160 * TICKRATE)  # 1:55 round + 40 s bomb timer, plus margin
        wanted.extend(range(r["freeze_end"], last + 1, step))
    props = ["X", "Y", "Z", "last_place_name", "team_num", "is_alive", "current_equip_value"]
    pos = None
    for attempt_props in (props, props[:6], props[:5]):
        try:
            try:
                pos = p.parse_ticks(attempt_props, ticks=wanted)
            except TypeError:
                pos = p.parse_ticks(attempt_props)
                pos = pos[pos["tick"].isin(set(wanted))]
            break
        except Exception as e:  # unknown prop on an older parser -> retry with fewer
            pos, err = None, e
    if pos is None or pos.empty:
        raise DemoParseError(f"could not read player positions ({err if pos is None else 'empty'})")
    pos = pos.copy()
    pos["steamid"] = colv(pos, "steamid", "player_steamid").map(sid_str)
    pos["name"] = colv(pos, "name", "player_name").astype(str)
    pos["team_num"] = pd.to_numeric(colv(pos, "team_num"), errors="coerce")
    pos = pos[pos["team_num"].isin([2, 3])]
    pos["round"] = round_of(pos["tick"])
    pos = pos[pos["round"] > 0]
    alive = colv(pos, "is_alive", default=True)
    pos["alive"] = alive.fillna(True).astype(bool)
    pos["equip"] = pd.to_numeric(colv(pos, "current_equip_value", default=0), errors="coerce").fillna(0)
    pos = pos.rename(columns={pick(pos, "X"): "x", pick(pos, "Y"): "y", pick(pos, "Z"): "z"})
    pos["place"] = colv(pos, "last_place_name", default="").fillna("").astype(str)

    # ---- which group of players is the scouted team? -------------------------
    first_tick = pos["tick"].min()
    groups = {}
    for tn, g in pos[pos["tick"] == first_tick].groupby("team_num"):
        groups[int(tn)] = (set(g["steamid"]), list(g["name"]))
    chosen = None
    if target_sids:
        ov = {tn: len(s & target_sids) for tn, (s, _) in groups.items()}
        if ov and max(ov.values()) > 0:
            chosen = max(ov, key=ov.get)
    if chosen is None and target_names:
        ov = {tn: len({n.lower() for n in names} & target_names) for tn, (_, names) in groups.items()}
        if ov and max(ov.values()) > 0:
            chosen = max(ov, key=ov.get)
    if chosen is None and choose_team:
        chosen = choose_team(map_name, groups)
    if chosen is None:
        raise DemoParseError("could not tell which team to scout (set PLAYERS, or parse interactively)")
    core = groups[chosen][0]

    # per-round team_num of the scouted team (handles the half-time swap and subs)
    tmap = {}
    for rnd, g in pos.groupby("round"):
        mine = g[g["steamid"].isin(core)]
        tmap[int(rnd)] = int(mine["team_num"].mode().iloc[0]) if len(mine) else None
    R["tgt_team_num"] = R["round"].map(tmap)
    R["tgt_side"] = R["tgt_team_num"].map(norm_side)
    R = R[R["tgt_side"].notna()].copy()
    R["tgt_won"] = R["winner_side"] == R["tgt_side"]
    pos["tgt"] = pos["team_num"] == pos["round"].map(tmap)
    pos["side"] = pos["team_num"].map(norm_side)
    fz = R.set_index("round")["freeze_end"]
    pos["t"] = (pos["tick"] - pos["round"].map(fz)) / TICKRATE
    pos = pos[pos["round"].isin(R["round"])]

    # economy: max equipment value in the first 12 s, summed per team
    early = pos[pos["t"] <= 12]
    eq = early.groupby(["round", "tgt", "steamid"])["equip"].max().groupby(["round", "tgt"]).sum().unstack()
    R["tgt_equip"] = R["round"].map(eq.get(True, pd.Series(dtype=float))).fillna(0)
    R["opp_equip"] = R["round"].map(eq.get(False, pd.Series(dtype=float))).fillna(0)

    side_of = {(int(r), s): tn for r, s, tn in pos[["round", "steamid", "team_num"]].drop_duplicates().itertuples(index=False)}

    # ---- kills / damage -----------------------------------------------------
    pprops = ["X", "Y", "Z", "last_place_name", "team_num"]
    k = ev("player_death", player=pprops)
    K = pd.DataFrame()
    if len(k):
        K = pd.DataFrame({
            "tick": k["tick"].astype("int64"),
            "att_sid": colv(k, "attacker_steamid").map(sid_str),
            "att_name": colv(k, "attacker_name", default="").fillna("").astype(str),
            "att_tn": pd.to_numeric(colv(k, "attacker_team_num"), errors="coerce"),
            "ax": pd.to_numeric(colv(k, "attacker_X"), errors="coerce"),
            "ay": pd.to_numeric(colv(k, "attacker_Y"), errors="coerce"),
            "az": pd.to_numeric(colv(k, "attacker_Z"), errors="coerce"),
            "a_place": colv(k, "attacker_last_place_name", default="").fillna("").astype(str),
            "vic_sid": colv(k, "user_steamid", "victim_steamid").map(sid_str),
            "vic_name": colv(k, "user_name", "victim_name", default="").fillna("").astype(str),
            "vic_tn": pd.to_numeric(colv(k, "user_team_num", "victim_team_num"), errors="coerce"),
            "vx": pd.to_numeric(colv(k, "user_X", "victim_X"), errors="coerce"),
            "vy": pd.to_numeric(colv(k, "user_Y", "victim_Y"), errors="coerce"),
            "vz": pd.to_numeric(colv(k, "user_Z", "victim_Z"), errors="coerce"),
            "v_place": colv(k, "user_last_place_name", "victim_last_place_name", default="").fillna("").astype(str),
            "weapon": colv(k, "weapon", default="").fillna("").astype(str),
            "headshot": colv(k, "headshot", default=False).fillna(False).astype(bool),
        })
        K["round"] = round_of(K["tick"])
        K = K[K["round"].isin(R["round"])].copy()
        for who in ("att", "vic"):  # fill team from positions when the event lacked it
            miss = K[f"{who}_tn"].isna()
            if miss.any():
                K.loc[miss, f"{who}_tn"] = np.array([side_of.get((int(r), s), np.nan) for r, s in
                                                     zip(K.loc[miss, "round"], K.loc[miss, f"{who}_sid"])], dtype=float)
        tn = K["round"].map(tmap)
        K["tgt_att"] = K["att_tn"] == tn
        K["tgt_vic"] = K["vic_tn"] == tn
        K["att_side"] = K["att_tn"].map(norm_side)
        K["vic_side"] = K["vic_tn"].map(norm_side)
        K["t"] = (K["tick"] - K["round"].map(fz)) / TICKRATE
        K = K.sort_values("tick").reset_index(drop=True)
        valid = (K["att_sid"] != "") & (K["att_sid"] != K["vic_sid"]) & (K["att_tn"] != K["vic_tn"])
        K["opening"] = False
        firsts = K[valid].groupby("round").head(1).index
        K.loc[firsts, "opening"] = True
        # trades: the killer dies to the victim's team within 5 s
        K["traded"] = False
        win = 5 * TICKRATE
        for _, g in K.groupby("round"):
            rows = g.to_dict("records")
            idxs = list(g.index)
            for i, a in enumerate(rows):
                for b in rows[i + 1:]:
                    if b["tick"] - a["tick"] > win:
                        break
                    if b["vic_sid"] == a["att_sid"] and b["att_tn"] == a["vic_tn"]:
                        K.at[idxs[i], "traded"] = True
                        break

    hurt = ev("player_hurt", player=["team_num"])
    D = pd.DataFrame()
    if len(hurt):
        D = pd.DataFrame({
            "tick": hurt["tick"].astype("int64"),
            "att_sid": colv(hurt, "attacker_steamid").map(sid_str),
            "att_tn": pd.to_numeric(colv(hurt, "attacker_team_num"), errors="coerce"),
            "vic_tn": pd.to_numeric(colv(hurt, "user_team_num"), errors="coerce"),
            "dmg": pd.to_numeric(colv(hurt, "dmg_health", default=0), errors="coerce").fillna(0).clip(upper=100),
        })
        D["round"] = round_of(D["tick"])
        D = D[(D["round"].isin(R["round"])) & (D["att_tn"] != D["vic_tn"]) & (D["att_sid"] != "")]
        D = D.groupby(["round", "att_sid"], as_index=False)["dmg"].sum()

    # ---- grenades -----------------------------------------------------------
    N = parse_nades(p, ev, round_of)
    if len(N):
        N = N[N["round"].isin(R["round"])].copy()
        N["team_num"] = np.array([side_of.get((int(r), s), np.nan) for r, s in zip(N["round"], N["sid"])], dtype=float)
        N["tgt"] = N["team_num"] == N["round"].map(tmap)
        N["side"] = N["team_num"].map(norm_side)
        N["t"] = (N["land_tick"] - N["round"].map(fz)) / TICKRATE
        # flash effectiveness from player_blind
        bl = ev("player_blind", player=["team_num"])
        N["enemies_flashed"] = 0
        N["team_flashed"] = 0
        if len(bl) and (N["kind"] == "flash").any():
            B = pd.DataFrame({
                "tick": bl["tick"].astype("int64"),
                "sid": colv(bl, "attacker_steamid").map(sid_str),
                "a_tn": pd.to_numeric(colv(bl, "attacker_team_num"), errors="coerce"),
                "u_tn": pd.to_numeric(colv(bl, "user_team_num"), errors="coerce"),
                "dur": pd.to_numeric(colv(bl, "blind_duration", default=0), errors="coerce").fillna(0),
            })
            fl = N[N["kind"] == "flash"][["land_tick", "sid"]].reset_index()
            for _, b in B[B["dur"] >= 1.0].iterrows():
                c = fl[(fl["sid"] == b["sid"]) & ((b["tick"] - fl["land_tick"]).between(-8, 40))]
                if len(c):
                    i = c.iloc[(c["land_tick"] - b["tick"]).abs().argmin()]["index"]
                    col = "team_flashed" if b["a_tn"] == b["u_tn"] else "enemies_flashed"
                    N.at[i, col] += 1

    # ---- bomb ---------------------------------------------------------------
    bp = ev("bomb_planted", player=["X", "Y", "Z", "last_place_name"])
    R["plant_tick"] = np.nan
    R["plant_place"] = ""
    R["plant_x"] = R["plant_y"] = R["plant_z"] = np.nan
    if len(bp):
        bp = bp.copy()
        bp["round"] = round_of(bp["tick"])
        for row in bp.to_dict("records"):
            m = R["round"] == row["round"]
            R.loc[m, "plant_tick"] = row["tick"]
            R.loc[m, "plant_place"] = str(row.get("user_last_place_name") or "")
            R.loc[m, "plant_x"] = row.get("user_X")
            R.loc[m, "plant_y"] = row.get("user_Y")
            R.loc[m, "plant_z"] = row.get("user_Z")
    R["plant_t"] = (R["plant_tick"] - R["freeze_end"]) / TICKRATE

    pos = pos[["round", "tick", "t", "steamid", "name", "side", "tgt", "alive", "x", "y", "z", "place"]]
    names = pos.groupby("steamid")["name"].agg(lambda s: s.mode().iloc[0]).to_dict()
    return dict(map=map_name, rounds=R, kills=K, dmg=D, nades=N, pos=pos, names=names,
                core=sorted(core), groups={k: v[1] for k, v in groups.items()})


def check_demo_file(path: Path):
    """Make sure this is an unpacked CS2 demo: unpack it in place if it's still .gz/.zst inside, else explain."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"PBDEMS2"):
        return
    kind = "gz" if head[:2] == b"\x1f\x8b" else "zst" if head[:4] == b"\x28\xb5\x2f\xfd" else None
    if kind:
        tmp = path.with_name(path.name + "." + kind)
        path.rename(tmp)
        try:
            decompress(tmp, path)
            tmp.unlink()
        except Exception:
            tmp.rename(path)
            raise
        with open(path, "rb") as f:
            if f.read(7) == b"PBDEMS2":
                return
    if head.startswith(b"HL2DEMO"):
        raise DemoParseError("this is a CS:GO demo, not CS2")
    size = path.stat().st_size
    raise DemoParseError(f"not a CS2 demo file ({human_size(size)}) - probably an incomplete download; "
                         f"delete it and download it again")


def rounds_from_game_state(p, match_start=0, log=print):
    """Rounds rebuilt from game-rules props (rounds played counter, freeze time, team scores).
    Used when a demo has no round_end events."""
    props = ["total_rounds_played", "is_freeze_period", "is_warmup_period", "team_rounds_total", "team_num"]
    df = None
    for pr in (props, props[:2] + props[3:], ["total_rounds_played", "team_rounds_total", "team_num"]):
        try:
            df = p.parse_ticks(pr)
            break
        except Exception:
            df = None
    if df is None or not len(df) or "total_rounds_played" not in df:
        return []
    df = df[df["tick"] >= match_start]
    g = df.groupby("tick").agg(trp=("total_rounds_played", "max"),
                               freeze=("is_freeze_period", "max") if "is_freeze_period" in df else ("total_rounds_played", lambda x: False),
                               warm=("is_warmup_period", "max") if "is_warmup_period" in df else ("total_rounds_played", lambda x: False))
    g = g.sort_index()
    g = g[~g["warm"].astype(bool)]
    if not len(g):
        return []
    trp = g["trp"].to_numpy()
    ticks = g.index.to_numpy()
    drops = np.where(np.diff(trp) < 0)[0]
    if len(drops):  # match restarted (e.g. after a knife/warmup): keep the last run
        cut = drops[-1] + 1
        g, trp, ticks = g.iloc[cut:], trp[cut:], ticks[cut:]
    scores = None
    if "team_rounds_total" in df and "team_num" in df:
        sc = df[df["team_num"].isin([2, 3])]
        scores = sc.pivot_table(index="tick", columns="team_num", values="team_rounds_total", aggfunc="max").sort_index()
    freeze = g["freeze"].astype(bool).to_numpy()
    rounds, prev = [], int(ticks[0])
    for i in np.where(np.diff(trp) > 0)[0]:
        endt = int(ticks[i + 1])
        seg = (ticks > prev) & (ticks <= endt)
        fz = np.where(seg[:-1] & freeze[:-1] & ~freeze[1:])[0] if len(freeze) > 1 else []
        freeze_end = int(ticks[fz[0] + 1]) if len(fz) else prev
        winner = None
        if scores is not None and len(scores):
            win = 5 * TICKRATE  # scores can update a moment after the round counter
            b = scores[(scores.index >= prev) & (scores.index <= prev + win)]
            before = b.max() if len(b) and rounds else scores[scores.index <= prev].tail(1).max()
            a_ = scores[(scores.index >= endt) & (scores.index <= endt + win)]
            after = a_.max() if len(a_) else scores[scores.index >= endt].head(1).max()
            if before is not None and after is not None and len(after):
                diff = (after - before).fillna(0)
                if diff.max() > 0:
                    winner = norm_side(int(diff.idxmax()))
        rounds.append(dict(round=len(rounds) + 1, start_tick=prev, freeze_end=freeze_end, end_tick=endt,
                           winner_side=winner, reason="from game state"))
        prev = endt
    if rounds:
        log(f"    (no round events in this demo - rebuilt {len(rounds)} rounds from the game state)")
    return rounds


def parse_nades(p, ev, round_of):
    """Landing spot + throw spot for every grenade."""
    rows = []
    g = None
    try:
        g = p.parse_grenades()
    except Exception:
        g = None
    if g is not None and len(g):
        g = g.copy()
        ent = pick(g, "grenade_entity_id", "entity_id", "entityid")
        xs, ys, zs = pick(g, "x", "X"), pick(g, "y", "Y"), pick(g, "z", "Z")
        typ = pick(g, "grenade_type", "type")
        g["sid"] = colv(g, "steamid", "thrower_steamid").map(sid_str)
        g["thrower"] = colv(g, "name", "thrower", default="").fillna("").astype(str)
        g = g.dropna(subset=[xs, ys])
        g = g.sort_values([ent, "tick"])
        seg = (g[ent] != g[ent].shift()) | (g["tick"].diff() > 32) | (g["sid"] != g["sid"].shift())
        g["seg"] = seg.cumsum()
        for _, s in g.groupby("seg"):
            kind = nade_kind(s[typ].iloc[0])
            if kind in (None, "decoy") or len(s) < 2:
                continue
            fx, fy, fz_ = s[xs].iloc[-1], s[ys].iloc[-1], s[zs].iloc[-1]
            d = np.hypot(s[xs] - fx, s[ys] - fy)
            land_tick = int(s.loc[d < 2, "tick"].iloc[0]) if (d < 2).any() else int(s["tick"].iloc[-1])
            rows.append(dict(kind=kind, sid=s["sid"].iloc[0], name=s["thrower"].iloc[0],
                             throw_tick=int(s["tick"].iloc[0]), land_tick=land_tick,
                             tx=s[xs].iloc[0], ty=s[ys].iloc[0], tz=s[zs].iloc[0], lx=fx, ly=fy, lz=fz_))
    if not rows:  # fallback: detonation events (no throw spot)
        for evn, kind in (("smokegrenade_detonate", "smoke"), ("flashbang_detonate", "flash"),
                          ("hegrenade_detonate", "he"), ("inferno_startburn", "molotov")):
            e = ev(evn)
            for r in e.to_dict("records") if len(e) else []:
                rows.append(dict(kind=kind, sid=sid_str(r.get("user_steamid")), name=str(r.get("user_name") or ""),
                                 throw_tick=int(r["tick"]), land_tick=int(r["tick"]),
                                 tx=np.nan, ty=np.nan, tz=np.nan, lx=r.get("x"), ly=r.get("y"), lz=r.get("z")))
    N = pd.DataFrame(rows)
    if len(N):
        N["round"] = round_of(N["land_tick"])
        N = N[N["round"] > 0]
    return N


# =============================================================================
#  Dataset: all parsed demos + filters + derived columns
# =============================================================================

# Common community callout names per map - offered as choices when you name spots with `callouts learn <map>`.
# (Names only. The demos don't contain these; you place them once per map and the script uses them from then on.)
FINE_CALLOUT_SUGGESTIONS = {
    "de_mirage": ["Firebox", "Triple", "Stairs", "Tetris", "Sandwich", "Jungle", "Bench", "Van", "Market", "Empty",
                  "E Box", "Catwalk", "Apartments", "Connector", "Pillars", "Palace", "Ramp", "Ticket", "Window",
                  "Short", "Underpass", "B Short", "Kitchen", "Default", "Ninja", "CT Spawn", "Top Mid", "Chair"],
    "de_inferno": ["Mexico", "Banana", "Boiler", "Car", "Apartments", "Graveyard", "Pit", "Fountain", "Library",
                   "Arch", "Long", "Short", "Quad", "Construction", "Coffins", "Dark", "Ruins", "Second Mid", "Top Mid",
                   "Moto", "Logs", "Sandbags", "Church", "Kitchen"],
    "de_ancient": ["Split", "Donut", "Top Mid", "Cubby", "Sniper's Nest", "Ramp", "Cave", "Cat", "Ruins", "Large Boxes",
                   "Triple Boxes", "Trees", "A Main", "B Main", "Elbow", "House", "Temple", "Heaven", "Pillar"],
    "de_nuke": ["A Site", "Heaven", "Hell", "Rafters", "Hut", "Squeaky", "Main", "Tetris", "Lobby", "Trophy", "Radio",
                "B Site", "Window", "Decon", "Doors", "Tunnels", "Back Vents", "Ramp", "Bottom Ramp", "Headshot",
                "Big Box", "Control", "Dark", "Outside", "Silo", "T Red", "CT Red", "Garage", "Secret", "Lockers", "T Roof"],
    "de_overpass": ["A Long", "Upper Tunnels", "Lower Tunnels", "Connector", "Party", "Mid", "Fountain", "Playground",
                    "Van", "Default", "Restroom", "A Short", "Monster", "Pipe", "Water", "Squeaky", "B Short",
                    "Sandbags", "Pillar", "Toxic Barrels", "Pit", "Heaven", "Bench", "Storage", "Bridge"],
    "de_train": ["A Main", "Olof", "E Box", "Hell", "Yellow", "A Red", "A Site", "A1", "A2", "A Doors", "B Halls", "Upper B",
                 "B Ramp", "B Ladder", "Orange", "B Site", "Blue", "B Red", "Headshot", "Catwalk", "Back Site", "T Stairs",
                 "CT Stairs", "Kitchen", "Showers", "Underpass", "Connector", "Old Heaven", "Ivy", "Pigeon", "Alley",
                 "Dumpster", "CT Tunnel"],
    "de_vertigo": ["Top Mid", "T Mid", "Mid", "A Ramp", "Scaffolding", "Sidewalk", "Fence", "Elevators"],
    "de_dust2": ["Car", "Goose", "Suicide", "Upper Tunnels", "Lower Tunnels", "A Short", "Cat", "Doors", "Long",
                 "Pit", "Blue", "Xbox", "Mid", "B Window", "Platform", "Back Plat", "Closet"],
    "de_cache": ["A Main", "Squeaky", "A Long", "Shroud", "NBK", "Quad", "Forklift", "Balcony", "Highway", "Default",
                 "B Main", "B Halls", "Sun Room", "Toxic", "Checkers", "Back Checkers", "Vents", "Headshot", "Heaven",
                 "Back Site", "Pit", "Garage", "T Red", "Cubby", "Boost", "White Box", "CT Connector", "Sandbags", "Mid Roof"],
    "de_anubis": [],
}
FINE_CALLOUT_FILE = "callout_points.json"


def kmeans_np(X, k, iters=20, seed=1):
    """Small k-means (no extra packages): returns (centers, labels)."""
    rng = np.random.default_rng(seed)
    C_ = [X[rng.integers(len(X))]]
    for _ in range(1, k):  # k-means++ start
        d = np.min(((X[:, None, :] - np.array(C_)[None, :, :]) ** 2).sum(-1), axis=1)
        C_.append(X[rng.choice(len(X), p=d / d.sum())] if d.sum() > 0 else X[rng.integers(len(X))])
    C_ = np.array(C_, dtype=float)
    lab = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        lab = np.argmin(((X[:, None, :] - C_[None, :, :]) ** 2).sum(-1), axis=1)
        new = np.array([X[lab == j].mean(0) if (lab == j).any() else C_[j] for j in range(k)])
        if np.allclose(new, C_):
            break
        C_ = new
    return C_, lab


class Dataset:
    def __init__(self, ws: Workspace, cfg: Config):
        self.ws, self.cfg = ws, cfg
        self.loaded = False
        self.rounds = self.kills = self.nades = self.pos = self.dmg = self.matches = None
        self.names = {}
        self.zones = {}
        self.index = None

    def clear(self):
        """Forget everything in memory (used when switching teams)."""
        self.loaded = False
        self.rounds = self.kills = self.nades = self.pos = self.dmg = self.matches = None
        self.index = None
        self.names, self.zones = {}, {}
        self._roster = None
        self.version = getattr(self, "version", 0) + 1

    def cache_file(self, demo: Path):
        return self.ws.cache / (demo_stem(demo) + ".pkl")

    def load(self, quiet=False):
        parts = defaultdict(list)
        metas, names = [], {}
        seen = set()
        for f in sorted(self.ws.cache.glob("*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                d = pd.read_pickle(f)
            except Exception:
                warn(f"cache {f.name} unreadable; re-run parse")
                continue
            if d.get("version") not in READABLE_PARSE_VERSIONS:
                continue
            meta = d["meta"]
            key = (meta.get("match_id"), meta.get("map"))
            if meta.get("match_id") and meta.get("match_id") != meta.get("demo"):
                if key in seen:  # same match/map parsed twice under different file names: keep the newest
                    continue
                seen.add(key)
            metas.append(meta)
            names.update(d.get("names", {}))
            for key in ("rounds", "kills", "nades", "pos", "dmg"):
                t = d.get(key)
                if t is None or len(t) == 0:
                    continue
                t = t.copy()
                for mk in ("demo", "map", "match_id", "date", "opponent"):
                    t[mk] = meta[mk]
                parts[key].append(t)
        if not metas:
            self.clear()
            if not quiet:
                warn("No parsed demos yet. Run `parse` first.")
            return False
        self.matches = pd.DataFrame(metas).sort_values("date")
        self.names = names
        empty = lambda: pd.DataFrame()
        self.rounds = pd.concat(parts["rounds"], ignore_index=True) if parts["rounds"] else empty()
        self.kills = pd.concat(parts["kills"], ignore_index=True) if parts["kills"] else empty()
        self.nades = pd.concat(parts["nades"], ignore_index=True) if parts["nades"] else empty()
        self.pos = pd.concat(parts["pos"], ignore_index=True) if parts["pos"] else empty()
        self.dmg = pd.concat(parts["dmg"], ignore_index=True) if parts["dmg"] else empty()
        self._canonical_names()
        self._enrich()
        self.loaded = True
        self.version = getattr(self, "version", 0) + 1
        if not quiet:
            good(f"Loaded {len(self.matches)} demo(s), {len(self.rounds)} rounds, "
                 f"{len(self.kills)} kills, {len(self.nades)} grenades.")
        return True

    # ---- derived columns ------------------------------------------------------
    def phase_of(self, t):
        a, b = [float(x) for x in (self.cfg["PHASE_SPLITS"] or "25,60").split(",")]
        t = np.asarray(t, dtype=float)
        return np.where(t <= a, "early", np.where(t <= b, "mid", "late"))

    def faceit_names(self):
        """SteamID64 -> FACEIT nickname, from FACEIT match rosters (match list) and the team roster (team.json)."""
        out = {}
        for r in self.ws.manifest():
            ids = [x for x in r.get("TeamSteamIds", "").split(";")]
            nk = [x for x in (r.get("TeamFaceitNicks") or "").split(";")]
            if r.get("TeamFaceitNicks") and len(ids) == len(nk):
                out.update({i: n for i, n in zip(ids, nk) if i and n})
        try:
            for m in json.loads((self.ws.team_root / "team.json").read_text()).get("members", []):
                if m.get("steamid") and m.get("nickname"):
                    out[str(m["steamid"])] = m["nickname"]
        except Exception:
            pass
        return out

    def _canonical_names(self):
        """One name per player (keyed by SteamID): his FACEIT nickname if known, else his most-used in-game name.
        Fixes stats splitting when someone renames between matches."""
        P = self.pos
        if P is None or not len(P):
            return
        self.ingame = P.groupby("steamid")["name"].agg(lambda s: s.mode().iloc[0]).to_dict()
        canon = dict(self.ingame)
        self.faceit = self.faceit_names() if str(self.cfg["PLAYER_NAMES"]).lower() != "ingame" else {}
        canon.update({sid: n for sid, n in self.faceit.items() if sid in canon})
        self.canon = canon
        P["name"] = P["steamid"].map(canon).fillna(P["name"])
        if self.nades is not None and len(self.nades):
            self.nades["name"] = self.nades["sid"].map(canon).fillna(self.nades["name"])
        if self.kills is not None and len(self.kills):
            self.kills["att_name"] = self.kills["att_sid"].map(canon).fillna(self.kills["att_name"])
            self.kills["vic_name"] = self.kills["vic_sid"].map(canon).fillna(self.kills["vic_name"])
        self.names = {sid: canon.get(sid, n) for sid, n in self.names.items()}

    def _enrich(self):
        R = self.rounds
        R["pistol"] = R["round"].isin([1, 13])
        R["buy"] = [buy_type(e, p) for e, p in zip(R["tgt_equip"], R["pistol"])]
        R["opp_buy"] = [buy_type(e, p) for e, p in zip(R["opp_equip"], R["pistol"])]
        R["planted"] = R["plant_tick"].notna()
        R["key"] = R["demo"] + "#" + R["round"].astype(str)
        side = R.set_index("key")["tgt_side"]
        for name in ("kills", "nades", "pos"):
            t = getattr(self, name)
            if len(t):
                t["key"] = t["demo"] + "#" + t["round"].astype(str)
                t["tgt_side"] = t["key"].map(side)
                t["phase"] = self.phase_of(t["t"])
                plant = R.set_index("key")["plant_tick"]
                tick_col = "land_tick" if name == "nades" else "tick"
                t["post_plant"] = t[tick_col] > t["key"].map(plant).fillna(np.inf)
        if len(self.dmg):
            self.dmg["key"] = self.dmg["demo"] + "#" + self.dmg["round"].astype(str)
            self.dmg["tgt_side"] = self.dmg["key"].map(side)
        self._build_places()
        R["site"] = [self.site_of(m, pl, x, y, z) if pk else ""
                     for m, pl, x, y, z, pk in zip(R["map"], R["plant_place"], R["plant_x"], R["plant_y"],
                                                   R["plant_z"], R["planted"])]
        self._t_targets()
        self._apply_fine_callouts()
        self._build_index()

    def fine_points(self):
        try:
            return json.loads((self.ws.root / FINE_CALLOUT_FILE).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def fine_names(self, maps, xs, ys, zs, fallback):
        """Your named spots: each position gets the nearest one whose circle it's inside, else keeps `fallback`."""
        pts = self.fine_points()
        out = np.asarray(fallback, dtype=object).copy()
        maps = np.asarray(maps)
        q = np.c_[np.asarray(xs, float), np.asarray(ys, float), np.asarray(zs, float) * 1.5]
        for m, lst in pts.items():
            if not lst:
                continue
            sel = np.where((maps == m) & ~np.isnan(q).any(axis=1))[0]
            if not len(sel):
                continue
            c = np.array([[p_["x"], p_["y"], p_["z"] * 1.5] for p_ in lst], float)
            r = np.array([p_.get("r", 250) for p_ in lst], float)
            names = np.array([p_["name"] for p_ in lst], dtype=object)
            for a0 in range(0, len(sel), 40000):  # in chunks, so big datasets don't eat memory
                ss = sel[a0:a0 + 40000]
                d = np.sqrt(((q[ss][:, None, :] - c[None, :, :]) ** 2).sum(-1))
                j = np.argmin(d / r[None, :], axis=1)  # nearest relative to each spot's size
                ok = d[np.arange(len(ss)), j] <= r[j]
                out[ss[ok]] = names[j[ok]]
        return out

    def _apply_fine_callouts(self):
        """Swap Valve's broad area names for your specific callouts wherever you've placed them."""
        if not self.fine_points():
            return
        P = self.pos
        if len(P):
            if "valve_place" not in P:
                P["valve_place"] = P["place"]
            P["place"] = self.fine_names(P["map"], P["x"], P["y"], P["z"], P["valve_place"])
        N = self.nades
        if N is not None and len(N):
            for xs, ys, zs, col in (("lx", "ly", "lz", "l_place"), ("tx", "ty", "tz", "t_place")):
                N[col] = self.fine_names(N["map"], N[xs], N[ys], N[zs], N[col])
        K = self.kills
        if K is not None and len(K):
            for xs, ys, zs, col in (("ax", "ay", "az", "a_place"), ("vx", "vy", "vz", "v_place")):
                K[col] = self.fine_names(K["map"], K[xs], K[ys], K[zs], K[col])
        if DISPLAY is not None:
            DISPLAY.learn([p_["name"] for lst in self.fine_points().values() for p_ in lst])

    def _build_places(self):
        """Nearest-callout lookup per map (from sampled player positions) + bombsite centroids."""
        self.trees = {}
        P = self.pos[self.pos["place"].astype(str) != ""]
        if DISPLAY is not None:
            DISPLAY.learn(P["place"].unique())
            for m_, g_ in P.groupby("map"):
                DISPLAY.learn(g_["place"].unique(), m_)
        for m, g in P.groupby("map"):
            g = g.sample(min(len(g), 40000), random_state=1) if len(g) > 40000 else g
            pts = g[["x", "y", "z"]].to_numpy(float) * np.array([1, 1, 1.5])
            self.trees[m] = (pts, g["place"].to_numpy())
            cents = {}
            for site in ("A", "B"):
                s = g[g["place"].str.lower().str.replace(" ", "").str.contains(f"bombsite{site.lower()}", regex=False)]
                if len(s):
                    cents[site] = s[["x", "y", "z"]].mean().to_numpy(float)
            if len(cents) < 2:
                pl = self.rounds[(self.rounds["map"] == m) & self.rounds["planted"]]
                for site in ("A", "B"):
                    s = pl[pl["plant_place"].str.upper().str.endswith(site)]
                    if site not in cents and len(s):
                        cents[site] = s[["plant_x", "plant_y", "plant_z"]].mean().to_numpy(float)
            self.zones[m] = cents
        for name, (xs, ys, zs, out) in (("nades", ("lx", "ly", "lz", "l_place")), ("nades", ("tx", "ty", "tz", "t_place"))):
            t = getattr(self, name)
            if len(t):
                t[out] = self.nearest_places(t["map"], t[xs], t[ys], t[zs])
        K = self.kills
        if len(K):
            for xs, ys, zs, out in (("ax", "ay", "az", "a_place"), ("vx", "vy", "vz", "v_place")):
                miss = K[out].fillna("") == ""
                if miss.any():
                    K.loc[miss, out] = self.nearest_places(K.loc[miss, "map"], K.loc[miss, xs], K.loc[miss, ys], K.loc[miss, zs])
        if len(self.pos):
            self.pos["zone"] = self.zones_of(self.pos["map"], self.pos["x"], self.pos["y"], self.pos["z"])

    def nearest_places(self, maps, xs, ys, zs):
        out = np.array([""] * len(xs), dtype=object)
        maps = np.asarray(maps)
        q = np.c_[np.asarray(xs, float), np.asarray(ys, float), np.asarray(zs, float) * 1.5]
        for m in set(maps):
            if m not in self.trees:
                continue
            pts, places = self.trees[m]
            sel = np.where((maps == m) & ~np.isnan(q).any(axis=1))[0]
            if not len(sel):
                continue
            try:
                from scipy.spatial import cKDTree
                _, idx = cKDTree(pts).query(q[sel])
            except Exception:
                idx = np.array([np.argmin(((pts - v) ** 2).sum(1)) for v in q[sel]])
            out[sel] = places[idx]
        return out

    def zones_of(self, maps, xs, ys, zs):
        maps = np.asarray(maps)
        out = np.array(["Mid"] * len(maps), dtype=object)
        P = np.c_[np.asarray(xs, float), np.asarray(ys, float), np.asarray(zs, float) * 1.5]
        for m, c in self.zones.items():
            if "A" not in c or "B" not in c:
                continue
            sel = maps == m
            a = c["A"] * np.array([1, 1, 1.5])
            b = c["B"] * np.array([1, 1, 1.5])
            da = np.linalg.norm(P[sel] - a, axis=1)
            db = np.linalg.norm(P[sel] - b, axis=1)
            z = np.where(da < 0.62 * db, "A", np.where(db < 0.62 * da, "B", "Mid"))
            out[sel] = z
        return out

    def site_of(self, m, place, x, y, z):
        p = str(place or "").lower().replace(" ", "")
        if "bombsitea" in p:
            return "A"
        if "bombsiteb" in p:
            return "B"
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "?"
        z_ = self.zones_of([m], [x], [y], [z])[0]
        return z_ if z_ in ("A", "B") else "?"

    def _t_targets(self):
        """For every round: which site/zone the T side went for (plant site, else where they ended up)."""
        R = self.rounds
        P = self.pos
        last = (P[P["alive"]].sort_values("t").groupby(["key", "side"]).tail(5))
        tz = last[last["side"] == "T"].groupby("key")["zone"].agg(lambda s: s.mode().iloc[0] if len(s) else "?")
        R["t_target"] = np.where(R["site"].isin(["A", "B"]), R["site"], R["key"].map(tz).fillna("?"))

    def _build_index(self):
        """One searchable table of grenades + kills, phrased from the scouted team's point of view."""
        rows = []
        N = self.nades
        if len(N):
            n = N[N["tgt"]].copy()
            rows.append(pd.DataFrame({
                "type": n["kind"], "map": n["map"], "side": n["tgt_side"], "phase": n["phase"],
                "round": n["round"], "t": n["t"].round(1), "player": n["name"], "from": n["t_place"],
                "place": n["l_place"], "detail": np.where(n["kind"] == "flash",
                                                          "blinded " + n["enemies_flashed"].astype(str), ""),
                "opponent": n["opponent"], "date": n["date"], "demo": n["demo"],
                "x": n["lx"], "y": n["ly"], "z": n["lz"], "fx": n["tx"], "fy": n["ty"], "fz": n["tz"]}))
        K = self.kills
        if len(K):
            for flag, typ, who, xyz, place in (("tgt_att", "kill", "att_name", ("ax", "ay", "az"), "a_place"),
                                               ("tgt_vic", "death", "vic_name", ("vx", "vy", "vz"), "v_place")):
                k = K[K[flag]].copy()
                t = np.where(k["opening"], "fk" if typ == "kill" else "fd", typ)
                other = k["vic_name"] if typ == "kill" else k["att_name"]
                rows.append(pd.DataFrame({
                    "type": t, "map": k["map"], "side": k["tgt_side"], "phase": k["phase"], "round": k["round"],
                    "t": k["t"].round(1), "player": k[who], "from": "", "place": k[place],
                    "detail": k["weapon"] + (" vs " if typ == "kill" else " by ") + other.astype(str),
                    "opponent": k["opponent"], "date": k["date"], "demo": k["demo"],
                    "x": k[xyz[0]], "y": k[xyz[1]], "z": k[xyz[2]], "fx": np.nan, "fy": np.nan, "fz": np.nan}))
        self.index = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
        if len(self.index):
            I = self.index
            pretty = lambda col: (pd.Series([show_place(m, v) for m, v in zip(I["map"], I[col].fillna(""))], index=I.index)
                                  .str.lower() if DISPLAY is not None else "")
            I["text"] = (pretty("place") + " " + pretty("from") + " " + I["type"] + " " + I["map"].str.replace("de_", "", regex=False) + " " + I["map"] + " "
                         + I["side"].fillna("").str.lower() + " " + I["phase"] + " " + I["player"].str.lower() + " "
                         + I["from"].fillna("").str.lower() + " " + I["place"].fillna("").str.lower() + " "
                         + I["detail"].fillna("").str.lower() + " " + I["opponent"].fillna("").str.lower())

    # ---- filters ----------------------------------------------------------------
    def demos_in_scope(self, **ov):
        cfg = lambda k: ov.get(k.lower(), self.cfg[k])
        M = self.matches
        if cfg("MAP"):
            M = M[M["map"] == cfg("MAP")]
        if cfg("OPPONENT"):
            M = M[M["opponent"].str.lower().str.contains(cfg("OPPONENT").lower(), regex=False)]
        n = ov.get("last_n", self.cfg.int("LAST_N", 0))
        if n:
            recent = M.drop_duplicates("match_id").sort_values("date").tail(int(n))["match_id"]
            M = M[M["match_id"].isin(recent)]
        if ov.get("roster", self.cfg["ROSTER"]) == "lineup":
            M = M[M["demo"].isin(self.main_lineup_demos())]
        se = season_set(ov.get("season", self.cfg["SEASON"]))
        if se:
            M = M[M["match_id"].map(self.match_seasons()).isin(se)]
        return set(M["demo"])

    def match_seasons(self):
        """match id -> ESEA season, from the match list's competition names."""
        return {r["MatchId"]: season_of(r.get("Competition")) for r in self.ws.manifest()}

    def f(self, table, side=True, phase=True, player=True, **ov):
        """Filtered copy of a table using MAP/SIDE/PHASE/PLAYER/LAST_N/OPPONENT (+ overrides)."""
        t = getattr(self, table)
        if t is None or len(t) == 0:
            return t if t is not None else pd.DataFrame()
        cfg = lambda k: ov.get(k.lower(), self.cfg[k])
        t = t[t["demo"].isin(self.demos_in_scope(**ov))]
        sd = cfg("SIDE")
        if side and sd and "tgt_side" in t:
            t = t[t["tgt_side"] == sd]
        ph = cfg("PHASE")
        if phase and ph and "phase" in t:
            t = t[t["phase"] == ph]
        pl = cfg("PLAYER")
        if player and pl:
            p = pl.lower()
            if table == "kills":
                t = t[(t["att_name"].str.lower() == p) | (t["vic_name"].str.lower() == p)]
            elif table in ("nades", "pos"):
                t = t[t["name"].str.lower() == p]
        out = set()
        if ov.get("roster", self.cfg["ROSTER"]) == "main":
            out = self.non_main_sids()
        if self.hide_dropped:
            out = out | self.dropped_sids()
        if True:
            if out:
                if table == "kills":
                    t = t[~((t["tgt_att"] & t["att_sid"].isin(out)) | (t["tgt_vic"] & t["vic_sid"].isin(out)))]
                elif table == "pos":
                    t = t[~(t["tgt"] & t["steamid"].isin(out))]
                elif table == "nades":
                    t = t[~(t["tgt"] & t["sid"].isin(out))]
                elif table == "dmg":
                    t = t[~t["att_sid"].isin(out)]
        return t

    # ---- roster status --------------------------------------------------------
    STATUSES = ("main", "sub", "dropped")
    STATUS_ALIASES = {"stand-in": "dropped", "standin": "dropped", "former": "dropped", "left": "dropped", "drop": "dropped"}
    TYPE_WORDS = {"FREE_AGENT": "free agent", "PERMANENT_FREE_AGENT": "permanent free agent", "SPECTATOR": "spectator",
                  "UNROSTERED_AGM": "AGM (unrostered)", "SIGNED": "signed"}

    def league_players(self, max_age_h=6):
        """SteamID -> {name, type, team} for every CSC player (public list, cached a few hours)."""
        path = self.ws.root / "league_players.json"
        try:
            if time.time() - path.stat().st_mtime < max_age_h * 3600:
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        try:
            ps = Csc(self.cfg["CSC_API"]).q("{ players { name steam64Id type team { name } } }").get("players") or []
            out = {str(p["steam64Id"]): {"name": p.get("name") or "", "type": p.get("type") or "",
                                         "team": (p.get("team") or {}).get("name", "")} for p in ps if p.get("steam64Id")}
            path.write_text(json.dumps(out), encoding="utf-8")
            return out
        except Exception:
            try:
                return json.loads(path.read_text(encoding="utf-8"))  # stale copy beats nothing
            except Exception:
                return {}

    def roster(self):
        """One row per scouted player: maps played, share, last played, whether he's still on the CSC roster, status.
        main / sub = on the team's CSC roster now; dropped = played for them in these demos but isn't on the roster now."""
        if getattr(self, "_roster", None) is not None and self._roster_ver == getattr(self, "version", 0):
            return self._roster
        if self.pos is None or self.matches is None:
            return pd.DataFrame()
        P = self.pos[self.pos["tgt"]]
        per = P.groupby(["demo", "steamid"])["key"].nunique().reset_index(name="rounds")
        per = per[per["rounds"] >= 5]  # played a real part of the map, not a reconnect blip
        dates = self.matches.set_index("demo")["date"]
        per["date"] = per["demo"].map(dates)
        order = self.matches.sort_values("date")["demo"].tolist()
        total = len(order)
        recent = set(order[-min(5, total):])
        names = P.groupby("steamid")["name"].agg(lambda s: s.mode().iloc[0])
        sid2nick = self.faceit_names()
        try:
            tm = json.loads((self.ws.team_root / "team.json").read_text())["members"]
            members = {m["nickname"].lower() for m in tm}
            member_sids = {str(m["steamid"]) for m in tm if m.get("steamid")}
        except Exception:
            members, member_sids = set(), set()
        try:
            overrides = json.loads((self.ws.team_root / "roster.json").read_text())
        except Exception:
            overrides = {}
        league = self.league_players() if members else {}
        my_team = ""
        try:
            my_team = json.loads((self.ws.team_root / "team.json").read_text()).get("name", "")
        except Exception:
            pass
        rows = []
        for sid, g in per.groupby("steamid"):
            n = g["demo"].nunique()
            share = n / max(1, total)
            rshare = g["demo"].isin(recent).sum() / max(1, len(recent))
            nick = sid2nick.get(sid, "")
            name = names.get(sid, sid)
            on_team = None
            if members:
                on_team = sid in member_sids or (nick.lower() in members) or (str(name).lower() in members)
            lp = league.get(str(sid), {})
            if on_team is False and lp.get("team", "").lower() == my_team.lower() and my_team:
                on_team = True  # roster changed since the team was looked up
            if on_team is False:
                auto = "dropped"
            elif share >= 0.5 or rshare >= 0.6:
                auto = "main"
            else:
                auto = "sub"
            if on_team is False:
                now = (lp.get("team") and f"on {lp['team']}") or self.TYPE_WORDS.get(lp.get("type", ""), lp.get("type", "").lower()) \
                    or ("not in CSC's player list" if league else "?")
            else:
                now = ""
            ov = overrides.get(sid) or overrides.get(str(name).lower())
            ov = self.STATUS_ALIASES.get(ov, ov)
            ing = getattr(self, "ingame", {}).get(sid, "")
            rows.append(dict(steamid=sid, player=name, **{"in-game name": ing if ing != name else ""},
                             maps=n, share=round(100 * share),
                             recent=f"{g['demo'].isin(recent).sum()}/{len(recent)}", last=g["date"].max(),
                             on_team={True: "yes", False: "no", None: "?"}[on_team], now=now,
                             status=ov or auto, auto=auto, override="yes" if ov else ""))
        R = pd.DataFrame(rows)
        if len(R):
            R["_o"] = R["status"].map({s: i for i, s in enumerate(self.STATUSES)})
            R = R.sort_values(["_o", "maps"], ascending=[True, False]).drop(columns="_o")
        self._roster, self._roster_ver = R, getattr(self, "version", 0)
        return R

    def status_of(self, name_or_sid):
        R = self.roster()
        if not len(R):
            return ""
        hit = R[(R["steamid"] == name_or_sid) | (R["player"].str.lower() == str(name_or_sid).lower())]
        return hit["status"].iloc[0] if len(hit) else ""

    def label(self, name):
        st = self.status_of(name)
        return f"{name} ({st})" if st and st != "main" else str(name)

    def non_main_sids(self):
        R = self.roster()
        return set(R[R["status"] != "main"]["steamid"]) if len(R) else set()

    hide_dropped = False  # set while a report is built (REPORT_HIDE_DROPPED): dropped players' data is left out

    def dropped_sids(self):
        R = self.roster()
        return set(R[R["status"] == "dropped"]["steamid"]) if len(R) else set()

    def main_lineup_demos(self):
        out = self.non_main_sids()
        P = self.pos[self.pos["tgt"]]
        per = P.groupby(["demo", "steamid"])["key"].nunique().reset_index(name="rounds")
        per = per[per["rounds"] >= 5]
        bad = set(per[per["steamid"].isin(out)]["demo"])
        return set(self.matches["demo"]) - bad

    def tgt_names(self, all_players=False):
        if self.pos is None or not len(self.pos):
            return []
        P = self.pos[self.pos["tgt"]]
        if not all_players and self.cfg["ROSTER"] in ("main", "lineup"):
            P = P[~P["steamid"].isin(self.non_main_sids())]
        if self.hide_dropped:
            P = P[~P["steamid"].isin(self.dropped_sids())]
        names = P.groupby("steamid")["name"].agg(lambda s: s.mode().iloc[0])
        R = self.roster()
        if len(R):  # main roster first, then subs, then dropped players
            rank = {sid: i for i, sid in enumerate(R["steamid"])}
            return [names[s] for s in sorted(names.index, key=lambda s: rank.get(s, 999))]
        return names.tolist()


def buy_type(equip, pistol):
    if pistol:
        return "pistol"
    if equip >= 18000:
        return "full"
    if equip >= 9000:
        return "force"
    if equip >= 4500:
        return "half-eco"
    return "eco"


# =============================================================================
#  Map radars & plotting
# =============================================================================
# pos_x, pos_y, scale, lower-level z cut-off (from the game's radar overview files)
MAP_DATA = {
    "de_ancient": (-2953, 2164, 5.0, None),
    "de_anubis": (-2796, 3328, 5.22, None),
    "de_dust2": (-2476, 3239, 4.4, None),
    "de_inferno": (-2087, 3870, 4.9, None),
    "de_mirage": (-3230, 1713, 5.0, None),
    "de_nuke": (-3453, 2887, 7.0, -495),
    "de_overpass": (-4831, 1781, 5.2, None),
    "de_train": (-2308, 2078, 4.082077, None),
    "de_vertigo": (-3168, 1762, 4.0, 11700),
}
SIDE_COL = {"T": "#ff4d4d", "CT": "#4dabf7"}   # T red, CT blue in every side-based map
NADE_MARK = {"smoke": "o", "flash": "^", "molotov": "s", "he": "D"}
COL = {"fk": "#3ddc84", "fd": "#ff4d4d", "kill": "#3ddc84", "death": "#ff4d4d", "smoke": "#e8eef2",
       "flash": "#ffd43b", "molotov": "#ff7b29", "he": "#c77dff", "pos": "#4dabf7"}


RADAR_REPO = os.environ.get("ESEA_RADAR_REPO", "https://github.com/pnxenopoulos/awpy-data")


class Radar:
    """Official CS2 radar images (the in-game minimap) + the world->pixel transform for each map.

    Downloaded automatically, once, from the public awpy-data releases (extracted from the game files)
    into <workspace>/radars. You can also drop your own <map>.png / <map>_lower.png there.
    """

    def __init__(self, ws: Workspace, cfg=None):
        self.ws, self.cfg = ws, cfg
        self.dir = ws.root / "radars"
        self.dirs = [self.dir, SCRIPT_DIR / "radars", Path.home() / ".awpy"]
        self.meta = dict(MAP_DATA)
        self._imgs = {}
        self._tried = False
        self._load_meta()

    # ---- coordinate tables ----------------------------------------------------
    def _load_meta(self):
        for d in self.dirs:
            if not d.exists():
                continue
            for f in list(d.rglob("map_data.json")) + list(d.rglob("map-data.json")):
                try:
                    data = json.loads(f.read_text())
                except Exception:
                    continue
                for m, v in data.items():
                    try:
                        cut = lower_cut_from(v)
                        old = self.meta.get(m, (0, 0, 0, None))
                        self.meta[m] = (float(v["pos_x"]), float(v["pos_y"]), float(v["scale"]),
                                        cut if cut is not None else old[3])
                    except (KeyError, TypeError, ValueError):
                        pass

    # ---- images ---------------------------------------------------------------
    def find(self, m, lower=False):
        fname = f"{m}_lower.png" if lower else f"{m}.png"
        for d in self.dirs:
            if not d.exists():
                continue
            if (d / fname).exists():
                return d / fname
            hits = list(d.rglob(fname))
            if hits:
                return max(hits, key=lambda p: p.stat().st_mtime)
        return None

    def image(self, m, lower=False):
        key = (m, lower)
        if key not in self._imgs:
            f = self.find(m, lower)
            if f is None and not lower and self.auto():
                self.download(quiet=False)
                f = self.find(m, lower)
            try:
                self._imgs[key] = plt.imread(str(f)) if f else None
            except Exception:
                self._imgs[key] = None
        return self._imgs[key]

    def auto(self):
        return (self.cfg is None or str(self.cfg["RADARS"]).lower() != "off") and not self._tried

    def download(self, quiet=True, force=False):
        """Fetch images.zip + map_data.json from the newest awpy-data release. Returns list of maps."""
        import tempfile
        import zipfile
        self._tried = True
        if not force and any(self.dir.glob("*.png")):
            return sorted(p.stem for p in self.dir.glob("*.png"))
        info("Downloading the CS2 radar images (one time)...")
        try:
            tag = latest_release_tag(RADAR_REPO)
            base = f"{RADAR_REPO}/releases/download/{tag}"
            self.dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as td:
                z = Path(td) / "images.zip"
                http_download(f"{base}/images.zip", z, "radars")
                n = 0
                with zipfile.ZipFile(z) as zf:
                    for name in zf.namelist():
                        if name.lower().endswith(".png"):
                            data = zf.read(name)
                            (self.dir / Path(name).name).write_bytes(data)
                            n += 1
            http_download(f"{base}/map_data.json", self.dir / "map_data.json", "map data")
            (self.dir / "VERSION").write_text(str(tag))
        except Exception as e:
            warn(f"Couldn't download radar images ({e}). Heatmaps will use a sketched map instead.\n"
                 f"    Try again later with `radars`, or drop <map>.png radar images into {self.dir}")
            return []
        self._imgs.clear()
        self._load_meta()
        maps = sorted(p.stem for p in self.dir.glob("*.png"))
        good(f"Radar images ready ({n} files, CS2 build {tag}) in {self.dir}")
        return maps

    def to_px(self, m, x, y):
        px, py, sc, _ = self.meta[m]
        return (np.asarray(x, float) - px) / sc, (py - np.asarray(y, float)) / sc

    def lower_cut(self, m):
        return self.meta.get(m, (0, 0, 0, None))[3]


def lower_cut_from(v):
    """Z height that splits the upper and lower radar on multi-level maps (Nuke, Vertigo, Train)."""
    if v.get("lower_level_max_units") is not None and v["lower_level_max_units"] > -1000000:
        return float(v["lower_level_max_units"])
    vs = v.get("verticalsections")
    if not isinstance(vs, dict):
        return None
    for name, sec in vs.items():
        if "lower" in str(name).lower() and isinstance(sec, dict):
            for k, val in sec.items():
                if k.lower() == "altitudemax":
                    return float(val)
    for name, sec in vs.items():
        if "default" in str(name).lower() and isinstance(sec, dict):
            for k, val in sec.items():
                if k.lower() == "altitudemin":
                    return float(val)
    return None


def latest_release_tag(repo_url):
    """Newest release tag; uses the /releases/latest redirect (no API rate limit), API as fallback."""
    req = urllib.request.Request(f"{repo_url}/releases/latest", headers={"User-Agent": "esea-analyst"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            m = re.search(r"/releases/tag/([^/?#]+)", r.geturl())
            if m:
                return urllib.parse.unquote(m.group(1))
    except Exception:
        pass
    api = repo_url.replace("https://github.com/", "https://api.github.com/repos/")
    req = urllib.request.Request(f"{api}/releases/latest",
                                 headers={"User-Agent": "esea-analyst", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["tag_name"]


def solid_cmap(color):
    """Transparent -> solid single-colour heat (so FK heat is green and FD heat is red)."""
    from matplotlib.colors import LinearSegmentedColormap, to_rgb
    r, g, b = to_rgb(color)
    return LinearSegmentedColormap.from_list("solid", [(r, g, b, 0.0), (r, g, b, 0.55), (r, g, b, 0.9)])


def smooth2d(h, sigma=2.2):
    try:
        from scipy.ndimage import gaussian_filter
        return gaussian_filter(h, sigma)
    except Exception:
        r = int(3 * sigma)
        k = np.exp(-np.arange(-r, r + 1) ** 2 / (2 * sigma ** 2))
        k /= k.sum()
        h = np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 0, h)
        return np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 1, h)


def map_plot(radar: Radar, m, title, out: Path, layers=(), density=None, arrows=(), labels=(), style="both",
             subtitle="", backdrop=None, densities=(), paths=()):
    """layers: [dict(x,y,z,color,label,size)], density: (x,y,z) arrays, arrows: [(fx,fy,fz,x,y,z,color)],
    labels: [(x,y,z,text)]"""
    radar.image(m)  # fetches the radar images on first use (RADARS auto)
    if backdrop is None and getattr(radar, "backdrop", None):
        backdrop = radar.backdrop(m)
    cut = radar.lower_cut(m)
    allz = np.concatenate([np.asarray(L["z"], float) for L in layers] + ([np.asarray(density[2], float)] if density is not None else []) or [np.array([])])
    levels = [("upper", None)]
    if cut is not None and len(allz) and np.nanmin(allz) < cut:
        levels = [("upper", lambda z: z >= cut), ("lower", lambda z: z < cut)]
    fig, axes = plt.subplots(1, len(levels), figsize=(9 * len(levels), 9.4), facecolor="#111418")
    axes = np.atleast_1d(axes)
    has_meta = m in radar.meta
    for ax, (lvl, sel) in zip(axes, levels):
        ax.set_facecolor("#111418")
        img = radar.image(m, lower=(lvl == "lower")) if has_meta else None
        use_px = img is not None or has_meta
        if img is not None:
            ax.imshow(img, extent=(0, 1024, 1024, 0), alpha=0.9)
        elif backdrop is not None:  # no radar: sketch the map from where players have stood
            bx, by, bz, bplaces = backdrop
            keep = np.ones(len(bx), bool)
            if sel is not None:
                keep = np.nan_to_num(sel(np.asarray(bz, float))).astype(bool)
            bx_, by_ = np.asarray(bx, float)[keep], np.asarray(by, float)[keep]
            if use_px:
                bx_, by_ = radar.to_px(m, bx_, by_)
            ax.scatter(bx_, by_, s=2, c="#3a434d", alpha=0.35, linewidths=0, zorder=0)
            for (cx, cy, cz, name) in bplaces:
                if sel is not None and not bool(sel(np.array([cz]))[0]):
                    continue
                px_, py_ = radar.to_px(m, [cx], [cy]) if use_px else ([cx], [cy])
                ax.text(px_[0], py_[0], name, color="#6f7b86", fontsize=7, ha="center", va="center", zorder=1)

        def tr(x, y, z):
            x, y, z = np.asarray(x, float), np.asarray(y, float), np.asarray(z, float)
            keep = ~np.isnan(x) & ~np.isnan(y)
            if sel is not None:
                keep &= np.nan_to_num(sel(z), nan=False).astype(bool) if lvl == "upper" else np.nan_to_num(sel(z)).astype(bool)
            x, y = x[keep], y[keep]
            if use_px:
                x, y = radar.to_px(m, x, y)
            return x, y, keep

        dens_list = [(density, "inferno")] if density is not None else []
        dens_list += [(d, solid_cmap(c)) for d, c in densities]
        if style in ("density", "both"):
            for dens, cmap in dens_list:
                dx, dy, _ = tr(*dens)
                if len(dx) < 3:
                    continue
                rng = [[0, 1024], [0, 1024]] if use_px else [[np.min(dx) - 300, np.max(dx) + 300], [np.min(dy) - 300, np.max(dy) + 300]]
                H, xe, ye = np.histogram2d(dx, dy, bins=160, range=rng)
                H = smooth2d(H.T)
                if H.max() > 0:
                    H = np.ma.masked_less(H / H.max(), 0.04)
                    ax.imshow(H, extent=(xe[0], xe[-1], ye[-1], ye[0]) if use_px else (xe[0], xe[-1], ye[0], ye[-1]),
                              origin="upper" if use_px else "lower", cmap=cmap, alpha=0.72 if cmap == "inferno" else 1.0,
                              interpolation="bilinear")
        for pth in paths:  # movement lines, e.g. rotations: dict(x, y, z, color)
            px_, py_, _ = tr(pth["x"], pth["y"], pth["z"])
            if len(px_) >= 2:
                ax.plot(px_, py_, color=pth["color"], lw=pth.get("lw", 1.5), alpha=pth.get("alpha", 0.55), zorder=2,
                        solid_capstyle="round")
                ax.annotate("", xy=(px_[-1], py_[-1]), xytext=(px_[-2], py_[-2]),
                            arrowprops=dict(arrowstyle="-|>", color=pth["color"], lw=1.2, alpha=0.8), zorder=2)
        for a in arrows:
            fx, fy, k1 = tr([a[0]], [a[1]], [a[2]])
            x, y, k2 = tr([a[3]], [a[4]], [a[5]])
            if len(fx) and len(x):
                ax.annotate("", xy=(x[0], y[0]), xytext=(fx[0], fy[0]),
                            arrowprops=dict(arrowstyle="->", color=a[6], lw=0.9, alpha=0.6))
                ax.scatter(fx, fy, s=14, c="none", edgecolors=a[6], linewidths=0.9, alpha=0.7, zorder=2)
        if style in ("points", "both") or density is None:
            for L in layers:
                x, y, _ = tr(L["x"], L["y"], L["z"])
                if len(x):
                    ax.scatter(x, y, s=L.get("size", 26), c=L["color"], edgecolors="black" if L.get("size", 26) > 8 else "none",
                               linewidths=0.5, alpha=L.get("alpha", 0.85),
                               label=f"{L['label']} ({len(x)})" if L.get("count", True) else L["label"], zorder=3,
                               marker=L.get("marker", "o"))
        for lx_, ly_, lz_, text in labels:
            x, y, _ = tr([lx_], [ly_], [lz_])
            if len(x):
                ax.text(x[0], y[0] - 14, text, color="white", fontsize=8, ha="center", zorder=4,
                        bbox=dict(boxstyle="round,pad=0.2", fc="#000000aa", ec="none"))
        if use_px:
            ax.set_xlim(0, 1024)
            ax.set_ylim(1024, 0)
        else:
            ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_visible(False)
        if len(levels) > 1:
            ax.set_title(lvl, color="#9aa4ad", fontsize=10)
        handles, _ = ax.get_legend_handles_labels()
        if handles:
            leg = ax.legend(loc="lower left", fontsize=8, facecolor="#1b1f24", edgecolor="#333", labelcolor="white")
            leg.set_zorder(5)
            for h in getattr(leg, "legend_handles", None) or getattr(leg, "legendHandles", []):
                try:  # legend icons always solid and readable, even for faint sample dots
                    h.set_alpha(1)
                    h.set_sizes([60])
                except Exception:
                    pass
    fig.suptitle(title, color="white", fontsize=13, y=0.985)
    if subtitle:
        fig.text(0.5, 0.945, subtitle, color="#9aa4ad", fontsize=9, ha="center")
    if not any(radar.image(m, lower=(lvl == "lower")) is not None for lvl, _ in levels):
        fig.text(0.5, 0.012, "map sketched from player positions - run `radars` for the real radar image",
                 color="#6b7680", fontsize=8, ha="center")
    fig.tight_layout(rect=(0, 0.02, 1, 0.94))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out


# =============================================================================
#  Analytics
# =============================================================================
class View:
    def __init__(self, key, title):
        self.key, self.title = key, title
        self.tables = []      # (subtitle, DataFrame)
        self.notes = []       # strings
        self.plots = []       # (label, fn(out_dir) -> [paths])

    def table(self, sub, df):
        self.tables.append((sub, df))
        return self

    def note(self, s):
        self.notes.append(s)
        return self


def pct(a, b):
    return round(100.0 * a / b, 1) if b else float("nan")


def fmt_pct(v):
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.0f}%"


class Analyst:
    def __init__(self, ds: Dataset, cfg: Config, radar: Radar):
        self.ds, self.cfg = ds, cfg
        self._bd = {}
        self.radar = radar

    @property
    def radar(self):
        return self._radar

    @radar.setter
    def radar(self, r):
        self._radar = r
        r.backdrop = self.backdrop

    def backdrop(self, m):
        """Faint map outline (all sampled player positions) + callout names, used when there is no radar image."""
        ver = getattr(self.ds, "version", 0)
        if (m, ver) in self._bd:
            return self._bd[(m, ver)]
        P = self.ds.pos
        if P is None or not len(P):
            return None
        g = P[P["map"] == m]
        if not len(g):
            return None
        smp = g.sample(min(len(g), 60000), random_state=2)
        places = []
        for name, q in g[g["place"] != ""].groupby("place"):
            places.append((q["x"].median(), q["y"].median(), q["z"].median(), name))
        self._bd[(m, ver)] = (smp["x"].to_numpy(), smp["y"].to_numpy(), smp["z"].to_numpy(), places)
        return self._bd[(m, ver)]

    @property
    def min_n(self):
        return max(1, self.cfg.int("MIN_SAMPLES", 4))

    def maps_in_scope(self):
        R = self.ds.f("rounds", side=False)
        return sorted(R["map"].unique()) if len(R) else []

    def scope_label(self):
        bits = [self.cfg[k] for k in ("MAP", "SIDE", "PHASE", "PLAYER") if self.cfg[k]]
        if self.cfg["LAST_N"]:
            bits.append(f"last {self.cfg['LAST_N']} matches")
        if self.cfg["OPPONENT"]:
            bits.append("vs " + self.cfg["OPPONENT"])
        if self.cfg["SEASON"]:
            bits.append("S" + self.cfg["SEASON"].replace(",", "+S"))
        if self.cfg["ROSTER"] == "main":
            bits.append("main roster only")
        elif self.cfg["ROSTER"] == "lineup":
            bits.append("main-5 maps only")
        return " | ".join(bits) if bits else "all data"

    # ------------------------------------------------------------------ views
    def v_overview(self):
        v = View("overview", "Overview")
        R = self.ds.f("rounds", side=False)
        rows = []
        for m, g in R.groupby("map"):
            t, ct = g[g["tgt_side"] == "T"], g[g["tgt_side"] == "CT"]
            pis = g[g["pistol"]]
            pl = t[t["planted"]]
            K = self.ds.f("kills", side=False, phase=False, player=False)
            K = K[(K["map"] == m) & K["opening"]]
            ow = int(K["tgt_att"].sum())
            od = int(K["tgt_vic"].sum())
            rows.append({"map": m, "matches": g["demo"].nunique(), "rounds": len(g),
                         "win%": pct(g["tgt_won"].sum(), len(g)), "T win%": pct(t["tgt_won"].sum(), len(t)),
                         "CT win%": pct(ct["tgt_won"].sum(), len(ct)),
                         "pistols": f"{int(pis['tgt_won'].sum())}-{int((~pis['tgt_won']).sum())}",
                         "T plants A/B": f"{(pl['site'] == 'A').sum()}/{(pl['site'] == 'B').sum()}",
                         "plant t (med)": round(pl["plant_t"].median(), 0) if len(pl) else None,
                         "opening win%": pct(ow, ow + od)})
        v.table("Per map", pd.DataFrame(rows))
        M = self.ds.matches
        M = M[M["demo"].isin(self.ds.demos_in_scope())]
        sc = []
        for _, mm in M.iterrows():
            g = self.ds.rounds[self.ds.rounds["demo"] == mm["demo"]]
            sc.append({"date": mm["date"], "opponent": mm["opponent"], "map": mm["map"],
                       "score": f"{int(g['tgt_won'].sum())}-{int((~g['tgt_won']).sum())}"})
        v.table("Matches", pd.DataFrame(sc).sort_values("date", ascending=False) if sc else pd.DataFrame())
        return v

    def v_openings(self):
        v = View("openings", "Opening duels (FK / FD)")
        K = self.ds.f("kills", phase=True)
        K = K[K["opening"]] if len(K) else K
        R = self.ds.f("rounds")
        rows = []
        for n in self.ds.tgt_names():
            fk = K[K["tgt_att"] & (K["att_name"] == n)]
            fd = K[K["tgt_vic"] & (K["vic_name"] == n)]
            played = self.ds.f("pos")
            played = played[played["name"] == n]["key"].nunique()
            if len(fk) + len(fd) == 0:
                continue
            rows.append({"player": n, "roster": self.ds.status_of(n), "rounds": played, "FK": len(fk), "FD": len(fd),
                         "attempt%": pct(len(fk) + len(fd), played), "win%": pct(len(fk), len(fk) + len(fd)),
                         "top FK spot": top1(fk["a_place"]), "top FD spot": top1(fd["v_place"])})
        v.table("Per player", pd.DataFrame(rows).sort_values("attempt%", ascending=False) if rows else pd.DataFrame())
        for flag, place, lbl in (("tgt_att", "a_place", "Where they WIN opening duels"),
                                 ("tgt_vic", "v_place", "Where they LOSE opening duels")):
            k = K[K[flag]]
            if len(k):
                t = (k.groupby(["map", "tgt_side", place]).agg(n=("tick", "size"), t_med=("t", "median"),
                                                              players=("att_name" if flag == "tgt_att" else "vic_name",
                                                                       lambda s: ", ".join(s.value_counts().index[:3])))
                     .reset_index().sort_values("n", ascending=False).rename(columns={place: "place", "tgt_side": "side"}))
                t["t_med"] = t["t_med"].round(0)
                v.table(lbl, t)
        tot_fk, tot_fd = int(K["tgt_att"].sum()), int(K["tgt_vic"].sum())
        v.note(f"Opening duels in scope: {tot_fk} won, {tot_fd} lost ({fmt_pct(pct(tot_fk, tot_fk + tot_fd))}) "
               f"over {len(R)} rounds.")

        def plot(out):
            paths = []
            for m in self.maps_in_scope():
                k = K[K["map"] == m]
                fk, fd = k[k["tgt_att"]], k[k["tgt_vic"]]
                if not len(fk) and not len(fd):
                    continue
                dens = (pd.concat([fk["ax"], fd["vx"]]), pd.concat([fk["ay"], fd["vy"]]), pd.concat([fk["az"], fd["vz"]]))
                paths.append(map_plot(self.radar, m, f"{m} - opening kills (green) / opening deaths (red)",
                                      out / f"fkfd_{m}_{self.fname()}.png",
                                      layers=[dict(x=fk["ax"], y=fk["ay"], z=fk["az"], color=COL["fk"], label="FK - opening kill"),
                                              dict(x=fd["vx"], y=fd["vy"], z=fd["vz"], color=COL["fd"], label="FD - opening death",
                                                   marker="X", size=44)],
                                      densities=[((fk["ax"], fk["ay"], fk["az"]), COL["fk"]),
                                                 ((fd["vx"], fd["vy"], fd["vz"]), COL["fd"])],
                                      style=self.cfg["HEATMAP_STYLE"], subtitle=self.scope_label()))
            return paths
        v.plots.append(("FK/FD heatmap", plot))
        return v

    def v_players(self):
        v = View("players", "Player profiles")
        K = self.ds.f("kills", player=False)
        D = self.ds.f("dmg", phase=False, player=False)
        N = self.ds.f("nades", player=False)
        P = self.ds.f("pos", phase=False, player=False)
        rows = []
        for n in self.ds.tgt_names():
            sid = P[P["name"] == n]["steamid"]
            sid = sid.iloc[0] if len(sid) else ""
            rounds = P[P["name"] == n]["key"].nunique()
            if not rounds:
                continue
            kills = K[K["tgt_att"] & (K["att_name"] == n)]
            deaths = K[K["tgt_vic"] & (K["vic_name"] == n)]
            nn = N[N["tgt"] & (N["name"] == n)]
            fl = nn[nn["kind"] == "flash"]
            dmg = D[D["att_sid"] == sid]["dmg"].sum() if len(D) else 0
            rows.append({"player": n, "roster": self.ds.status_of(n), "rounds": rounds, "K": len(kills), "D": len(deaths),
                         "K/D": round(len(kills) / max(1, len(deaths)), 2), "ADR": round(dmg / rounds, 1),
                         "HS%": pct(kills["headshot"].sum(), len(kills)),
                         "FK": int(kills["opening"].sum()), "FD": int(deaths["opening"].sum()),
                         "traded deaths%": pct(deaths["traded"].sum(), len(deaths)),
                         "nades/rnd": round(len(nn) / rounds, 2),
                         "flash blinds/flash": round(fl["enemies_flashed"].sum() / max(1, len(fl)), 2),
                         "team-flashes": int(fl["team_flashed"].sum())})
        v.table("Scouted players (filters: map/side/phase apply)", pd.DataFrame(rows))
        # roles from positions
        roles = self.roles()
        if len(roles):
            v.table("Where each player plays (most common zone/callout, mid-round)", roles)
        return v

    def roles(self):
        P = self.ds.f("pos", phase=False, player=False)
        P = P[P["tgt"] & P["alive"] & (P["t"].between(15, 40))]
        rows = []
        for (n, side), g in P.groupby(["name", "tgt_side"]):
            per_round = g.groupby("key").agg(zone=("zone", lambda s: s.mode().iloc[0]),
                                             place=("place", lambda s: s.mode().iloc[0]))
            if len(per_round) < self.min_n:
                continue
            z = per_round["zone"].value_counts(normalize=True)
            p = per_round["place"].value_counts(normalize=True)
            rows.append({"player": n, "roster": self.ds.status_of(n), "side": side, "rounds": len(per_round), "zone": z.index[0],
                         "zone%": round(100 * z.iloc[0]), "callout": p.index[0], "callout%": round(100 * p.iloc[0])})
        return pd.DataFrame(rows).sort_values(["side", "player"]) if rows else pd.DataFrame()

    def v_utility(self, kind):
        names = {"smoke": "Smokes", "flash": "Flashes", "molotov": "Molotovs / incendiaries", "he": "HE grenades"}
        v = View(kind, names[kind])
        N = self.ds.f("nades")
        N = N[N["tgt"] & (N["kind"] == kind)] if len(N) else N
        R = self.ds.f("rounds")
        if not len(N):
            v.note("No grenades of this type in scope.")
            return v
        nr = R.groupby("tgt_side").size().to_dict()
        g = (N.groupby(["map", "tgt_side", "l_place"])
             .agg(n=("round", "size"), rounds=("key", "nunique"), t_med=("t", "median"),
                  throwers=("name", lambda s: ", ".join(s.value_counts().index[:3])))
             .reset_index().rename(columns={"tgt_side": "side", "l_place": "lands at"}))
        g["% of rounds"] = [pct(r, len(R[(R["map"] == m) & (R["tgt_side"] == s)])) for m, s, r in
                            zip(g["map"], g["side"], g["rounds"])]
        g["t_med"] = g["t_med"].round(0)
        v.table("Landing spots", g.sort_values("n", ascending=False)[["map", "side", "lands at", "n", "% of rounds", "t_med", "throwers"]])
        L = self.lineups(N)
        if len(L):
            v.table("Lineups (thrown from -> lands at)", L)
        per = (N.groupby(["name", "tgt_side"]).size().unstack(fill_value=0)) if len(N) else pd.DataFrame()
        if len(per):
            per = per.reset_index().rename(columns={"name": "player"})
            v.table("Per player", per)
        ph = N.groupby(["tgt_side", "phase"]).size().unstack(fill_value=0).reset_index().rename(columns={"tgt_side": "side"})
        v.table("By round phase", ph)
        if kind == "flash":
            fl = N.groupby("name").agg(flashes=("round", "size"), enemies_blinded=("enemies_flashed", "sum"),
                                       team_flashed=("team_flashed", "sum")).reset_index()
            fl["blinds/flash"] = (fl["enemies_blinded"] / fl["flashes"]).round(2)
            v.table("Flash effectiveness (enemies blinded >= 1 s)", fl.rename(columns={"name": "player"}))
        v.note(f"{len(N)} {names[kind].lower()} thrown by the scouted team in {sum(nr.values())} rounds in scope.")

        def plot(out):
            paths = []
            for m in self.maps_in_scope():
                n = N[N["map"] == m]
                if not len(n):
                    continue
                top = self.lineups(n, top=12)
                arrows = []
                if len(top):
                    for _, r in top.iterrows():
                        s = n[(n["t_place"] == r["from"]) & (n["l_place"] == r["lands at"])]
                        arrows.append((s["tx"].median(), s["ty"].median(), s["tz"].median(),
                                       s["lx"].median(), s["ly"].median(), s["lz"].median(),
                                       SIDE_COL.get(r["side"], COL[kind])))
                lab = []
                for place, s in n.groupby("l_place"):
                    if len(s) >= max(2, self.min_n):
                        lab.append((s["lx"].median(), s["ly"].median(), s["lz"].median(), f"{show_place(m, place)} ({len(s)})"))
                layers, dens = [], []
                for side in ("T", "CT"):
                    s = n[n["tgt_side"] == side]
                    if len(s):
                        layers.append(dict(x=s["lx"], y=s["ly"], z=s["lz"], color=SIDE_COL[side], label=f"{side} side",
                                           marker=NADE_MARK.get(kind, "o"), size=34 if kind == "smoke" else 26))
                        dens.append(((s["lx"], s["ly"], s["lz"]), SIDE_COL[side]))
                paths.append(map_plot(self.radar, m, f"{m} - {names[kind].lower()} landing spots",
                                      out / f"{kind}_{m}_{self.fname()}.png", layers=layers,
                                      densities=dens, arrows=arrows, labels=lab,
                                      style=self.cfg["HEATMAP_STYLE"], subtitle=self.scope_label() + "  |  arrows = common lineups"))
            return paths
        v.plots.append((f"{kind} map", plot))
        return v

    def lineups(self, N, top=25):
        n = N[N["t_place"].astype(str) != ""]
        if not len(n):
            return pd.DataFrame()
        L = (n.groupby(["map", "tgt_side", "t_place", "l_place"])
             .agg(n=("round", "size"), t_med=("t", "median"), throwers=("name", lambda s: ", ".join(s.value_counts().index[:2])))
             .reset_index().rename(columns={"tgt_side": "side", "t_place": "from", "l_place": "lands at"}))
        L = L[L["n"] >= 2].sort_values("n", ascending=False).head(top)
        L["t_med"] = L["t_med"].round(0)
        return L

    def v_positions(self, phase):
        v = View(f"pos_{phase}", f"Positioning - {phase} round")
        P = self.ds.f("pos", phase=False)
        P = P[P["tgt"] & P["alive"] & (P["phase"] == phase)] if len(P) else P
        if not len(P):
            v.note("No position data in scope.")
            return v
        rows = []
        for (m, side), g in P.groupby(["map", "tgt_side"]):
            nr = g["key"].nunique()
            occ = g.groupby("place").agg(rounds=("key", "nunique"),
                                        players=("name", lambda s: ", ".join(s.value_counts().index[:2])))
            avg = g.groupby(["place", "key"])["steamid"].nunique().groupby("place").mean()
            for place, r in occ.iterrows():
                if not place:
                    continue
                rows.append({"map": m, "side": side, "callout": place, "% of rounds": pct(r["rounds"], nr),
                             "avg players": round(avg.get(place, 0), 1), "usually": r["players"]})
        T = pd.DataFrame(rows).sort_values(["map", "side", "% of rounds"], ascending=[True, True, False])
        v.table(f"Callout presence during {phase} round (scouted team, alive players)", T)
        zone = (P.groupby(["map", "tgt_side", "key", "zone"])["steamid"].nunique().groupby(["map", "tgt_side", "zone"]).mean()
                .unstack(fill_value=0).round(1).reset_index().rename(columns={"tgt_side": "side"}))
        v.table("Average players per zone", zone)

        def plot(out):
            paths = []
            for m in self.maps_in_scope():
                for side in ([self.cfg["SIDE"]] if self.cfg["SIDE"] else ["T", "CT"]):
                    g = P[(P["map"] == m) & (P["tgt_side"] == side)]
                    if len(g) < 5:
                        continue
                    lab = []
                    tops = g.groupby("place")["key"].nunique().sort_values(ascending=False).head(6)
                    nr = g["key"].nunique()
                    for place, c in tops.items():
                        s = g[g["place"] == place]
                        if place:
                            lab.append((s["x"].median(), s["y"].median(), s["z"].median(), f"{show_place(m, place)} {pct(c, nr):.0f}%"))
                    paths.append(map_plot(self.radar, m, f"{m} - {side} side positions, {phase} round",
                                          out / f"pos_{phase}_{side}_{m}_{self.fname()}.png",
                                          densities=[((g["x"], g["y"], g["z"]), SIDE_COL[side])], labels=lab,
                                          layers=[dict(x=g["x"], y=g["y"], z=g["z"], color=SIDE_COL[side], size=3, alpha=0.18,
                                                       label=f"{side} side ({nr} rounds)", count=False)],
                                          style="both", subtitle=self.scope_label() + "  |  % = rounds with someone there"))
            return paths
        v.plots.append((f"{phase} positions", plot))
        return v

    def v_postplant(self):
        """Where they set up after planting (T) and where they retake from (CT), per site."""
        v = View("postplant", "Post-plant setups & retakes")
        R = self.ds.f("rounds", side=True)
        R = R[R["planted"] & R["site"].isin(["A", "B"])] if len(R) else R
        if not len(R):
            v.note("No planted rounds in scope.")
            return v
        info_ = R.set_index("key")
        P = self.ds.f("pos", phase=False)
        P = P[P["tgt"] & P["alive"] & P["key"].isin(R["key"])].copy()
        P["since"] = P["t"] - P["key"].map(info_["plant_t"])
        P = P[P["since"].between(2, 30)]  # settled into the post-plant / retake, before it resolves
        P["site"] = P["key"].map(info_["site"])
        label = {"T": "post-plant (they planted)", "CT": "retake (opponent planted)"}
        if not len(P):
            v.note("No position data after plants in scope.")
            return v
        # results
        res = (R.groupby(["map", "tgt_side", "site"]).agg(rounds=("tgt_won", "size"), won=("tgt_won", "mean"),
                                                        plant=("plant_t", "median")).reset_index())
        res["win%"] = (100 * res.pop("won")).round(0)
        res["median plant (s)"] = res.pop("plant").round(0)
        res["situation"] = res["tgt_side"].map(label)
        v.table("After the plant: rounds and win rate", res.rename(columns={"tgt_side": "side"})[
            ["map", "side", "site", "situation", "rounds", "win%", "median plant (s)"]])
        # where they stand
        rows = []
        for (m, side, site), g in P.groupby(["map", "tgt_side", "site"]):
            nr = g["key"].nunique()
            occ = g.groupby("place").agg(rounds=("key", "nunique"),
                                        who=("name", lambda s_: ", ".join(s_.value_counts().index[:2])))
            for place, r in occ.sort_values("rounds", ascending=False).iterrows():
                if place:
                    rows.append({"map": m, "side": side, "site": site, "callout": place,
                                 "% of rounds": pct(r["rounds"], nr), "usually": r["who"]})
        v.table("Where they play it (someone in that callout 2-30 s after the plant)", pd.DataFrame(rows))
        # setups: each player's main callout -> one line per round
        per = P.groupby(["key", "name"])["place"].agg(lambda s_: s_.mode().iloc[0] if len(s_.mode()) else "").reset_index()
        kmap = info_["map"].to_dict()
        setup = {k: " + ".join(f"{show_place(kmap.get(k), p_)}" + (f" x{c}" if c > 1 else "")
                               for p_, c in Counter(g_["place"]).most_common() if p_)
                 for k, g_ in per.groupby("key")}
        S = R.assign(setup=R["key"].map(setup)).dropna(subset=["setup"])
        S = S[S["setup"] != ""]
        if len(S):
            st = (S.groupby(["map", "tgt_side", "site", "setup"]).agg(rounds=("tgt_won", "size"), won=("tgt_won", "mean"))
                  .reset_index().sort_values(["map", "tgt_side", "site", "rounds"], ascending=[True, False, True, False]))
            st["win%"] = (100 * st.pop("won")).round(0)
            v.table("Their setups (each alive player's main spot)", st.rename(columns={"tgt_side": "side"}))
        # utility after the plant
        N = self.ds.f("nades", phase=False)
        N = N[N["tgt"] & N["key"].isin(R["key"]) & N["post_plant"]] if len(N) else N
        if len(N):
            N = N.assign(site=N["key"].map(info_["site"]), after=N["t"] - N["key"].map(info_["plant_t"]))
            u = (N.groupby(["map", "tgt_side", "site", "kind", "l_place"])
                 .agg(n=("round", "size"), rounds=("key", "nunique"), after=("after", "median")).reset_index())
            u["after plant (s)"] = u.pop("after").round(0)
            u = u.rename(columns={"tgt_side": "side", "l_place": "lands at"}).sort_values("n", ascending=False)
            v.table("Utility after the plant", u)
        for (m, side, site), g in R.groupby(["map", "tgt_side", "site"]):
            if len(g) >= self.min_n:
                v.note(f"{m} {side} {label[side].split(' (')[0]} on {site}: {g['tgt_won'].mean():.0%} won over {len(g)} rounds.")

        def plot(out):
            paths = []
            for (m, side, site), g in P.groupby(["map", "tgt_side", "site"]):
                if g["key"].nunique() < 2:
                    continue
                pl = R[(R["map"] == m) & (R["tgt_side"] == side) & (R["site"] == site)]
                nr = g["key"].nunique()
                lab = []
                for place, c in g.groupby("place")["key"].nunique().sort_values(ascending=False).head(6).items():
                    q = g[g["place"] == place]
                    if place:
                        lab.append((q["x"].median(), q["y"].median(), q["z"].median(), f"{show_place(m, place)} {pct(c, nr):.0f}%"))
                what = "post-plant" if side == "T" else "retake"
                paths.append(map_plot(self.radar, m, f"{m} - {side} {what} on {site} ({nr} rounds)",
                                      out / f"postplant_{side}_{site}_{m}_{self.fname()}.png",
                                      densities=[((g["x"], g["y"], g["z"]), SIDE_COL[side])], labels=lab,
                                      layers=[dict(x=g["x"], y=g["y"], z=g["z"], color=SIDE_COL[side], size=3, alpha=0.18,
                                                   label=f"{side} players after the plant", count=False),
                                              dict(x=pl["plant_x"], y=pl["plant_y"], z=pl["plant_z"], color="#ffd43b",
                                                   marker="*", size=110, label="bomb plants")],
                                      style="both", subtitle=self.scope_label() + "  |  2-30 s after the plant  |  % = rounds with someone there"))
            return paths
        v.plots.append(("post-plant", plot))
        return v

    PLAYER_COLORS = ["#ffd43b", "#69db7c", "#da77f2", "#ff922b", "#63e6be", "#f783ac", "#a9e34b", "#74c0fc", "#e599f7"]

    def rotation_events(self):
        """CT rounds: each player's default zone (8-20 s) and, if he later moves to the other site, when and how."""
        P = self.ds.f("pos", side=False, phase=False)
        if not len(P):
            return pd.DataFrame(), {}
        P = P[P["tgt"] & P["alive"] & (P["tgt_side"] == "CT")]
        plant = self.ds.rounds.set_index("key")["plant_t"].to_dict()
        rows, paths = [], {}
        for (m, key, name), g in P.groupby(["map", "key", "name"]):
            g = g.sort_values("t")
            early = g[g["t"].between(8, 20)]
            if len(early) < 2:
                continue
            start = early["zone"].mode().iloc[0]
            later = g[g["t"] > 20]
            z, t = later["zone"].tolist(), later["t"].tolist()
            arrive = next((i for i in range(len(z) - 1) if z[i] in ("A", "B") and z[i] != start and z[i + 1] == z[i]), None)
            if arrive is None:
                rows.append(dict(map=m, key=key, name=name, start=start, rotated=False))
                continue
            before = [i for i in range(arrive) if z[i] == start]
            leave_t = t[before[-1]] if before else t[0]
            pt = plant.get(key)
            rows.append(dict(map=m, key=key, name=name, start=start, rotated=True, to=z[arrive], leave=leave_t,
                             arrive=t[arrive], after_plant=bool(pt == pt and pt is not None and t[arrive] > pt)))
            seg = g[(g["t"] >= leave_t) & (g["t"] <= t[arrive])]
            paths[(key, name)] = seg[["x", "y", "z"]]
        return pd.DataFrame(rows), paths

    def v_rotations(self, team_only=False):
        v = View("rotations", "Rotations (CT side)")
        E, paths = self.rotation_events()
        if not len(E):
            v.note("No CT rounds in scope.")
            return v
        rows = []
        for (m, name), g in E.groupby(["map", "name"]):
            rot = g[g["rotated"]]
            dest = Counter(f"{a}->{b}" for a, b in zip(rot.get("start", []), rot.get("to", [])))
            rows.append({"map": m, "player": name, "roster": self.ds.status_of(name), "CT rounds": len(g),
                         "default": g["start"].mode().iloc[0] if len(g) else "",
                         "rotations": len(rot), "rotate %": pct(len(rot), len(g)),
                         "leaves at (median s)": round(rot["leave"].median(), 0) if len(rot) else None,
                         "after the plant %": pct(rot["after_plant"].sum(), len(rot)) if len(rot) else None,
                         "goes": ", ".join(f"{k} {c}" for k, c in dest.most_common(3))})
        T = pd.DataFrame(rows).sort_values(["map", "rotate %"], ascending=[True, False])
        v.table("Who rotates (default zone at 8-20 s -> the other site later)", T)
        for _, r in T.iterrows():
            if r["CT rounds"] >= self.min_n and r["rotate %"] >= 40:
                v.note(f"{r['map']}: {self.ds.label(r['player'])} rotates off his {r['default']} default in "
                       f"{r['rotate %']:.0f}% of CT rounds, usually leaving around {r['leaves at (median s)']:.0f}s ({r['goes']}).")
        single = bool(self.cfg["PLAYER"])
        names = list(dict.fromkeys(E["name"]))
        color = {n: self.PLAYER_COLORS[i % len(self.PLAYER_COLORS)] for i, n in enumerate(names)}

        def draw(out, m, who, title, fname):
            rot = E[(E["map"] == m) & E["rotated"] & E["name"].isin(who)]
            if not len(rot):
                return None
            pths, starts, ends = [], [], []
            for r in rot.itertuples():
                pth = paths.get((r.key, r.name))
                if pth is None or len(pth) < 2:
                    continue
                c = color[r.name] if len(who) > 1 else SIDE_COL["CT"]
                pths.append(dict(x=pth["x"], y=pth["y"], z=pth["z"], color=c, alpha=0.5 if len(who) > 1 else 0.6))
                starts.append(pth.iloc[0])
                ends.append(pth.iloc[-1])
            if not pths:
                return None
            layers = []
            if len(who) > 1:
                for n in who:
                    k = rot[rot["name"] == n]
                    if len(k):
                        e = pd.DataFrame([paths[(kk, n)].iloc[-1] for kk in k["key"] if (kk, n) in paths])
                        if len(e):
                            layers.append(dict(x=e["x"], y=e["y"], z=e["z"], color=color[n], label=f"{n} ({len(k)} rotations)",
                                               size=22, count=False))
            else:
                S, Ee = pd.DataFrame(starts), pd.DataFrame(ends)
                layers = [dict(x=S["x"], y=S["y"], z=S["z"], color="#ffffff", label="leaves default", size=18, marker="o"),
                          dict(x=Ee["x"], y=Ee["y"], z=Ee["z"], color=SIDE_COL["CT"], label="arrives", size=30, marker="X")]
            lab = []
            if len(who) == 1:
                dest = Counter(f"{a} -> {b}" for a, b in zip(rot["start"], rot["to"]))
                sub = ", ".join(f"{k} {c}x" for k, c in dest.most_common(2)) + f"  |  leaves ~{rot['leave'].median():.0f}s"
            else:
                sub = f"{len(rot)} rotations  |  colour = player"
            return map_plot(self.radar, m, title, out / fname, layers=layers, paths=pths, style="points",
                            subtitle=f"{sub}  |  {self.scope_label()}")

        def plot(out):
            res = []
            for m in self.maps_in_scope():
                ms = E[E["map"] == m]
                who = list(dict.fromkeys(ms["name"]))
                if single:
                    for n in who:
                        p_ = draw(out, m, [n], f"{m} - {n} CT rotations", f"rotations_{safe_name(n)}_{m}_{self.fname()}.png")
                        if p_:
                            res.append(p_)
                    continue
                p_ = draw(out, m, who, f"{m} - CT rotations (all players)", f"rotations_team_{m}_{self.fname()}.png")
                if p_:
                    res.append(p_)
                if not team_only:
                    for n in who:
                        p_ = draw(out, m, [n], f"{m} - {n} CT rotations", f"rotations_{safe_name(n)}_{m}_{self.fname()}.png")
                        if p_:
                            res.append(p_)
            return res
        v.plots.append(("rotations", plot))
        return v

    def ct_setups(self, P=None, at=(15, 25)):
        P = self.ds.f("pos", side=False, phase=False) if P is None else P
        P = P[P["tgt"] & P["alive"] & (P["tgt_side"] == "CT") & P["t"].between(*at)]
        if not len(P):
            return pd.DataFrame()
        per = P.groupby(["map", "key", "name"])["zone"].agg(lambda s: s.mode().iloc[0]).reset_index()
        cnt = per.groupby(["map", "key"])["zone"].agg(
            nA=lambda z: int((z == "A").sum()), nM=lambda z: int((z == "Mid").sum()), nB=lambda z: int((z == "B").sum())
        ).reset_index()
        cnt["setup"] = [" ".join(f"{lbl}{n}" for lbl, n in (("A", a_), ("M", m_), ("B", b_)) if n)
                        for a_, m_, b_ in zip(cnt["nA"], cnt["nM"], cnt["nB"])]
        return cnt  # columns: map, key, nA, nM, nB, setup (zones with nobody left out, e.g. "M2 B3")

    def v_setups(self):
        v = View("setups", "CT setups")
        S = self.ct_setups()
        if not len(S):
            v.note("No CT data in scope.")
            return v
        f = (S.groupby(["map", "setup"]).size().rename("rounds").reset_index())
        f["%"] = f.apply(lambda r: pct(r["rounds"], (S["map"] == r["map"]).sum()), axis=1)
        v.table("Setups at 15-25 s (players in A / Mid / B)", f.sort_values(["map", "rounds"], ascending=[True, False]))
        roles = self.roles()
        if len(roles):
            v.table("CT positions by player", roles[roles["side"] == "CT"])
        R = self.ds.rounds.set_index("key")
        S["won"] = S["key"].map(R["tgt_won"])
        w = S.groupby(["map", "setup"]).agg(rounds=("won", "size"), win=("won", "mean")).reset_index()
        w["win%"] = (100 * w["win"]).round(0)
        v.table("Win% by setup", w[w["rounds"] >= 2].drop(columns="win").sort_values("rounds", ascending=False))
        return v

    def v_executes(self):
        v = View("executes", "T side - site hits & timings")
        R = self.ds.f("rounds", side=False)
        R = R[R["tgt_side"] == "T"]
        if not len(R):
            v.note("No T rounds in scope.")
            return v
        rows = []
        for m, g in R.groupby("map"):
            pl = g[g["planted"]]
            for site in ("A", "B", "Mid", "?"):
                s = g[g["t_target"] == site]
                if not len(s):
                    continue
                sp = s[s["planted"]]
                rows.append({"map": m, "went": site, "rounds": len(s), "%": pct(len(s), len(g)),
                             "planted": len(sp), "plant t med": round(sp["plant_t"].median(), 0) if len(sp) else None,
                             "fast (<35s)": int((sp["plant_t"] < 35).sum()), "win%": pct(s["tgt_won"].sum(), len(s))})
        v.table("Where the T side goes (plant site, else where they ended the round)", pd.DataFrame(rows))
        routes = self.t_routes(R)
        R = R.assign(hit=R["key"].map(lambda k: routes.get(k, ("", "", ""))[0]),
                     route=R["key"].map(lambda k: routes.get(k, ("", "", ""))[1]),
                     lanes=R["key"].map(lambda k: routes.get(k, ("", "", ""))[2]))
        hits = R[R["t_target"].isin(["A", "B"]) & (R["route"] != "")]
        if len(hits):
            h = (hits.groupby(["map", "t_target", "hit", "lanes"])
                 .agg(rounds=("tgt_won", "size"), won=("tgt_won", "mean"),
                      plant=("plant_t", "median")).reset_index())
            h["% of site hits"] = [pct(r, (hits["map"].eq(m_) & hits["t_target"].eq(st)).sum())
                                   for m_, st, r in zip(h["map"], h["t_target"], h["rounds"])]
            h["win%"] = (100 * h.pop("won")).round(0)
            h["median plant (s)"] = h.pop("plant").round(0)
            h = h.rename(columns={"t_target": "site", "lanes": "entry"}).sort_values(["map", "site", "rounds"], ascending=[True, True, False])
            v.table("How they hit each site (entry lanes into the site)", h)
            sp = hits.groupby(["map", "t_target"])["hit"].apply(lambda x: pct((x == "split").sum(), len(x))).reset_index()
            for _, r in sp.iterrows():
                v.note(f"{r['map']}: {r['hit']:.0f}% of their {r['t_target']} hits are splits (2+ entrances with 2+ players each).")
        by_buy = R.groupby(["map", "buy", "t_target"]).size().unstack(fill_value=0).reset_index()
        v.table("Site by buy type", by_buy)
        pis = R[R["pistol"]][["date", "opponent", "map", "round", "t_target", "hit", "route", "plant_t", "tgt_won"]]
        if len(pis):
            v.table("T pistol rounds", pis.rename(columns={"t_target": "went", "tgt_won": "won"}))
        # first contact timing
        K = self.ds.f("kills", side=False, phase=False, player=False)
        K = K[K["opening"] & (K["tgt_side"] == "T")]
        if len(K):
            fc = K.groupby("map")["t"].describe()[["count", "25%", "50%", "75%"]].round(0).reset_index()
            v.table("First kill timing on T side (s after freeze)", fc)
        return v

    def t_routes(self, R):
        """For each T round: ('split'|'single lane'|'', route text) from the lane each player entered the site by.
        A player's lane is the last area he was in before first stepping onto the bombsite (or, if he never got
        there, the last area he reached on that side of the map)."""
        P = self.ds.pos
        if P is None or not len(P) or not len(R):
            return {}
        P = P[P["tgt"] & (P["tgt_side"] == "T") & P["key"].isin(R["key"]) & (P["t"] >= 3)]
        target = R.set_index("key")["t_target"].to_dict()
        plant = R.set_index("key")["plant_t"].to_dict()
        maps = R.set_index("key")["map"].to_dict()
        on_site = lambda pl, x: f"bombsite{x.lower()}" in str(pl).lower().replace(" ", "")
        out = {}
        for key, g in P.groupby("key"):
            site = target.get(key)
            if site not in ("A", "B"):
                continue
            limit = plant.get(key)
            if limit is not None and not (isinstance(limit, float) and math.isnan(limit)):
                g = g[g["t"] <= limit + 2]
            lanes = Counter()
            for sid, h in g.sort_values("t").groupby("steamid"):
                places, zones, alive = h["place"].tolist(), h["zone"].tolist(), h["alive"].tolist()
                idx = next((i for i, pl in enumerate(places) if on_site(pl, site)), None)
                if idx is None:  # never reached the site: count him only if he got to that side of the map
                    live = [i for i, a_ in enumerate(alive) if a_]
                    if not live or zones[live[-1]] != site:
                        continue
                    idx = live[-1] + 1
                prev = [pl for pl in places[:idx] if pl and "spawn" not in str(pl).lower() and not on_site(pl, site)]
                if prev:
                    lanes[prev[-1]] += 1
            if not lanes:
                continue
            m = maps.get(key)
            top = lanes.most_common()
            big = [(pl, c) for pl, c in top if c >= 2]
            name = lambda pl: show_place(m, pl)
            if len(big) >= 2:
                text = "split: " + " + ".join(f"{name(pl)} x{c}" for pl, c in big)
                rest = [(pl, c) for pl, c in top if c < 2]
                kind = "split"
            else:
                text = f"via {name(top[0][0])} x{top[0][1]}"
                rest = top[1:]
                kind = "single lane"
            if rest:
                text += " (+" + ", ".join(f"{c} {name(pl)}" for pl, c in rest) + ")"
            sig = ("split: " + " + ".join(sorted(name(pl) for pl, _ in big))) if kind == "split" else f"via {name(top[0][0])}"
            out[key] = (kind, text, sig)
        return out

    def v_economy(self):
        v = View("economy", "Economy")
        R = self.ds.f("rounds", phase=False)
        if not len(R):
            return v
        e = R.groupby(["tgt_side", "buy"]).agg(rounds=("tgt_won", "size"), wins=("tgt_won", "sum")).reset_index()
        e["win%"] = (100 * e["wins"] / e["rounds"]).round(0)
        v.table("Their buy types and win rate", e.rename(columns={"tgt_side": "side"}))
        rows = []
        for demo, g in R.groupby("demo"):
            g = g.set_index("round")
            for pr in (1, 13):
                if pr in g.index and pr + 1 in g.index:
                    nxt = g.loc[pr + 1]
                    rows.append({"pistol": "won" if g.loc[pr, "tgt_won"] else "lost", "next buy": nxt["buy"],
                                 "next won": bool(nxt["tgt_won"]), "map": nxt["map"]})
        if rows:
            A = pd.DataFrame(rows)
            s = A.groupby(["pistol", "next buy"]).agg(attempts=("next won", "size"), won=("next won", "sum")).reset_index()
            v.table("Round after pistol", s)
            lost = A[A["pistol"] == "lost"]
            if len(lost):
                v.note(f"After losing a pistol they force/half-buy {fmt_pct(pct((lost['next buy'] != 'eco').sum(), len(lost)))} "
                       f"of the time ({len(lost)} cases).")
        vs = R.groupby(["opp_buy"]).agg(rounds=("tgt_won", "size"), won=("tgt_won", "sum")).reset_index()
        vs["win%"] = (100 * vs["won"] / vs["rounds"]).round(0)
        v.table("Their win rate vs opponent buy", vs.rename(columns={"opp_buy": "opponent buy"}))
        return v

    # ------------------------------------------------------------------ eco / force rounds
    LOW_BUYS = {"eco": "eco", "half-eco": "eco", "force": "force"}
    LOWBUY_WINDOW = 35  # seconds after freeze end used for "where they set up / which way they go"

    def _buy_groups(self, R):
        """Rounds tagged eco (eco + half-eco), force or full; pistols left out."""
        R = R[~R["pistol"]].copy()
        R["bg"] = R["buy"].map(lambda b: self.LOW_BUYS.get(b, "full" if b == "full" else None))
        return R[R["bg"].notna()]

    @staticmethod
    def _shape(zone_counts):
        return f"A{zone_counts.get('A', 0)} M{zone_counts.get('Mid', 0)} B{zone_counts.get('B', 0)}"

    def round_shapes(self, P, at=(15, 25)):
        """key -> 'A2 M1 B2': how many alive players of the scouted team were in each zone around `at` seconds."""
        p = P[P["tgt"] & P["alive"] & P["t"].between(*at)]
        if not len(p):
            return {}
        per = p.groupby(["key", "steamid"])["zone"].agg(lambda z: z.mode().iloc[0])
        return {k: self._shape(g.value_counts().to_dict()) for k, g in per.groupby(level=0)}

    def player_paths(self, P, keys, upto=None, steps=4):
        """(key, player) -> list of the first `steps` distinct callouts a player walked through (spawn skipped)."""
        upto = upto or self.LOWBUY_WINDOW
        p = P[P["tgt"] & P["key"].isin(keys) & P["t"].between(0, upto)].sort_values("t")
        out = {}
        for (key, name), g in p.groupby(["key", "name"]):
            seq = []
            for pl, alive in zip(g["place"], g["alive"]):
                if not alive:
                    break
                if not pl or "spawn" in str(pl).lower():
                    continue
                if not seq or seq[-1] != pl:
                    seq.append(pl)
                if len(seq) >= steps:
                    break
            if seq:
                out[(key, name)] = seq
        return out

    @staticmethod
    def kills_vs_force(cases):
        """Link between pistol kills and forcing (force = 1, eco/half-eco = 0; full buys left out).
        Returns dict(n, r, p, forced_avg, saved_avg, n_forced, n_saved) or None when there's too little to say."""
        d = [(c["pistol kills"], 1 if c["buy"] == "force" else 0) for c in cases if c["buy"] in ("force", "eco", "half-eco")]
        if len(d) < 4:
            return None
        x = np.array([k for k, _ in d], dtype=float)
        y = np.array([f for _, f in d], dtype=float)
        out = dict(n=len(d), n_forced=int(y.sum()), n_saved=int(len(y) - y.sum()),
                   forced_avg=float(x[y == 1].mean()) if y.sum() else None,
                   saved_avg=float(x[y == 0].mean()) if (y == 0).any() else None, r=None, p=None)
        if x.std() > 0 and 0 < y.sum() < len(y):
            out["r"] = float(np.corrcoef(x, y)[0, 1])
            if have("scipy"):
                try:
                    from scipy import stats
                    out["p"] = float(stats.pearsonr(x, y)[1])
                except Exception:
                    pass
        return out

    @staticmethod
    def describe_corr(c, what):
        """One plain-English line about a kills_vs_force() result."""
        if not c:
            return None
        if c["r"] is None:
            if not c["n_forced"]:
                return f"{what}: they never forced ({c['n']} cases), whatever their pistol kills - no link to measure."
            if not c["n_saved"]:
                return f"{what}: they always forced ({c['n']} cases), whatever their pistol kills - no link to measure."
            return f"{what}: their pistol kills were the same every time ({c['n']} cases) - no link to measure."
        r = c["r"]
        strength = "no real" if abs(r) < 0.1 else "a weak" if abs(r) < 0.3 else "a moderate" if abs(r) < 0.5 else "a strong"
        way = "more" if r > 0 else "less"
        txt = (f"{what}: {strength} link to pistol kills (r = {r:+.2f}, n = {c['n']}"
               + ((", p < 0.01" if c["p"] < 0.01 else f", p = {c['p']:.2f}") if c["p"] is not None else "") + ")")
        if abs(r) >= 0.1:
            txt += (f" - they force {way} often the more kills they got in the pistol "
                    f"(avg {c['forced_avg']:.1f} pistol kills when they forced vs {c['saved_avg']:.1f} when they saved)")
        txt += "."
        if c["n"] < 15:
            txt += " Small sample - treat it as a lean, not a rule."
        return txt

    def force_profile(self):
        """How often they force, and what they buy on CT after losing round 2 of the half, with their pistol kills.
        Returns (rates, ct_cases); ct_cases has one row per CT half with the decisions around rounds 2 and 3."""
        R = self.ds.f("rounds", phase=False, side=False)
        if not len(R):
            return pd.DataFrame(), pd.DataFrame()
        roster = "all" if self.cfg["ROSTER"] == "main" else self.cfg["ROSTER"]  # count every pistol kill
        K = self.ds.f("kills", phase=False, side=False, player=False, roster=roster)
        tk = (K[K["tgt_att"] & ~K["tgt_vic"]].groupby(["demo", "round"]).size()
              if len(K) else pd.Series(dtype=int))
        # --- rates (pistols, last round of each half and overtime left out: those buys aren't a real choice)
        reg = R[~R["pistol"] & ~R["round"].isin([12, 24]) & (R["round"] <= 24)]
        rows = []
        for (m, side), g in list(reg.groupby(["map", "tgt_side"])) + [(("all maps", "both"), reg)]:
            if not len(g):
                continue
            n_force = int((g["buy"] == "force").sum())
            n_eco = int(g["buy"].isin(["eco", "half-eco"]).sum())
            n_full = int((g["buy"] == "full").sum())
            fw = g[g["buy"] == "force"]
            rows.append({"map": m, "side": side, "rounds": len(g), "full buy %": pct(n_full, len(g)),
                         "force %": pct(n_force, len(g)), "eco %": pct(n_eco, len(g)),
                         "force when short %": pct(n_force, n_force + n_eco) if n_force + n_eco else None,
                         "short rounds": n_force + n_eco, "force win %": pct(fw["tgt_won"].sum(), len(fw)) if len(fw) else None})
        rates = pd.DataFrame(rows)
        # --- CT halves
        cases = []
        for demo, g in R.groupby("demo"):
            g = g.set_index("round")
            for pr in (1, 13):
                if pr not in g.index or g.loc[pr, "tgt_side"] != "CT" or pr + 1 not in g.index:
                    continue
                r1, r2 = g.loc[pr], g.loc[pr + 1]
                r3 = g.loc[pr + 2] if pr + 2 in g.index else None
                cases.append({"demo": demo, "map": r1["map"], "half": "1st" if pr == 1 else "2nd",
                              "pistol": "won" if r1["tgt_won"] else "lost", "pistol kills": int(tk.get((demo, pr), 0)),
                              "r2 buy": r2["buy"], "r2": "won" if r2["tgt_won"] else "lost",
                              "r3 buy": r3["buy"] if r3 is not None else None,
                              "r3": (("won" if r3["tgt_won"] else "lost") if r3 is not None else None)})
        return rates, pd.DataFrame(cases)

    def ct_force_tables(self, cases):
        """Tables + notes for CT round 2 / round 3 buys and pistol kills. Returns (tables, notes, correlations)."""
        tables, notes, corrs = [], [], {}
        if not len(cases):
            return tables, notes, corrs
        lost2 = cases[(cases["r2"] == "lost") & cases["r3 buy"].notna()]

        def buy_mix(g, col, won_col):
            n = len(g)
            return {"halves": n, "force %": pct((g[col] == "force").sum(), n),
                    "eco %": pct(g[col].isin(["eco", "half-eco"]).sum(), n), "full buy %": pct((g[col] == "full").sum(), n),
                    "won that round %": pct((g[won_col] == "won").sum(), n)}
        if len(lost2):
            rows = [{"after": f"{p} pistol, lost round 2", **buy_mix(g, "r3 buy", "r3")}
                    for p, g in lost2.groupby("pistol")]
            rows.append({"after": "any pistol, lost round 2", **buy_mix(lost2, "r3 buy", "r3")})
            tables.append(("CT side - their round-3 buy after losing round 2 of the half (rounds 3 / 15)", pd.DataFrame(rows)))
            won_p = lost2[lost2["pistol"] == "won"]
            if len(won_p):
                f_ = (won_p["r3 buy"] == "force").mean()
                notes.append(f"CT, won the pistol but lost round 2: they force round 3 in {f_:.0%} of {len(won_p)} halves"
                             f" (save {won_p['r3 buy'].isin(['eco', 'half-eco']).mean():.0%},"
                             f" full buy {(won_p['r3 buy'] == 'full').mean():.0%}).")
            any_f = (lost2["r3 buy"] == "force").mean()
            notes.append(f"CT, lost round 2 (any pistol result): they force round 3 in {any_f:.0%} of {len(lost2)} halves.")
        lostp = cases[cases["pistol"] == "lost"]
        if len(lostp):
            rows = [{"after": "lost CT pistol", **buy_mix(lostp, "r2 buy", "r2")}]
            tables.append(("CT side - their round-2 buy after losing the pistol", pd.DataFrame(rows)))
        # pistol kills vs the decision
        rows = []
        for label, g, col in (("R3 after losing CT round 2", lost2, "r3 buy"), ("R2 after losing CT pistol", lostp, "r2 buy")):
            if not len(g):
                continue
            for k, gg in g.groupby("pistol kills"):
                n = len(gg)
                rows.append({"decision": label, "pistol kills": int(k), "halves": n,
                             "force %": pct((gg[col] == "force").sum(), n),
                             "eco %": pct(gg[col].isin(["eco", "half-eco"]).sum(), n),
                             "full buy %": pct((gg[col] == "full").sum(), n)})
            c = self.kills_vs_force([{"pistol kills": k, "buy": b} for k, b in zip(g["pistol kills"], g[col])])
            corrs[label] = c
            line = self.describe_corr(c, label)
            if line:
                notes.append(line)
            elif len(g):
                notes.append(f"{label}: only {len(g)} case(s) - not enough to check a link to pistol kills.")
        if rows:
            tables.append(("CT side - pistol kills (theirs) vs what they bought next", pd.DataFrame(rows)))
        return tables, notes, corrs

    def v_lowbuy(self):
        v = View("lowbuy", "Eco & force rounds - where they play and how they path")
        rates, cases = self.force_profile()
        sd = self.cfg["SIDE"]
        if len(rates):
            shown = rates if not sd else rates[rates["side"].isin([sd, "both"])]
            v.table("How often they force (pistols and last round of each half left out; "
                    "'force when short' = forced instead of saving when they couldn't full buy)", shown)
            allr = rates[rates["side"] == "both"]
            if len(allr) and allr.iloc[0]["short rounds"]:
                a = allr.iloc[0]
                v.note(f"They force {fmt_pct(a['force %'])} of all non-pistol rounds, and when they can't full buy they "
                       f"force {fmt_pct(a['force when short %'])} of the time instead of saving ({int(a['short rounds'])} rounds).")
        ct_tables, ct_notes = [], []
        if sd in ("", "CT"):
            ct_tables, ct_notes, _ = self.ct_force_tables(cases)
        R = self._buy_groups(self.ds.f("rounds", phase=False))
        low = R[R["bg"].isin(["eco", "force"])]
        if not len(low):
            for t, df in ct_tables:
                v.table(t, df)
            for n_ in ct_notes:
                v.note(n_)
            v.note("No eco or force-buy rounds in scope (pistol rounds are left out).")
            return v
        P = self.ds.f("pos", phase=False, player=False)
        P = P[P["key"].isin(R["key"])]
        shapes = self.round_shapes(P)
        R["shape"] = R["key"].map(shapes).fillna("?")
        low = R[R["bg"].isin(["eco", "force"])]
        full = R[R["bg"] == "full"]
        W = self.LOWBUY_WINDOW

        # 1. what they do on low buys
        rows = []
        for (m, side, bg), g in R[R["bg"].isin(["eco", "force"])].groupby(["map", "tgt_side", "bg"]):
            ref = full[(full["map"] == m) & (full["tgt_side"] == side)]
            row = {"map": m, "side": side, "buy": bg, "rounds": len(g), "win%": pct(g["tgt_won"].sum(), len(g)),
                   "full-buy win%": pct(ref["tgt_won"].sum(), len(ref)) if len(ref) else None,
                   "usual shape": top1(g["shape"][g["shape"] != "?"]),
                   "shape %": pct((g["shape"] == top1(g["shape"][g["shape"] != "?"])).sum(), len(g))}
            if side == "T":
                vc = g["t_target"].value_counts()
                row["went"] = ", ".join(f"{k} {100 * c / len(g):.0f}%" for k, c in vc.items() if k != "?")
                pl = g[g["planted"]]
                row["planted%"] = pct(len(pl), len(g))
                row["plant t med"] = round(pl["plant_t"].median(), 0) if len(pl) else None
            rows.append(row)
        v.table("Low-buy rounds (shape = players in A / Mid / B at 15-25 s)", pd.DataFrame(rows))
        for t, df in ct_tables:
            v.table(t, df)
        for n_ in ct_notes:
            v.note(n_)

        # 2. where they stand compared with full buys
        PW = P[P["tgt"] & P["alive"] & P["t"].between(0, W) & (P["place"].astype(str) != "")]
        rows = []
        for (m, side), g in PW.groupby(["map", "tgt_side"]):
            ref_keys = set(full[(full["map"] == m) & (full["tgt_side"] == side)]["key"])
            ref = g[g["key"].isin(ref_keys)]
            ref_share = ref.groupby("place")["key"].nunique() / max(1, len(ref_keys))
            for bg in ("eco", "force"):
                keys = set(low[(low["map"] == m) & (low["tgt_side"] == side) & (low["bg"] == bg)]["key"])
                if len(keys) < 2:
                    continue
                gg = g[g["key"].isin(keys)]
                share = gg.groupby("place")["key"].nunique() / len(keys)
                who = gg.groupby("place")["name"].agg(lambda s_: ", ".join(s_.value_counts().index[:2]))
                tfirst = gg.groupby(["place", "key"])["t"].min().groupby("place").median()
                for place, sh in share.sort_values(ascending=False).items():
                    base = ref_share.get(place, 0.0)
                    if "spawn" in str(place).lower():
                        continue
                    if ref_keys and abs(sh - base) < 0.15:  # same as on full buys: not specific to low buys
                        continue
                    if not ref_keys and sh < 0.3:
                        continue
                    rows.append({"map": m, "side": side, "buy": bg, "callout": place, "% of rounds": round(100 * sh),
                                 "% on full buys": round(100 * base) if ref_keys else None,
                                 "diff": round(100 * (sh - base)) if ref_keys else None,
                                 "first there (s)": round(tfirst.get(place, float("nan")), 0), "usually": who.get(place, "")})
        T = pd.DataFrame(rows)
        if len(T):
            T = T.assign(_d=T["diff"].abs().fillna(T["% of rounds"])).sort_values(
                ["map", "side", "buy", "_d"], ascending=[True, True, True, False]).drop(columns="_d")
            T = T.groupby(["map", "side", "buy"], group_keys=False).head(8)
        v.table(f"Where they play differently on low buys (first {W} s, alive; diff = points vs their full buys)", T)

        # 3. paths
        paths = self.player_paths(P, set(low["key"]))
        bgk = low.set_index("key")[["map", "tgt_side", "bg"]]
        cnt, who, rnds = Counter(), defaultdict(Counter), defaultdict(set)
        for (key, name), seq in paths.items():
            m, side, bg = bgk.loc[key]
            txt = " > ".join(show_place(m, x) for x in seq)
            cnt[(m, side, bg, txt)] += 1
            who[(m, side, bg, txt)][name] += 1
            rnds[(m, side, bg, txt)].add(key)
        nlow = low.groupby(["map", "tgt_side", "bg"]).size().to_dict()
        rows = [{"map": m, "side": side, "buy": bg, "path": txt, "times": c,
                 "% of rounds": pct(len(rnds[(m, side, bg, txt)]), nlow.get((m, side, bg), 1)),
                 "players": ", ".join(f"{n}" for n, _ in who[(m, side, bg, txt)].most_common(3))}
                for (m, side, bg, txt), c in cnt.items() if c >= 2]
        PT = pd.DataFrame(rows)
        if len(PT):
            PT = (PT.sort_values(["map", "side", "buy", "times"], ascending=[True, True, True, False])
                  .groupby(["map", "side", "buy"], group_keys=False).head(6))
        v.table(f"Most common routes on low buys (first callouts each player walks through, {W} s)", PT)

        # 4. fights
        K = self.ds.f("kills", phase=False, player=False)
        K = K[K["opening"] & K["key"].isin(low["key"])]
        if len(K):
            K = K.assign(bg=K["key"].map(low.set_index("key")["bg"]),
                         spot=np.where(K["tgt_att"], K["a_place"], K["v_place"]),
                         who=np.where(K["tgt_att"], K["att_name"], K["vic_name"]))
            K = K[K["tgt_att"] | K["tgt_vic"]]
            f = (K.groupby(["map", "tgt_side", "bg", "spot"])
                 .agg(FK=("tgt_att", "sum"), FD=("tgt_vic", "sum"), t_med=("t", "median"),
                      players=("who", lambda s_: ", ".join(s_.value_counts().index[:2])))
                 .reset_index().rename(columns={"tgt_side": "side", "bg": "buy", "spot": "place"}))
            f["t_med"] = f["t_med"].round(0)
            f["n"] = f["FK"] + f["FD"]
            v.table("Opening duels on low buys (where the first fight happens)",
                    f.sort_values("n", ascending=False).drop(columns="n"))
            early = K[K["t"] <= 20]
            for (m, side, bg), g in low.groupby(["map", "tgt_side", "bg"]):
                if len(g) < 3:
                    continue
                e = early[(early["map"] == m) & (early["tgt_side"] == side) & (early["bg"] == bg)]
                share = e["key"].nunique() / len(g)
                if share >= 0.4:
                    v.note(f"{m} {side} {bg}: the first fight comes before 20 s in {share:.0%} of rounds - "
                           f"they look for early picks/rushes instead of saving.")

        # 5. headline notes
        for (m, side, bg), g in low.groupby(["map", "tgt_side", "bg"]):
            if len(g) < 3:
                continue
            sh = g["shape"][g["shape"] != "?"]
            if len(sh):
                top, share = sh.value_counts().index[0], sh.value_counts(normalize=True).iloc[0]
                if share >= 0.5:
                    v.note(f"{m} {side} {bg} rounds: usually {top} at 15-25 s ({share:.0%} of {len(g)} rounds).")
            if side == "T":
                vc = g["t_target"][g["t_target"] != "?"].value_counts(normalize=True)
                if len(vc) and vc.iloc[0] >= 0.6:
                    v.note(f"{m} T {bg} rounds: they go {vc.index[0]} {vc.iloc[0]:.0%} of the time ({len(g)} rounds).")

        def plot(out):
            paths_ = []
            for m in self.maps_in_scope():
                for side in ([self.cfg["SIDE"]] if self.cfg["SIDE"] else ["T", "CT"]):
                    keys = set(low[(low["map"] == m) & (low["tgt_side"] == side)]["key"])
                    g = PW[PW["key"].isin(keys) & (PW["map"] == m)]
                    if len(g) < 5:
                        continue
                    nr = len(keys)
                    tops = g.groupby("place")["key"].nunique().sort_values(ascending=False).head(7)
                    lab = [(g[g["place"] == pl]["x"].median(), g[g["place"] == pl]["y"].median(), g[g["place"] == pl]["z"].median(),
                            f"{show_place(m, pl)} {pct(c, nr):.0f}%") for pl, c in tops.items() if pl]
                    layers, dens = [], []
                    for bg, col, mk in (("eco", "#ffd43b", "o"), ("force", "#c77dff", "s")):
                        kk = set(low[(low["map"] == m) & (low["tgt_side"] == side) & (low["bg"] == bg)]["key"])
                        s_ = g[g["key"].isin(kk)]
                        if len(s_):
                            layers.append(dict(x=s_["x"], y=s_["y"], z=s_["z"], color=col, size=5, alpha=0.25, marker=mk,
                                               label=f"{bg} ({len(kk)} rounds)", count=False))
                    paths_.append(map_plot(self.radar, m, f"{m} - {side} side on eco / force rounds (first {W} s)",
                                           out / f"lowbuy_{side}_{m}_{self.fname()}.png",
                                           densities=[((g["x"], g["y"], g["z"]), SIDE_COL[side])], layers=layers, labels=lab,
                                           style="both", subtitle=self.scope_label() + "  |  % = low-buy rounds with someone there"))
            return paths_
        v.plots.append(("low-buy positions", plot))
        return v

    def _force_counterplay(self, add):
        """Counter-play lines from their CT round-2 / round-3 buying (all maps in scope)."""
        _, cases = self.force_profile()
        if not len(cases):
            return
        lost2 = cases[(cases["r2"] == "lost") & cases["r3 buy"].notna()]
        won_p = lost2[lost2["pistol"] == "won"]
        if len(won_p) >= 3:
            f_ = (won_p["r3 buy"] == "force").mean()
            sv = won_p["r3 buy"].isin(["eco", "half-eco"]).mean()
            if f_ >= 0.6:
                add(2.6 + f_, "all", "CT", "eco/force",
                    f"When they win the CT pistol but lose round 2, they force round 3 {f_:.0%} of the time. "
                    f"Expect SMGs/pistols with armor on CT in round 3/15: play for range, keep the AWP up and don't "
                    f"run into close angles - win that round and they're broke for round 4.", f"{len(won_p)} halves")
            elif sv >= 0.6:
                add(2.2 + sv, "all", "CT", "eco/force",
                    f"When they win the CT pistol but lose round 2, they save round 3 {sv:.0%} of the time. "
                    f"Take free map control in round 3/15 and don't over-invest; expect a full buy in round 4/16.",
                    f"{len(won_p)} halves")
        lostp = cases[cases["pistol"] == "lost"]
        for label, g, col in (("round 3 after losing CT round 2", lost2, "r3 buy"),
                              ("round 2 after losing the CT pistol", lostp, "r2 buy")):
            c = self.kills_vs_force([{"pistol kills": k, "buy": b} for k, b in zip(g["pistol kills"], g[col])])
            if c and c["r"] is not None and abs(c["r"]) >= 0.4 and c["n"] >= 6:
                hi = c["forced_avg"] if c["r"] > 0 else c["saved_avg"]
                act = "force" if c["r"] > 0 else "save"
                add(1.9 + abs(c["r"]), "all", "CT", "eco/force",
                    f"Their {label} depends on the pistol: they {act} more when they got more pistol kills "
                    f"(about {hi:.1f} on average when they {act}d; r = {c['r']:+.2f}). Count their pistol kills "
                    f"to predict the buy.", f"{c['n']} halves")

    def _lowbuy_counterplay(self, m, rm, P, add):
        """Counter-play lines for their eco / force rounds on one map."""
        R = self._buy_groups(rm)
        if not len(R):
            return
        P = P[P["map"] == m]
        shapes = self.round_shapes(P[P["key"].isin(R["key"])])
        R = R.assign(shape=R["key"].map(shapes).fillna("?"))
        full = R[R["bg"] == "full"]
        W = self.LOWBUY_WINDOW
        for side in ("T", "CT"):
            low = R[(R["tgt_side"] == side) & R["bg"].isin(["eco", "force"])]
            if len(low) < max(4, self.min_n):
                continue
            label = "eco/force"
            if side == "T":
                vc = low["t_target"][low["t_target"] != "?"].value_counts(normalize=True)
                if len(vc) and vc.iloc[0] >= 0.6 and vc.index[0] in ("A", "B"):
                    pl = low[low["planted"]]
                    tm = f", plants around {pl['plant_t'].median():.0f}s" if len(pl) >= 2 else ""
                    add(2.4 + vc.iloc[0], m, "T", "eco/force",
                        f"On their {label} T rounds they go {vc.index[0]} {vc.iloc[0]:.0%} of the time{tm}. "
                        f"Shade your anti-eco setup toward {vc.index[0]}, hold close angles with a partner and keep "
                        f"a rotator ready - don't give free rifles by peeking alone.", f"{len(low)} low-buy T rounds")
            sh = low["shape"][low["shape"] != "?"]
            if len(sh) >= 4:
                top, share = sh.value_counts().index[0], sh.value_counts(normalize=True).iloc[0]
                full_share = (full[full["tgt_side"] == side]["shape"] == top).mean() if len(full) else 0
                if share >= 0.45 and share - full_share >= 0.2:
                    n_ = dict(zip(("A", "Mid", "B"), (int(x[1:] if x[0] != "M" else x[1:]) for x in top.split())))
                    big = max(n_, key=n_.get)
                    if big in ("A", "B") and n_[big] >= 3:
                        what = (f"Expect {n_[big]} of them hitting {big} together; " if side == "T"
                                else f"Expect {n_[big]} of them stacked on {big}; ")
                    elif n_["Mid"] >= 3:
                        what = f"Expect them grouped up mid-map ({n_['Mid']} together) before they commit; "
                    else:
                        what = "Expect a different spread than their usual setup; "
                    add(2.0 + share, m, side, "eco/force",
                        f"On {label} {side} rounds they switch to {top} at 15-25 s ({share:.0%}, vs {full_share:.0%} on full buys). "
                        + what + "use utility before contact and trade in pairs.", f"{len(sh)} rounds")
            keys = set(low["key"])
            ref_keys = set(full[full["tgt_side"] == side]["key"])
            pw = P[P["tgt"] & P["alive"] & P["t"].between(0, W) & (P["place"].astype(str) != "")]
            a = pw[pw["key"].isin(keys)].groupby("place")["key"].nunique() / len(keys)
            b = (pw[pw["key"].isin(ref_keys)].groupby("place")["key"].nunique() / len(ref_keys)) if ref_keys else pd.Series(dtype=float)
            for place, sh_ in a.sort_values(ascending=False).head(10).items():
                base = b.get(place, 0.0)
                if sh_ >= 0.5 and sh_ - base >= 0.25:
                    t0 = pw[pw["key"].isin(keys) & (pw["place"] == place)].groupby("key")["t"].min().median()
                    add(1.8 + sh_, m, side, "eco/force",
                        f"On {label} {side} rounds someone is at {place} by ~{t0:.0f}s in {sh_:.0%} of rounds "
                        f"(only {base:.0%} on full buys). Pre-aim or molly {place} at that timing in the anti-eco.",
                        f"{len(keys)} rounds")

    def v_trends(self):
        v = View("trends", "Trends over time")
        R = self.ds.f("rounds", side=False)
        if not len(R):
            return v
        K = self.ds.f("kills", side=False, phase=False, player=False)
        N = self.ds.f("nades", side=False, phase=False, player=False)
        rows = []
        for demo, g in R.groupby("demo"):
            t = g[g["tgt_side"] == "T"]
            k = K[(K["demo"] == demo) & K["opening"]]
            n = N[(N["demo"] == demo) & N["tgt"]]
            rows.append({"date": g["date"].iloc[0], "opponent": g["opponent"].iloc[0], "map": g["map"].iloc[0],
                         "score": f"{int(g['tgt_won'].sum())}-{int((~g['tgt_won']).sum())}",
                         "T->A%": pct((t["t_target"] == "A").sum(), len(t)), "T->B%": pct((t["t_target"] == "B").sum(), len(t)),
                         "plant t": round(t["plant_t"].median(), 0) if t["planted"].any() else None,
                         "opening win%": pct(k["tgt_att"].sum(), len(k)),
                         "nades/rnd": round(len(n) / max(1, len(g)), 1)})
        v.table("Match by match", pd.DataFrame(rows).sort_values("date"))
        for line in self.shifts():
            v.note(line)
        return v

    # ------------------------------------------------------------------ anomalies
    def anomalies(self):
        """Things that stand out: player outliers, odd rounds, extreme results, recent shifts."""
        out = []
        R = self.ds.f("rounds", side=False, phase=False)
        if not len(R):
            return out
        # 1. player outliers vs teammates
        P = self.v_players().tables[0][1]
        if len(P) >= 3:
            for col, label, hi_bad in (("FK", "opening kills", False), ("FD", "opening deaths", True),
                                       ("traded deaths%", "traded deaths", False), ("ADR", "ADR", False),
                                       ("nades/rnd", "utility per round", False),
                                       ("flash blinds/flash", "enemies blinded per flash", False)):
                s = pd.to_numeric(P[col], errors="coerce")
                if s.std(ddof=0) == 0 or s.isna().all():
                    continue
                z = (s - s.mean()) / s.std(ddof=0)
                for i in z.index:
                    if abs(z[i]) >= 1.5 and P.loc[i, "rounds"] >= 10:
                        hi = z[i] > 0
                        out.append(dict(kind="player", map="all", score=abs(z[i]),
                                        text=f"{self.ds.label(P.loc[i, 'player'])}: {'very high' if hi else 'very low'} {label} "
                                             f"({P.loc[i, col]} vs team avg {s.mean():.1f})"))
        # 2. results that stand out
        for m, g in R.groupby("map"):
            for side in ("T", "CT"):
                s = g[g["tgt_side"] == side]
                if len(s) >= 15:
                    w = pct(s["tgt_won"].sum(), len(s))
                    if w >= 65 or w <= 35:
                        out.append(dict(kind="result", map=m, score=abs(w - 50) / 10,
                                        text=f"{side} side on {m}: {w:.0f}% round win rate over {len(s)} rounds "
                                             f"({'a strength' if w > 50 else 'a weakness'})"))
            for buy in ("eco", "half-eco", "force"):
                s = g[g["buy"] == buy]
                if len(s) >= 4 and s["tgt_won"].mean() >= 0.4:
                    out.append(dict(kind="result", map=m, score=3 * s["tgt_won"].mean(),
                                    text=f"They win {pct(s['tgt_won'].sum(), len(s)):.0f}% of {buy} rounds on {m} "
                                         f"({len(s)} rounds) - unusually dangerous on low buys"))
            full = g[(g["buy"] == "full") & (g["opp_buy"].isin(["eco", "half-eco"]))]
            if len(full) >= 4 and full["tgt_won"].mean() <= 0.7:
                out.append(dict(kind="result", map=m, score=2.5,
                                text=f"They lose {pct((~full['tgt_won']).sum(), len(full)):.0f}% of full-buy rounds vs "
                                     f"opponent ecos on {m} - anti-eco discipline is weak"))
        # 4. very fast hits
        t = R[(R["tgt_side"] == "T") & R["planted"] & (R["plant_t"] < 30)]
        for m, g in t.groupby("map"):
            out.append(dict(kind="timing", map=m, score=1 + len(g) / 2,
                            text=f"{len(g)} rush plant(s) under 30 s on {m} (sites: "
                                 f"{', '.join(f'{k}{v}' for k, v in g['site'].value_counts().items())})"))
        # 5. recent shifts
        for line in self.shifts():
            out.append(dict(kind="trend", map="", score=2.5, text=line))
        out.sort(key=lambda d: -d["score"])
        return out

    def shifts(self):
        """Compare the most recent matches with older ones on the same map."""
        res = []
        R = self.ds.f("rounds", side=False, phase=False, last_n=0)
        for m, g in R.groupby("map"):
            demos = g.sort_values("date")["demo"].unique()
            if len(demos) < 4:
                continue
            k = max(2, len(demos) // 3)
            recent, old = g[g["demo"].isin(demos[-k:])], g[g["demo"].isin(demos[:-k])]
            for site in ("A", "B"):
                a = pct((recent[recent["tgt_side"] == "T"]["t_target"] == site).sum(), (recent["tgt_side"] == "T").sum())
                b = pct((old[old["tgt_side"] == "T"]["t_target"] == site).sum(), (old["tgt_side"] == "T").sum())
                if not math.isnan(a) and not math.isnan(b) and abs(a - b) >= 20:
                    res.append(f"{m}: T side goes {site} {a:.0f}% in their last {k} matches vs {b:.0f}% before")
            N = self.ds.nades
            n = N[(N["map"] == m) & N["tgt"] & (N["kind"] == "smoke")]
            if len(n):
                newl = set(zip(n[n["demo"].isin(demos[-k:])]["t_place"], n[n["demo"].isin(demos[-k:])]["l_place"]))
                oldl = set(zip(n[n["demo"].isin(demos[:-k])]["t_place"], n[n["demo"].isin(demos[:-k])]["l_place"]))
                fresh = Counter((a, b) for a, b in zip(n[n["demo"].isin(demos[-k:])]["t_place"],
                                                       n[n["demo"].isin(demos[-k:])]["l_place"]) if (a, b) not in oldl and a and b)
                for (a, b), c in fresh.most_common(2):
                    if c >= 2:
                        res.append(f"{m}: new smoke in recent matches - {a} -> {b} ({c}x, never before)")
        return res

    # ------------------------------------------------------------------ counter-play
    def counterplay(self):
        """Rule-based suggestions, each with the evidence behind it."""
        S = []
        mn = self.min_n
        R = self.ds.f("rounds", side=False, phase=False)
        K = self.ds.f("kills", side=False, phase=False, player=False)
        N = self.ds.f("nades", side=False, phase=False, player=False)
        P = self.ds.f("pos", side=False, phase=False, player=False)
        if not len(R):
            return S

        def add(prio, m, side, area, text, ev):
            S.append(dict(priority=round(prio, 2), map=m, their_side=side, area=area, suggestion=text, evidence=ev))

        for m, rm in R.groupby("map"):
            rt, rc = rm[rm["tgt_side"] == "T"], rm[rm["tgt_side"] == "CT"]
            k = K[(K["map"] == m) & K["opening"]]
            # --- T: site preference + timing
            if len(rt) >= 8:
                vc = rt["t_target"].value_counts(normalize=True)
                site, share = vc.index[0], vc.iloc[0]
                pl = rt[rt["planted"]]
                med = pl["plant_t"].median() if len(pl) else float("nan")
                if site in ("A", "B") and share >= 0.6:
                    add(2 + 3 * share, m, "T", "site preference",
                        f"Lean your CT setup toward {site}: they go {site} in {share:.0%} of T rounds. "
                        f"Their median plant is at {med:.0f}s, so rotate off early info rather than waiting for the plant.",
                        f"{(rt['t_target'] == site).sum()}/{len(rt)} T rounds")
                fast = (pl["plant_t"] < 35).mean() if len(pl) else 0
                if len(pl) >= 6 and fast >= 0.3:
                    add(2 + fast, m, "T", "fast hits",
                        f"Expect fast hits ({fast:.0%} of plants under 35 s). Keep early utility (molly/HE) for the "
                        f"entrance they use and don't play deep/aggressive positions in the first 30 s.",
                        f"{int((pl['plant_t'] < 35).sum())}/{len(pl)} plants")
                elif len(pl) >= 6 and med >= 70:
                    add(2, m, "T", "slow defaults",
                        f"They play slow defaults (median plant {med:.0f}s). Take early map control and mid-round "
                        f"picks; save rotations until they commit - fakes are less likely than late executes.",
                        f"median plant {med:.0f}s over {len(pl)} plants")
                pis = rt[rt["pistol"]]
                if len(pis) >= 2:
                    pv = pis["t_target"].value_counts()
                    if pv.iloc[0] / len(pis) >= 0.66:
                        add(2.2, m, "T", "pistol",
                            f"T pistol: they went {pv.index[0]} in {pv.iloc[0]}/{len(pis)} pistols - stack it or set a crossfire there.",
                            f"{len(pis)} T pistol rounds")
            # --- CT: aggression spots and weak zones
            kc = k[(k["tgt_side"] == "CT") & (k["tgt_att"] | k["tgt_vic"])].copy()
            kc["who"] = np.where(kc["tgt_att"], kc["att_name"], kc["vic_name"])
            kc["spot"] = np.where(kc["tgt_att"], kc["a_place"], kc["v_place"])
            for (player, place), g in kc.groupby(["who", "spot"]):
                won = (g["tgt_att"]).sum()
                if len(g) >= mn and place:
                    rate = won / len(g)
                    t_med = g["t"].median()
                    if rate >= 0.55:
                        add(1.5 + min(len(g), 12) / 4, m, "CT", "aggression",
                            f"{self.ds.label(player)} takes early fights at {place} on CT (around {t_med:.0f}s) and wins {rate:.0%}. "
                            f"Don't dry-peek it: flash/molly {place} first or swing with a trade partner.",
                            f"{len(g)} opening duels")
                    elif rate <= 0.4:
                        add(1.2 + min(len(g), 12) / 4, m, "CT", "exploit",
                            f"{self.ds.label(player)} keeps taking the opening duel at {place} on CT and loses it ({rate:.0%} won). "
                            f"Prepare a trade/crossfire there around {t_med:.0f}s and let him come to you.",
                            f"{len(g)} opening duels")
            S_ = self.ct_setups(P[P["map"] == m])
            if len(S_) >= 8:
                vc = S_["setup"].value_counts(normalize=True)
                for zone in ("A", "B"):
                    ones = (S_[f"n{zone}"] == 1).mean()
                    zeros = (S_[f"n{zone}"] == 0).mean()
                    if ones + zeros >= 0.55:
                        anchor = self._anchor(P[P["map"] == m], zone)
                        add(1.5 + 2.5 * (ones + zeros), m, "CT", "weak site",
                            f"{zone} is usually held by one player or nobody at 15-25 s ({ones + zeros:.0%} of CT rounds"
                            f"{', usually ' + anchor if anchor else ''}). Fast or split hits on {zone} should face a 1v5 entry.",
                            f"{len(S_)} CT rounds sampled")
                stack = ((S_["nA"] >= 4) | (S_["nB"] >= 4)).mean()
                if stack >= 0.15:
                    add(1.8, m, "CT", "stacks",
                        f"They stack a site (4+ players) in {stack:.0%} of CT rounds. Get early info before committing "
                        f"- a fast default can find the empty site.", f"{len(S_)} CT rounds")
            # --- utility predictability
            n = N[(N["map"] == m) & N["tgt"]]
            for side, rr in (("T", rt), ("CT", rc)):
                if len(rr) < 8:
                    continue
                ns = n[n["tgt_side"] == side]
                for kind in ("smoke", "molotov", "flash"):
                    nk = ns[ns["kind"] == kind]
                    if not len(nk):
                        continue
                    by = nk.groupby("l_place").agg(rounds=("key", "nunique"), t=("t", "median"),
                                                   frm=("t_place", lambda s: s.mode().iloc[0] if len(s.mode()) else ""))
                    for place, r in by.iterrows():
                        share = r["rounds"] / len(rr)
                        if place and r["rounds"] >= mn and share >= 0.35:
                            if kind == "smoke" and side == "T":
                                txt = (f"Their {place} smoke comes in {share:.0%} of T rounds at ~{r['t']:.0f}s "
                                       f"(thrown from {r['frm']}). Hold an off-angle that smoke doesn't cover, or molly "
                                       f"{r['frm']} a few seconds before {r['t']:.0f}s to stop the lineup.")
                            elif kind == "smoke":
                                txt = (f"On CT they smoke {place} in {share:.0%} of rounds at ~{r['t']:.0f}s. "
                                       f"Time your push for when it fades (~{r['t'] + 18:.0f}s) or go elsewhere first.")
                            elif kind == "molotov" and side == "CT":
                                txt = (f"They molly {place} in {share:.0%} of CT rounds at ~{r['t']:.0f}s. "
                                       f"Bait it with one player, then go through {place} once it burns out (~7 s).")
                            elif kind == "molotov":
                                txt = (f"Their {place} molly comes in {share:.0%} of T rounds at ~{r['t']:.0f}s - "
                                       f"don't play the spot it clears at that timing.")
                            else:
                                txt = (f"They flash {place} in {share:.0%} of {side} rounds at ~{r['t']:.0f}s. "
                                       f"Turn away/reposition at that timing and re-peek after the pop.")
                            add(1 + 2.5 * share, m, side, kind, txt, f"{int(r['rounds'])}/{len(rr)} rounds")
            # --- T entry fraggers
            kt = k[(k["tgt_side"] == "T") & (k["tgt_att"] | k["tgt_vic"])].copy()
            kt["who"] = np.where(kt["tgt_att"], kt["att_name"], kt["vic_name"])
            for player, g in kt.groupby("who"):
                if len(g) >= max(mn, 6):
                    rate = g["tgt_att"].mean()
                    place = top1(np.where(g["tgt_att"], g["a_place"], g["v_place"]))
                    share = len(g) / max(1, len(rt))
                    if share >= 0.25:
                        add(1.5 + share * 2, m, "T", "entry",
                            f"{self.ds.label(player)} is their entry ({share:.0%} of T rounds, mostly via {place}) and wins "
                            f"{rate:.0%} of those duels. {'Deny him the first duel with utility and double-peeks.' if rate >= 0.5 else 'He loses more than he wins - hold a tight angle and punish his entry.'}",
                            f"{len(g)} opening duels")
            # --- lurkers
            pt = P[(P["map"] == m) & P["tgt"] & P["alive"] & (P["tgt_side"] == "T") & P["t"].between(25, 75)]
            if len(pt):
                lurk = self._lurkers(pt)
                for player, (share, place, rounds) in lurk.items():
                    if share >= 0.3 and rounds >= mn:
                        add(1.6 + share, m, "T", "lurk",
                            f"{self.ds.label(player)} lurks {place} in {share:.0%} of T rounds (far from the team mid-round). "
                            f"Keep a player or utility for the late flank through {place}.",
                            f"{rounds} rounds")
            # --- economy
            nxt = []
            for demo, g in rm.groupby("demo"):
                g = g.set_index("round")
                for pr in (1, 13):
                    if pr in g.index and pr + 1 in g.index and not g.loc[pr, "tgt_won"]:
                        nxt.append(g.loc[pr + 1, "buy"])
            if len(nxt) >= 3:
                force = sum(b in ("force", "half-eco") for b in nxt) / len(nxt)
                if force >= 0.5:
                    add(2.2, m, "both", "economy",
                        f"After losing a pistol they force {force:.0%} of the time. In the anti-eco, play for "
                        f"distance (rifles at range, avoid close angles), and buy armor.", f"{len(nxt)} lost pistols")
            # --- eco / force rounds
            self._lowbuy_counterplay(m, rm, P, add)
        self._force_counterplay(add)
        # --- players who die alone
        PL = self.v_players().tables[0][1]
        for _, r in PL.iterrows():
            if r["D"] >= 15 and r["traded deaths%"] <= 25:
                add(1.7, "all", "both", "isolation",
                    f"{self.ds.label(r['player'])} is rarely traded ({r['traded deaths%']:.0f}% of deaths). He plays isolated - "
                    f"pressure his area with two players and he can't be helped in time.", f"{r['D']} deaths")
        S.sort(key=lambda d: -d["priority"])
        return S

    def _anchor(self, P, zone):
        p = P[P["tgt"] & P["alive"] & (P["tgt_side"] == "CT") & P["t"].between(15, 25) & (P["zone"] == zone)]
        if not len(p):
            return ""
        vc = p.groupby("name")["key"].nunique().sort_values(ascending=False)
        return vc.index[0]

    ROLE_NAMES = {"awper": "AWPer", "awp": "AWPer", "entry": "Entry", "lurker": "Lurker", "lurk": "Lurker",
                  "support": "Support", "rifler": "Rifler", "igl": "IGL", "secondary awp": "Secondary AWP"}

    def player_roles(self):
        """Best guess at each scouted player's role from what he does in the demos (+ your overrides)."""
        K = self.ds.f("kills", side=False, phase=False, player=False)
        N = self.ds.f("nades", side=False, phase=False, player=False)
        P = self.ds.f("pos", side=False, phase=False, player=False)
        if P is None or not len(P):
            return pd.DataFrame()
        P = P[P["tgt"]]
        names = self.ds.tgt_names()
        rows = []
        # trade kills: he kills the enemy who just (<=5 s) killed a teammate
        trade_kills = Counter()
        if len(K):
            for _, g in K.sort_values("tick").groupby("key"):
                recs = g.to_dict("records")
                for i, b_ in enumerate(recs):
                    if not b_["tgt_att"]:
                        continue
                    for a_ in recs[max(0, i - 6):i]:
                        if a_["tgt_vic"] and a_["att_sid"] == b_["vic_sid"] and 0 <= b_["tick"] - a_["tick"] <= 5 * TICKRATE:
                            trade_kills[b_["att_name"]] += 1
                            break
        lurk = self._lurkers(P[P["alive"] & (P["tgt_side"] == "T") & P["t"].between(25, 75)]) if len(P) else {}
        rot, _ = self.rotation_events()
        ct_roles = self.roles()
        for n in names:
            mine = P[P["name"] == n]
            rt = mine[mine["tgt_side"] == "T"]["key"].nunique()
            rc = mine[mine["tgt_side"] == "CT"]["key"].nunique()
            kills = K[K["tgt_att"] & (K["att_name"] == n)] if len(K) else K
            ok = K[K["opening"] & ((K["tgt_att"] & (K["att_name"] == n)) | (K["tgt_vic"] & (K["vic_name"] == n)))
                   & (K["tgt_side"] == "T")] if len(K) else K
            nn = N[N["tgt"] & (N["name"] == n)] if len(N) else N
            nn_t = nn[nn["tgt_side"] == "T"] if len(nn) else nn
            awp = kills["weapon"].str.contains("awp", case=False).sum() if len(kills) else 0
            ct = ct_roles[(ct_roles["player"] == n) & (ct_roles["side"] == "CT")] if len(ct_roles) else pd.DataFrame()
            r_ = rot[(rot["name"] == n)] if len(rot) else rot
            rows.append({"player": n, "T rounds": rt, "CT rounds": rc, "kills": len(kills),
                         "AWP kill %": pct(awp, len(kills)), "T opening duels/rnd": round(len(ok) / max(1, rt), 2),
                         "T nades/rnd": round(len(nn_t) / max(1, rt), 2),
                         "enemies flashed/rnd": round(nn["enemies_flashed"].sum() / max(1, rt + rc), 2) if len(nn) else 0,
                         "trade kills %": pct(trade_kills.get(n, 0), len(kills)),
                         "lurk %": round(100 * lurk.get(n, (0, "", 0))[0]) if n in lurk else 0,
                         "CT spot": f"{ct['zone'].iloc[0]} ({ct['zone%'].iloc[0]}%)" if len(ct) else "",
                         "_ct_zone": ct["zone"].iloc[0] if len(ct) else "", "_ct_pct": ct["zone%"].iloc[0] if len(ct) else 0,
                         "rotate %": pct(r_["rotated"].sum(), len(r_)) if len(r_) else 0})
        T = pd.DataFrame(rows)
        if not len(T):
            return T
        T["role"], T["CT role"], T["why"] = "Rifler", "", ""
        valid = T["kills"] >= 10
        # AWPer(s)
        aw = T[valid & (T["AWP kill %"] >= 25)].sort_values("AWP kill %", ascending=False)
        for j, (i, r) in enumerate(aw.iterrows()):
            T.at[i, "role"] = "AWPer" if j == 0 else "Secondary AWP"
            T.at[i, "why"] = f"{r['AWP kill %']:.0f}% of kills with AWP"
        free = T["role"] == "Rifler"
        med_open = T["T opening duels/rnd"].median()
        cand = T[free & (T["T rounds"] >= 8)].sort_values("T opening duels/rnd", ascending=False)
        if len(cand) and cand.iloc[0]["T opening duels/rnd"] >= max(0.2, 1.25 * med_open):
            i = cand.index[0]
            T.at[i, "role"] = "Entry"
            T.at[i, "why"] = f"{T.at[i, 'T opening duels/rnd']:.2f} opening duels/T round (team {med_open:.2f})"
        free = T["role"] == "Rifler"
        cand = T[free & (T["lurk %"] >= 30)].sort_values("lurk %", ascending=False)
        if len(cand):
            i = cand.index[0]
            T.at[i, "role"] = "Lurker"
            T.at[i, "why"] = f"alone mid-round in {T.at[i, 'lurk %']:.0f}% of T rounds"
        free = T["role"] == "Rifler"
        score = T["T nades/rnd"] / max(0.01, T["T nades/rnd"].median()) + T["enemies flashed/rnd"] / max(0.01, T["enemies flashed/rnd"].median() or 0.01)
        cand = T[free].assign(sc=score).sort_values("sc", ascending=False)
        if len(cand) and cand.iloc[0]["sc"] >= 2.3 and cand.iloc[0]["T opening duels/rnd"] <= med_open:
            i = cand.index[0]
            T.at[i, "role"] = "Support"
            T.at[i, "why"] = f"most utility ({T.at[i, 'T nades/rnd']:.2f} nades/T round), few entries"
        for i, r in T.iterrows():
            if r["role"] == "Rifler" and not r["why"]:
                T.at[i, "why"] = "no single trait stands out"
            z, zp = r["_ct_zone"], r["_ct_pct"]
            ctr = f"{z} anchor" if z in ("A", "B") and zp >= 65 else ("Mid" if z == "Mid" else (f"{z} side" if z else ""))
            if r["rotate %"] >= 35:
                ctr = (ctr + " / rotator").strip(" /")
            T.at[i, "CT role"] = ctr
        # IGL: not visible in a demo -> FACEIT captain as a hint, your overrides win
        try:
            tj = json.loads((self.ds.ws.team_root / "team.json").read_text())
            leader = tj.get("leader", "")
        except Exception:
            leader = ""
        T["IGL"] = ""
        if leader:
            T.loc[T["player"].str.lower() == leader.lower(), "IGL"] = "likely (CSC captain)"
        try:
            ov = json.loads((self.ds.ws.team_root / "roles.json").read_text())
        except Exception:
            ov = {}
        cur_map = self.cfg["MAP"]
        for i, r in T.iterrows():
            o = dict(ov.get(r["player"].lower(), {}))
            if cur_map:
                o.update(o.get("maps", {}).get(cur_map, {}))
            if o.get("role"):
                T.at[i, "role"], T.at[i, "why"] = o["role"], "set by you"
            if o.get("igl") is not None:
                T.at[i, "IGL"] = "yes (set by you)" if o["igl"] else ""
            if o.get("ct"):
                T.at[i, "CT role"] = o["ct"]
        T["roster"] = [self.ds.status_of(n) for n in T["player"]]
        return T.drop(columns=["_ct_zone", "_ct_pct"])

    def roles_by_map(self):
        """player_roles() for every map in scope: one row per player per map (with a `map` column)."""
        saved, out = self.cfg.v["MAP"], []
        try:
            for m in self.maps_in_scope():
                self.cfg.v["MAP"] = m
                T = self.player_roles()
                if len(T):
                    out.append(T.assign(map=m))
        finally:
            self.cfg.v["MAP"] = saved
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame()

    # ------------------------------------------------------------------ power positions
    POWER_WINDOW = (0, 70)  # seconds after freeze end: the live part of the round, before plant/retake chaos
    POWER_SPAWN_SKIP = 20   # time in CT/T spawn during the first 20 s doesn't count (everyone starts there)

    def power_positions(self):
        """Each player's power position per map and half: the callout where he spends most of the live round
        (first 70 s, pre-plant, alive; time in CT/T spawn during the first 20 s is ignored), the % of his rounds on
        that half he plays it, and his K-D there."""
        P = self.ds.f("pos", side=True, phase=False, player=True)
        if P is None or not len(P):
            return pd.DataFrame()
        P = P[P["tgt"] & P["alive"] & P["t"].between(*self.POWER_WINDOW) & ~P["post_plant"]]
        in_spawn = P["place"].astype(str).str.lower().str.contains("spawn")
        P = P[(P["place"].astype(str) != "") & ~(in_spawn & (P["t"] < self.POWER_SPAWN_SKIP))]
        if not len(P):
            return pd.DataFrame()
        # his spot each round = where he spent the most samples
        per = (P.groupby(["map", "tgt_side", "name", "key", "place"]).size().reset_index(name="n")
               .sort_values("n", ascending=False).drop_duplicates(["map", "tgt_side", "name", "key"]))
        K = self.ds.f("kills", side=True, phase=False, player=False)
        rows = []
        for (m, side, n), g in per.groupby(["map", "tgt_side", "name"]):
            rounds = g["key"].nunique()
            if rounds < 3:
                continue
            vc = g["place"].value_counts()
            top, cnt = vc.index[0], int(vc.iloc[0])
            second = f"{show_place(m, vc.index[1])} ({pct(int(vc.iloc[1]), rounds):.0f}%)" if len(vc) > 1 else ""
            keys = set(g[g["place"] == top]["key"])
            kills = deaths = 0
            if K is not None and len(K):
                km = K[K["key"].isin(keys)]
                kills = int(((km["att_name"] == n) & km["tgt_att"] & (km["a_place"] == top)).sum())
                deaths = int(((km["vic_name"] == n) & km["tgt_vic"] & (km["v_place"] == top)).sum())
            pts = P[(P["map"] == m) & (P["tgt_side"] == side) & (P["name"] == n) & (P["place"] == top)]
            rows.append({"map": m, "player": n, "half": f"{side} half", "power position": top,
                         "% of rounds": pct(cnt, rounds), "rounds": rounds,
                         "K-D there": f"{kills}-{deaths}", "next most": second,
                         "_x": pts["x"].median(), "_y": pts["y"].median(), "_z": pts["z"].median(), "_side": side})
        T = pd.DataFrame(rows)
        if not len(T):
            return T
        T["roster"] = [self.ds.status_of(n) for n in T["player"]]
        order = {n: i for i, n in enumerate(self.ds.tgt_names(all_players=True))}
        T["_o"] = T["player"].map(order).fillna(99)
        return T.sort_values(["map", "_side", "_o"], ascending=[True, False, True]).drop(columns="_o").reset_index(drop=True)

    def v_power(self):
        v = View("power", "Power positions - each player's spot per map and half")
        T = self.power_positions()
        if not len(T):
            v.note("No data in scope.")
            return v
        cols = ["map", "half", "player", "roster", "power position", "% of rounds", "K-D there", "next most", "rounds"]
        v.table("Power positions (where each player spends most of the live round, % of his rounds on that half)", T[cols])
        for (m, half), g in T.groupby(["map", "half"]):
            locked = g[g["% of rounds"] >= 70]
            if len(locked):
                v.note(f"{m} {half}: " + ", ".join(f"{r['player']} is at {show_place(m, r['power position'])} "
                                                   f"{r['% of rounds']:.0f}% of rounds" for _, r in locked.iterrows())
                       + " - predictable, pre-aim / utility those spots.")

        def plot(out):
            paths_ = []
            for m in sorted(T["map"].unique()):
                for side in ("T", "CT"):
                    g = T[(T["map"] == m) & (T["_side"] == side)]
                    if not len(g):
                        continue
                    layers, labels = [], []
                    for i, (_, r) in enumerate(g.iterrows()):
                        col = self.PLAYER_COLORS[i % len(self.PLAYER_COLORS)]
                        layers.append(dict(x=[r["_x"]], y=[r["_y"]], z=[r["_z"]], color=col, size=260, marker="o",
                                           label=f"{r['player']} - {show_place(m, r['power position'])} {r['% of rounds']:.0f}%",
                                           count=False))
                        labels.append((r["_x"], r["_y"], r["_z"], f"{r['player']} {r['% of rounds']:.0f}%"))
                    paths_.append(map_plot(self.radar, m, f"{m} - power positions, {side} half",
                                           out / f"power_{side}_{m}_{self.fname()}.png", layers=layers, labels=labels,
                                           style="points", subtitle=self.scope_label() + "  |  % = his rounds on that half"))
            return paths_
        v.plots.append(("power positions", plot))
        return v

    def v_roles(self):
        v = View("roles", "Player roles")
        maps = self.maps_in_scope()
        if not self.cfg["MAP"] and len(maps) > 1:
            B = self.roles_by_map()
            if not len(B):
                v.note("No data in scope.")
                return v
            B["cell"] = B["role"] + np.where(B["CT role"] != "", " | CT " + B["CT role"], "")
            grid = B.pivot_table(index="player", columns="map", values="cell", aggfunc="first").fillna("-").reset_index()
            grid.columns.name = None
            grid.insert(1, "roster", [self.ds.status_of(n) for n in grid["player"]])
            v.table("Roles by map (T role | CT role) - best guess from the demos, `role set` to correct", grid)
            v.table("Why (per map)", B[["map", "player", "role", "CT role", "why"]].sort_values(["map", "player"]))
            T = self.player_roles()
            if len(T):
                v.table("All maps together", T[["player", "roster", "role", "CT role", "IGL", "why"]])
            v.note("IGL can't be seen in demo data: the CSC team captain is marked as the likely IGL. "
                   "Fix any guess with: role set <player> <AWPer|Entry|Support|Lurker|Rifler|IGL|not IGL> [map]")
            return v
        T = self.player_roles()
        if not len(T):
            v.note("No data in scope.")
            return v
        v.table("Roles" + (f" on {self.cfg['MAP'] or maps[0]}" if maps else "") + " (best guess from the demos - `role set` to correct)",
                T[["player", "roster", "role", "CT role", "IGL", "why"]])
        v.table("The numbers behind it", T[["player", "T rounds", "CT rounds", "AWP kill %", "T opening duels/rnd",
                                            "T nades/rnd", "enemies flashed/rnd", "trade kills %", "lurk %", "CT spot", "rotate %"]])
        v.note("IGL can't be seen in demo data: the CSC team captain is marked as the likely IGL. "
               "Fix any guess with: role set <player> <AWPer|Entry|Support|Lurker|Rifler|IGL|not IGL> [map]")
        return v

    def _lurkers(self, pt):
        res = {}
        cnt = Counter()
        places = defaultdict(Counter)
        total = pt.groupby("name")["key"].nunique()
        for (key, tick), g in pt.groupby(["key", "tick"]):
            if len(g) < 3:
                continue
            xy = g[["x", "y"]].to_numpy(float)
            for i, (n, pl) in enumerate(zip(g["name"], g["place"])):
                others = np.delete(xy, i, axis=0)
                if np.min(np.hypot(*(others - xy[i]).T)) > 1400:
                    cnt[(n, key)] += 1
                    places[n][pl] += 1
        rounds = Counter(n for (n, key), c in cnt.items() if c >= 3)
        for n, r in rounds.items():
            res[n] = (r / max(1, total.get(n, 1)), places[n].most_common(1)[0][0] if places[n] else "?", r)
        return res

    def fname(self):
        bits = [self.cfg[k] for k in ("SIDE", "PHASE", "PLAYER") if self.cfg[k]]
        if self.cfg["LAST_N"]:
            bits.append(f"last{self.cfg['LAST_N']}")
        return safe_name("_".join(bits) or "all").replace(" ", "_")


def veto_tables(vetos, team="They"):
    """Map tables from veto records, sorted most -> least."""
    played, bans, first_bans, picks, opp_bans, opp_picks, deciders, inpool = (Counter() for _ in range(8))
    wins = Counter()
    have_steps = sum(1 for v in vetos if v["steps"])
    for v in vetos:
        for mp in v["pool"]:
            inpool[mp] += 1
        for mp, r in v["results"].items():
            played[mp] += 1
            wins[mp] += bool(r["won"])
        if not v["results"]:
            for mp in v["picks"]:
                played[mp] += 1
        first = True
        for st in v["steps"]:
            if st["random"]:
                continue
            if st["action"] == "drop":
                if st["by"] == "us":
                    bans[st["map"]] += 1
                    if first:
                        first_bans[st["map"]] += 1
                    first = False
                elif st["by"] == "them":
                    opp_bans[st["map"]] += 1
            elif st["action"] == "pick":
                (picks if st["by"] == "us" else opp_picks)[st["map"]] += 1
        if v["steps"] and v["picks"]:
            chosen = {st["map"] for st in v["steps"] if st["action"] == "pick"}
            for mp in v["picks"]:
                if mp not in chosen:
                    deciders[mp] += 1
    maps = sorted(set(inpool) | set(played) | set(bans) | set(opp_bans))
    n = len(vetos)
    rows = []
    for mp in maps:
        rows.append({"map": mp, "played": played[mp], "W-L": f"{wins[mp]}-{played[mp] - wins[mp]}" if played[mp] else "-",
                     "win%": round(100 * wins[mp] / played[mp]) if played[mp] else None,
                     "their picks": picks[mp], "opp picks": opp_picks[mp], "deciders": deciders[mp],
                     "their bans": bans[mp], "first bans": first_bans[mp], "opp bans": opp_bans[mp],
                     "in pool": inpool[mp]})
    T = pd.DataFrame(rows)
    out = {}
    if len(T):
        out["played"] = T[T["played"] > 0].sort_values(["played", "win%"], ascending=[False, False])[
            ["map", "played", "W-L", "win%", "their picks", "opp picks", "deciders"]]
        if have_steps:
            out["bans"] = T[T["their bans"] > 0].sort_values(["their bans", "first bans"], ascending=False)[
                ["map", "their bans", "first bans", "in pool"]].assign(
                **{"% of vetos": lambda d: (100 * d["their bans"] / have_steps).round()})
            out["picks"] = T[T["their picks"] > 0].sort_values("their picks", ascending=False)[["map", "their picks", "played", "win%"]]
            out["opp_bans"] = T[T["opp bans"] > 0].sort_values("opp bans", ascending=False)[["map", "opp bans"]]
        else:  # no ban order available: maps in the pool they never end up on are their likely bans
            T["never played %"] = (100 * (T["in pool"] - T["played"]).clip(lower=0) / T["in pool"].replace(0, np.nan)).round()
            out["likely_bans"] = T[(T["in pool"] > 0)].sort_values(["never played %", "played"], ascending=[False, True])[
                ["map", "in pool", "played", "never played %"]]
    return out, have_steps, n


def veto_suggestions(tabs, have_steps, n):
    tips = []
    if not n:
        return tips
    if "bans" in tabs and len(tabs["bans"]):
        b = tabs["bans"].iloc[0]
        tips.append(f"They ban {b['map']} in {b['their bans']}/{have_steps} vetos"
                    + (f" ({b['first bans']}x as their first ban)" if b["first bans"] else "")
                    + " - don't plan your veto around playing it.")
    if "likely_bans" in tabs and len(tabs["likely_bans"]):
        lb = tabs["likely_bans"]
        never = lb[(lb["played"] == 0) & (lb["in pool"] >= 2)]["map"].tolist()
        if never:
            tips.append(f"They never ended up on {', '.join(m.replace('de_', '') for m in never)} in {n} vetos - "
                        f"their permabans are among these (some will be opponents' bans).")
    pl = tabs.get("played")
    if pl is not None and len(pl):
        top = pl.iloc[0]
        tips.append(f"Their most played map is {top['map']} ({top['played']}x, {top['W-L']}) - ban it unless it's your best map.")
        weak = pl[(pl["played"] >= 2) & pl["win%"].notna()].sort_values("win%")
        if len(weak) and weak.iloc[0]["win%"] <= 45 and weak.iloc[0]["map"] != top["map"]:
            w = weak.iloc[0]
            tips.append(f"Their weakest map is {w['map']} ({w['W-L']}, {w['win%']:.0f}%) - steer the veto toward it.")
    if "picks" in tabs and len(tabs["picks"]):
        p = tabs["picks"].iloc[0]
        tips.append(f"When they get a pick they take {p['map']} ({p['their picks']}x).")
    return tips


def top1(s):
    s = pd.Series(s)
    s = s[s.astype(str) != ""]
    return s.value_counts().index[0] if len(s) else ""


# =============================================================================
#  Report
# =============================================================================
REPORT_CSS = """
:root{--bg:#0f1216;--card:#171b21;--ink:#e8edf2;--mut:#93a0ac;--acc:#4dabf7;--good:#3ddc84;--bad:#ff6b6b;--line:#262c34}
@media (prefers-color-scheme: light){:root{--bg:#f5f7f9;--card:#fff;--ink:#14181d;--mut:#5b6670;--line:#e1e6eb}}
body{background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}
main{max-width:1180px;margin:auto}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:32px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
h3{font-size:15px;margin:20px 0 8px;color:var(--mut)}.sub{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:6px 0 14px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--mut);font-weight:600}.wrap td{white-space:normal}.tw{overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px}.grid img{width:100%;border-radius:8px;border:1px solid var(--line)}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:9px;background:#4dabf722;color:var(--acc);margin-right:6px}
li{margin:4px 0}nav a{color:var(--acc);margin-right:12px;text-decoration:none}
.toc div{margin:6px 0}.toc a{text-decoration:none}.toc .tp{color:var(--acc);font-weight:700;margin-right:10px}
.toc .ts{display:inline-block;background:#4dabf722;color:var(--ink);border-radius:9px;padding:1px 9px;margin:2px 3px;font-size:12.5px}
h2 .up{font-size:13px;color:var(--mut);text-decoration:none;margin-left:6px}
.grid img{cursor:zoom-in}#lb{display:none;position:fixed;inset:0;background:#000d;z-index:9;align-items:center;justify-content:center;cursor:zoom-out}
#lb img{max-width:96vw;max-height:96vh;border-radius:8px}#lb.on{display:flex}
"""
REPORT_JS = """<div id='lb' onclick="this.classList.remove('on')"><img alt=''></div><script>
document.querySelectorAll('.grid img').forEach(i=>i.onclick=()=>{const lb=document.getElementById('lb');lb.querySelector('img').src=i.src;lb.classList.add('on')});
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.getElementById('lb').classList.remove('on')});</script>"""


PLACE_COLS = {"place", "power position", "from", "lands at", "callout", "top FK spot", "top FD spot", "a_place", "v_place", "l_place",
              "t_place", "plant_place", "went to"}
TEXT_COLS = {"suggestion", "text", "evidence", "veto", "detail"}
GENERIC_NAMES = {"bombsitea": "A Site", "bombsiteb": "B Site", "bombsitec": "C Site", "ctspawn": "CT Spawn",
                 "tspawn": "T Spawn", "topofmid": "Top Mid", "middle": "Mid", "tramp": "T Ramp", "ctramp": "CT Ramp"}
# Valve area name -> what players call it, where the two differ. Kept to well-established equivalents;
# "Human (Valve)" keeps Valve's name visible so a wrong default is easy to spot. `rename` overrides any of these.
DEFAULT_ALIASES = {
    "de_mirage": {"Catwalk": "B Short (Catwalk)", "PalaceAlley": "Palace Alley", "Apartments": "B Apartments",
                  "SideAlley": "Side Alley", "BackAlley": "Back Alley", "SnipersNest": "Sniper's Nest"},
    "de_dust2": {"LongDoors": "Long Doors", "OutsideLong": "Outside Long", "LongA": "A Long",
                 "ShortStairs": "Short Stairs", "Catwalk": "Short (Catwalk)", "UpperTunnel": "Upper Tunnels",
                 "LowerTunnel": "Lower Tunnels", "BDoors": "B Doors", "CTMid": "CT Mid", "ExtendedA": "A Long (ExtendedA)"},
    "de_inferno": {"SecondMid": "Second Mid", "LowerMid": "Lower Mid", "TopofMid": "Top Mid"},
    "de_nuke": {"Tunnels": "Tunnels", "Silo": "Silo", "Hut": "Hut"},
    "de_train": {"PopDog": "Pop Dog", "BombsiteA": "A Site", "BombsiteB": "B Site"},
}


def tidy_place(raw):
    """Valve area name -> readable: TopofMid -> Top of Mid, BombsiteA -> A Site, CTSpawn -> CT Spawn."""
    r = str(raw or "")
    if not r:
        return r
    if r.lower() in GENERIC_NAMES:
        return GENERIC_NAMES[r.lower()]
    t = re.sub(r"(?<=[a-z])(of|to|the|and)(?=[A-Z])", r" \1 ", r)
    t = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", t)
    t = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", t)
    t = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", t)
    t = re.sub(r"\bBombsite ([AB])\b", r"\1 Site", t)
    return re.sub(r"\s+", " ", t).strip()


class CalloutNames:
    """Display names for Valve's area names: your `rename`s first, then an automatic tidy-up.
    The data keeps Valve's names; only what's shown changes."""

    def __init__(self, root: Path, cfg):
        self.path = root / "callout_names.json"
        self.cfg = cfg
        self.known = set()
        try:
            self.user = json.loads(self.path.read_text())
        except Exception:
            self.user = {}
        self._rx = None

    def on(self):
        return str(self.cfg["CALLOUT_NAMES"]).lower() != "raw"

    def save(self):
        self.path.write_text(json.dumps(self.user, indent=2, sort_keys=True))
        self._rx = None

    def learn(self, places, m=None):
        new = {p for p in places if p} - self.known
        if m:  # remember which maps use each area name (same name on two maps = can't guess in free text)
            self.by_map = getattr(self, "by_map", defaultdict(set))
            for p_ in places:
                if p_:
                    self.by_map[p_].add(m)
            self._rx = None
        if new:
            self.known |= new
            self._rx = None

    def pretty(self, m, raw):
        raw = "" if raw is None or (isinstance(raw, float) and math.isnan(raw)) else str(raw)
        if not raw or not self.on():
            return raw
        if m and raw in self.user.get(m, {}):
            return self.user[m][raw]
        if m and raw in DEFAULT_ALIASES.get(m, {}):
            return DEFAULT_ALIASES[m][raw]
        if not m:  # no map known: use a name only if every map that has this area agrees on it
            maps_with = getattr(self, "by_map", {}).get(raw, set())
            named = {mm for mm, d in self.user.items() if raw in d} | {mm for mm, d in DEFAULT_ALIASES.items() if raw in d}
            if maps_with - named:  # it also exists on a map that keeps the plain name -> don't guess
                return tidy_place(raw)
            vals = {d[raw] for d in self.user.values() if raw in d}
            if not vals:
                vals = {d[raw] for d in DEFAULT_ALIASES.values() if raw in d}
            if len(vals) == 1:
                return vals.pop()
        return tidy_place(raw)

    def source(self, m, raw):
        if raw in self.user.get(m, {}):
            return "yours"
        if raw in DEFAULT_ALIASES.get(m, {}):
            return "default"
        return "auto" if tidy_place(raw) != raw else ""

    def text(self, s):
        """Swap Valve names inside a sentence."""
        if not s or not self.on() or not isinstance(s, str):
            return s
        if self._rx is None:
            names = sorted({n for n in self.known | {k for d in self.user.values() for k in d}
                            | {k for d in DEFAULT_ALIASES.values() for k in d}
                            if n and self.pretty(None, n) != n}, key=len, reverse=True)
            self._rx = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else False
        return self._rx.sub(lambda m_: self.pretty(None, m_.group(1)), s) if self._rx else s

    def df(self, df):
        if df is None or not len(df):
            return df
        df = df.rename(columns=COL_LABELS)
        if not self.on():
            return df
        d = df.copy()
        for c in d.columns:
            if c in PLACE_COLS:
                if "map" in d.columns:
                    d[c] = [self.pretty(m, v) for m, v in zip(d["map"], d[c])]
                else:
                    d[c] = [self.pretty(None, v) for v in d[c]]
            elif c in TEXT_COLS:
                d[c] = [self.text(v) for v in d[c]]
        return d

    def matches(self, m, raw, want):
        """Does a typed name match this area (Valve name or display name, loosely)?"""
        key = re.sub(r"[^a-z0-9]", "", want.lower())
        for cand in (raw, self.pretty(m, raw)):
            if key and key in re.sub(r"[^a-z0-9]", "", str(cand).lower()):
                return True
        return False


DISPLAY = None  # CalloutNames, set by the console
COL_LABELS = {"t_med": "median time (s)"}  # internal column name -> what's shown


def show_names(df):
    if DISPLAY is not None:
        return DISPLAY.df(df)
    return df.rename(columns=COL_LABELS) if df is not None and len(df) else df


def show_text(s):
    return DISPLAY.text(s) if DISPLAY is not None else s


def show_place(m, raw):
    return DISPLAY.pretty(m, raw) if DISPLAY is not None else raw


def df_html(df, max_rows=40, wrap=False):
    if df is None or not len(df):
        return "<p class='sub'>No data.</p>"
    df = show_names(df)
    return f"<div class='tw{' wrap' if wrap else ''}'>" + df.head(max_rows).to_html(index=False, escape=True, na_rep="-",
                                                                                   float_format=lambda x: f"{x:.1f}", border=0) + "</div>"


def render_report_html(team, pages, maps):
    h = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{html.escape(str(team))} scouting report</title><style>{report_css()}</style></head><body><main>"]
    logo_done = False
    team_lg = ""
    if THEME.get("team_logo") and Path(THEME["team_logo"]).exists():
        team_lg = (f"<img class='logo teamlogo' alt='team logo' src='data:image/png;base64,"
                   f"{base64.b64encode(Path(THEME['team_logo']).read_bytes()).decode()}'>")
    esc = lambda t: html.escape(str(t))
    has_cover = bool(pages) and bool(pages[0][1]) and pages[0][1][0][0] == "cover"
    top = "cover" if has_cover else "p0"
    for name, blocks in pages:
        cover_pg = bool(blocks) and blocks[0][0] == "cover"
        h.append("<section class='cover'>" if cover_pg else f"<section>{team_lg}")
        for b in blocks:
            if b[0] == "cover":
                brand = ""
                if THEME.get("logo") and Path(THEME["logo"]).exists() and THEME.get("logo") != THEME.get("team_logo"):
                    mime = "image/png" if THEME["logo"].lower().endswith(".png") else "image/jpeg"
                    brand = (f"<img class='logo' alt='logo' src='data:{mime};base64,"
                             f"{base64.b64encode(Path(THEME['logo']).read_bytes()).decode()}'>")
                logo_done = True
                big = (f"<img class='coverlogo' alt='team logo' src='data:image/png;base64,"
                       f"{base64.b64encode(Path(THEME['team_logo']).read_bytes()).decode()}'>") if team_lg else ""
                h.append(f"{brand}<h1 id='{b[3]}' class='covertitle'>{esc(b[1])}</h1><p class='sub'>{esc(b[2])}</p>{big}")
                continue
            if b[0] == "title":
                lg = ""
                if not logo_done and THEME.get("logo") and Path(THEME["logo"]).exists() and THEME.get("logo") != THEME.get("team_logo"):
                    mime = "image/png" if THEME["logo"].lower().endswith(".png") else "image/jpeg"
                    lg = (f"<img class='logo' alt='logo' src='data:{mime};base64,"
                          f"{base64.b64encode(Path(THEME['logo']).read_bytes()).decode()}'>")
                logo_done = True
                h.append(f"{lg}<h1 id='{b[3] if len(b) > 3 else ''}'>{esc(b[1])}</h1><p class='sub'>{esc(b[2])}</p>")
            elif b[0] == "index":
                h.append("<div class='card toc'>" + "".join(
                    f"<div><a class='tp' href='#{a}'>{esc(t)}</a> " + " ".join(f"<a class='ts' href='#{sa}'>{esc(st)}</a>" for st, sa in secs)
                    + "</div>" for t, a, secs in b[1]) + "</div>")
            elif b[0] == "h2":
                h.append(f"<h2 id='{b[2] if len(b) > 2 else ''}'>{esc(b[1])} <a class='up' href='#{top}' title='back to contents'>&#8593;</a></h2>")
            elif b[0] == "h3":
                h.append(f"<h3>{esc(b[1])}</h3>")
            elif b[0] == "bullets":
                h.append("<div class='card'><ul>" + "".join(
                    "<li>" + "".join(f"<span class='tag'>{esc(t)}</span>" for t in tags) + esc(text)
                    + (f" <span class='sub'>({esc(ev)})</span>" if ev else "") + "</li>" for tags, text, ev in b[1]) + "</ul></div>")
            elif b[0] == "table":
                h.append(df_html(b[1], b[2]))
            elif b[0] == "links":
                h.append("<div class='card'><ul>" + "".join(
                    "<li>" + (f"<a href='{esc(u)}' target='_blank' rel='noopener'>{esc(t)}</a>" if u else esc(t))
                    + (f" <span class='sub'>({esc(n)})</span>" if n else "") + "</li>" for t, u, n in b[1]) + "</ul></div>")
            elif b[0] == "images":
                h.append("<div class='grid'>" + "".join(img_tag(p) for p in b[1]) + "</div>")
        h.append("</section>")
    if THEME.get("footer"):
        h.append(f"<footer>{esc(THEME['footer'])}</footer>")
    h.append("</main>" + REPORT_JS + "</body></html>")
    return "\n".join(h)



# =============================================================================
#  Report themes  (`style` command / REPORT_THEME option / report_style.json)
# =============================================================================
THEMES = {
    "midnight": dict(label="Midnight - dark grey, blue accent (original look)",
                     bg="#0f1216", card="#171b21", ink="#e8edf2", mut="#93a0ac", acc="#4dabf7", line="#262c34"),
    "blackout": dict(label="Blackout - pure black, white text, amber accent",
                     bg="#000000", card="#0e0e0e", ink="#f2f2f2", mut="#9a9a9a", acc="#ffb020", line="#262626"),
    "carbon":   dict(label="Carbon - charcoal, gold accent",
                     bg="#141414", card="#1d1d1d", ink="#ece8df", mut="#a39e93", acc="#e3b341", line="#2e2e2e"),
    "crimson":  dict(label="Crimson - dark slate, red accent",
                     bg="#12131a", card="#1a1c25", ink="#ecebf2", mut="#9a99a8", acc="#ef4444", line="#2a2c38"),
    "emerald":  dict(label="Emerald - deep green-black, green accent",
                     bg="#0c1412", card="#13201c", ink="#e6f2ed", mut="#8fa89f", acc="#34d399", line="#22332d"),
    "navy":     dict(label="Navy - navy blue, cyan accent",
                     bg="#0a1628", card="#10213a", ink="#e7eef8", mut="#8ea3bf", acc="#38bdf8", line="#1d3352"),
    "light":    dict(label="Light - white pages, blue accent (good on screen)",
                     bg="#f6f7f9", card="#ffffff", ink="#15191e", mut="#5b6670", acc="#1c6fd1", line="#dde2e8"),
    "print":    dict(label="Print - white, black text, little ink (for printing)",
                     bg="#ffffff", card="#ffffff", ink="#000000", mut="#4a4a4a", acc="#000000", line="#bdbdbd"),
}

# Report typefaces: primary (titles, text) = Arial, technical (tables, numbers) = Courier New.
# Metric-compatible stand-ins (Liberation) are used where the Microsoft fonts aren't installed.
REPORT_FONTS = {
    "primary":        ["arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf", "Arimo-Regular.ttf"],
    "primary_bold":   ["arialbd.ttf", "Arial Bold.ttf", "Arial-BoldMT.ttf", "LiberationSans-Bold.ttf", "Arimo-Bold.ttf"],
    "technical":      ["cour.ttf", "Courier New.ttf", "LiberationMono-Regular.ttf", "Cousine-Regular.ttf"],
    "technical_bold": ["courbd.ttf", "Courier New Bold.ttf", "LiberationMono-Bold.ttf", "Cousine-Bold.ttf"],
}
_FONT_CACHE = {}


def find_font(kind):
    """Path of an installed font file for a report typeface, or '' (then DejaVu, bundled with matplotlib, is used)."""
    if kind in _FONT_CACHE:
        return _FONT_CACHE[kind]
    flat = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
            Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"), Path.home() / "Library" / "Fonts"]
    nested = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts", Path.home() / ".local/share/fonts"]
    found = ""
    for name in REPORT_FONTS.get(kind, []):
        for d in flat:
            if (d / name).is_file():
                found = str(d / name)
                break
        if not found:
            for d in nested:
                if d.is_dir():
                    hit = next(iter(d.rglob(name)), None)
                    if hit:
                        found = str(hit)
                        break
        if found:
            break
    _FONT_CACHE[kind] = found
    return found


THEME_KEYS = ("bg", "card", "ink", "mut", "acc", "line", "tag_bg", "row_alt", "head")
THEME = {}          # the active theme (filled by apply_theme)
DEFAULT_FOOTER = "Tool by xDiedrich and Brian"
STYLE_FILE_NAME = "report_style.json"
STYLE_TEMPLATE = {
    "_help": "Tweaks on top of the selected theme (`style <name>`). Delete a line to use the theme's value. "
             "Colors are hex like #ff8800. font/font_bold: a .ttf file. logo: a .png/.jpg shown on page 1. "
             "footer: small text at the bottom of every page.",
    "colors": {"acc": "", "head": "", "bg": "", "card": "", "ink": "", "mut": "", "line": ""},
    "font": "", "font_bold": "", "logo": "", "footer": ""}


def _hex(c):
    c = str(c).lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    """Blend hex color a toward b by t (0..1)."""
    x, y = _hex(a), _hex(b)
    return "#" + "".join(f"{round(p + (q - p) * t):02x}" for p, q in zip(x, y))


def _is_light(c):
    r, g, b = _hex(c)
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150


def load_theme(name="midnight", style_path=None):
    """Theme dict = preset + your report_style.json tweaks, with the derived colors filled in."""
    t = dict(THEMES.get((name or "midnight").lower(), THEMES["midnight"]))
    t["name"] = (name or "midnight").lower() if (name or "").lower() in THEMES else "midnight"
    t.update(font="", font_bold="", logo="", footer=DEFAULT_FOOTER)
    if style_path and Path(style_path).exists():
        try:
            st = json.loads(Path(style_path).read_text(encoding="utf-8"))
            for k, v in (st.get("colors") or {}).items():
                if k in THEME_KEYS and re.fullmatch(r"#?[0-9a-fA-F]{6}", str(v or "")):
                    t[k] = "#" + str(v).lstrip("#")
            for k in ("font", "font_bold", "logo", "footer"):
                if st.get(k):
                    t[k] = str(st[k])
            if str(st.get("footer", "")).strip().lower() in ("off", "none"):
                t["footer"] = ""
            t["custom"] = True
        except Exception as e:
            warn(f"{STYLE_FILE_NAME} couldn't be read ({e}) - using the plain {t['name']} theme.")
    light = _is_light(t["bg"])
    t.setdefault("tag_bg", _mix(t["card"], t["acc"], 0.22))
    t.setdefault("head", _mix(t["acc"], t["ink"], 0.2))  # section headers: a softer shade of the accent
    t.setdefault("row_alt", _mix(t["bg"], t["ink"], 0.035 if not light else 0.025))
    t["light"] = light
    return t


def apply_theme(t):
    global THEME
    THEME = t
    _Page.BG, _Page.CARD, _Page.INK, _Page.MUT, _Page.ACC, _Page.LINE = (t[k] for k in ("bg", "card", "ink", "mut", "acc", "line"))
    _Page.TAG_BG, _Page.ROW_ALT = t["tag_bg"], t["row_alt"]


def report_css(t=None):
    t = t or THEME or load_theme()
    return (f":root{{--bg:{t['bg']};--card:{t['card']};--ink:{t['ink']};--mut:{t['mut']};--acc:{t['acc']};"
            f"--line:{t['line']};--tag:{t['tag_bg']};--alt:{t['row_alt']};--head:{t['head']};--good:#3ddc84;--bad:#ff6b6b}}\n"
            + REPORT_CSS.split("\n", 3)[3].replace("#4dabf722", "var(--tag)")
            + "\nbody{font-family:Arial,'Liberation Sans',Helvetica,sans-serif}"
            + "\ntr:nth-child(even) td{background:var(--alt)}.logo{float:right;max-height:64px;max-width:240px;margin-left:16px}"
              "\nsection{clear:both}.teamlogo{margin-top:18px}h2{color:var(--head)}"
              "\n.cover{text-align:center;padding:40px 0 10px}.covertitle{font-size:38px;margin:10px 0 6px}"
              "\n.coverlogo{display:block;margin:28px auto;max-width:280px;max-height:280px}"
              "\n.cover .toc{text-align:left;max-width:900px;margin:20px auto}.cover .toc .tp{font-size:18px}"
              "\nfooter{color:var(--mut);font-size:12px;margin:40px 0 0;border-top:1px solid var(--line);padding-top:10px}"
            + ("\n@media print{body{background:#fff;color:#000}}" if t["name"] == "print" else ""))

class _Page:
    """Minimal page layout on PIL: headings, wrapped bullets with tag chips, tables, image grids."""
    W, M = 1800, 56
    BG, CARD, INK, MUT, ACC, LINE = "#0f1216", "#171b21", "#e8edf2", "#93a0ac", "#4dabf7", "#262c34"
    TAG_BG, ROW_ALT = "#1f3a52", "#141820"

    def __init__(self):
        from PIL import ImageFont
        import matplotlib
        ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        reg = find_font("primary") or str(ttf / "DejaVuSans.ttf")
        bold = find_font("primary_bold") or str(ttf / "DejaVuSans-Bold.ttf")
        mono = find_font("technical") or str(ttf / "DejaVuSansMono.ttf")
        mono_b = find_font("technical_bold") or str(ttf / "DejaVuSansMono-Bold.ttf")
        for key, default in (("font", reg), ("font_bold", bold)):  # your own .ttf from report_style.json
            want = (THEME or {}).get(key)
            if want and Path(want).exists():
                if key == "font":
                    reg = want
                    bold = want if not (THEME or {}).get("font_bold") else bold
                else:
                    bold = want

        def f(path, size):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                return ImageFont.truetype(str(ttf / ("DejaVuSans-Bold.ttf" if path == bold else "DejaVuSans.ttf")), size)
        self.f = {"title": f(bold, 44), "h2": f(bold, 30), "h3": f(bold, 21), "body": f(reg, 19), "small": f(reg, 16),
                  "tag": f(bold, 14), "th": f(bold, 15), "td": f(reg, 15)}
        self.parts = []  # rendered block images
        self.meta = []   # per part: {"anchors": {name: y}, "links": [(x0, y0, x1, y1, target)]}

    def _add(self, im, anchors=None, links=None):
        self.parts.append(im)
        self.meta.append({"anchors": anchors or {}, "links": links or [], "title": getattr(self, "_kind", "") == "title"})
        self._kind = ""

    def _canvas(self, h):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (self.W, max(1, int(h))), self.BG)
        return im, ImageDraw.Draw(im)

    def _wrap(self, d, text, font, width):
        lines, cur = [], ""
        for word in str(text).split():
            t = (cur + " " + word).strip()
            if d.textlength(t, font=font) <= width or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = word
        return lines + ([cur] if cur else [])

    def cover(self, title, sub, logo_path=None, anchor=None):
        """Cover block: big centered title, subtitle, then the team logo centered underneath."""
        from PIL import Image, ImageDraw, ImageFont
        d0 = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        tf = self.f["title"].font_variant(size=64) if hasattr(self.f["title"], "font_variant") else self.f["title"]
        sf = self.f["body"]
        lines = self._wrap(d0, title, tf, self.W - 2 * self.M)
        lg = None
        if logo_path and Path(logo_path).exists():
            try:
                lg = Image.open(logo_path).convert("RGBA")
                side = 420
                sc = min(side / lg.width, side / lg.height)
                lg = lg.resize((max(1, int(lg.width * sc)), max(1, int(lg.height * sc))))
            except Exception:
                lg = None
        th = 70 + len(lines) * (tf.size + 14) + 14 + sf.size + 30 + ((lg.height + 50) if lg else 0)
        im, d = self._canvas(th)
        y = 70
        for ln in lines:
            w = d.textlength(ln, font=tf)
            d.text(((self.W - w) / 2, y), ln, font=tf, fill=self.INK)
            y += tf.size + 14
        y += 6
        w = d.textlength(sub, font=sf)
        d.text(((self.W - w) / 2, y), sub, font=sf, fill=self.MUT)
        y += sf.size + 30
        if lg:
            im.paste(lg, ((self.W - lg.width) // 2, y), lg)
        self._kind = "title"
        self._add(im, anchors={anchor: 0} if anchor else None)

    def logo(self, path, h=96, right_pad=0):
        """Logo at the top right of the current (title) block."""
        from PIL import Image
        try:
            lg = Image.open(path).convert("RGBA")
        except Exception:
            return
        lg = lg.resize((max(1, int(lg.width * h / lg.height)), h))
        if lg.width > 420:
            lg = lg.resize((420, max(1, int(lg.height * 420 / lg.width))))
        if not self.parts:
            return
        im = self.parts[0]
        need = lg.height + 20
        if im.height < need:  # make room
            from PIL import Image as I_
            big = I_.new("RGB", (im.width, need), self.BG)
            big.paste(im, (0, 0))
            self.parts[0] = im = big
        im.paste(lg, (self.W - self.M - right_pad - lg.width, 14), lg)

    def text(self, kind, txt, color=None, pad=(18, 8), anchor=None):
        self._kind = kind
        from PIL import Image, ImageDraw
        d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        font = self.f[kind]
        lines = self._wrap(d, txt, font, self.W - 2 * self.M)
        lh = font.size + 10
        im, d = self._canvas(pad[0] + lh * len(lines) + pad[1])
        for i, ln in enumerate(lines):
            d.text((self.M, pad[0] + i * lh), ln, font=font, fill=color or (self.INK if kind != "small" else self.MUT))
        if kind == "h2":
            d.line((self.M, im.height - 3, self.W - self.M, im.height - 3), fill=self.LINE, width=2)
        self._add(im, anchors={anchor: 0} if anchor else None)

    def bullets(self, items):
        from PIL import Image, ImageDraw
        if not items:
            return
        d0 = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        body, tagf, small = self.f["body"], self.f["tag"], self.f["small"]
        inner = self.W - 2 * self.M - 80
        rows = []
        for tags, text, ev in items:
            tag_w = sum(d0.textlength(str(t), font=tagf) + 22 for t in tags)
            first = max(200, inner - tag_w)
            words = (str(text) + (f"  ({ev})" if ev else "")).split()
            lines, cur, width = [], "", first
            for w_ in words:
                t = (cur + " " + w_).strip()
                if d0.textlength(t, font=body) <= width or not cur:
                    cur = t
                else:
                    lines.append(cur)
                    cur, width = w_, inner
            lines.append(cur)
            rows.append((tags, tag_w, lines))
        lh = body.size + 9
        h = 24 + sum(len(r[2]) * lh + 10 for r in rows) + 14
        im, d = self._canvas(h)
        d.rounded_rectangle((self.M, 6, self.W - self.M, h - 6), radius=12, fill=self.CARD, outline=self.LINE)
        y = 24
        for tags, tag_w, lines in rows:
            x = self.M + 22
            d.ellipse((x - 2, y + 8, x + 5, y + 15), fill=self.ACC)
            x += 16
            for t in tags:
                tw = d.textlength(str(t), font=tagf)
                d.rounded_rectangle((x, y + 1, x + tw + 14, y + body.size + 4), radius=9, fill=self.TAG_BG)
                d.text((x + 7, y + 4), str(t), font=tagf, fill=self.ACC)
                x += tw + 22
            for i, ln in enumerate(lines):
                main, _, evid = ln.partition("  (") if i == len(lines) - 1 else (ln, "", "")
                xx = x if i == 0 else self.M + 38
                d.text((xx, y + i * lh), main, font=body, fill=self.INK)
                if evid:
                    d.text((xx + d.textlength(main + "  ", font=body), y + i * lh + 2), "(" + evid, font=small, fill=self.MUT)
            y += len(lines) * lh + 10
        self._add(im)

    def weblinks(self, items):
        """Card with one line per (text, url, note): clickable in the PDF when there's a url."""
        from PIL import Image, ImageDraw
        if not items:
            return
        body, small = self.f["body"], self.f["small"]
        lh = body.size + 14
        h = 24 + lh * len(items) + 12
        im, d = self._canvas(h)
        d.rounded_rectangle((self.M, 6, self.W - self.M, h - 6), radius=12, fill=self.CARD, outline=self.LINE)
        links, y = [], 22
        for text, url, note in items:
            x = self.M + 24
            d.text((x, y), "\u00bb" if url else "\u2022", font=body, fill=self.ACC if url else self.MUT)
            x += 30
            col = self.ACC if url else self.INK
            d.text((x, y), text, font=body, fill=col)
            w = d.textlength(text, font=body)
            if url:
                d.line((x, y + body.size + 3, x + w, y + body.size + 3), fill=self.ACC, width=1)
                links.append((x - 4, y - 2, x + w + 4, y + body.size + 6, url))
            if note:
                d.text((x + w + 14, y + 3), f"({note})", font=small, fill=self.MUT)
            y += lh
        self._add(im, links=links)

    def table(self, df, max_rows=12):
        if df is None or not len(df):
            self.text("small", "No data.")
            return
        from PIL import Image, ImageDraw
        df = show_names(df).head(max_rows)
        cols = list(df.columns)
        cell = lambda v: "-" if v is None or (isinstance(v, float) and math.isnan(v)) else (
            (f"{v:.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:.1f}") if isinstance(v, float) else str(v))
        data = [[cell(v) for v in row] for row in df.itertuples(index=False)]
        d0 = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        th, td = self.f["th"], self.f["td"]
        widths = [max([d0.textlength(str(c), font=th)] + [d0.textlength(r[i], font=td) for r in data]) + 24 for i, c in enumerate(cols)]
        avail = self.W - 2 * self.M
        cap = 420
        while sum(min(w, cap) for w in widths) > avail and cap > 90:
            cap -= 20
        widths = [min(w, cap) for w in widths]
        rh = td.size + 15
        h = rh * (len(data) + 1) + 16
        im, d = self._canvas(h)
        x0 = self.M

        def clip(t, font, w):
            if d.textlength(t, font=font) <= w - 16:
                return t
            while t and d.textlength(t + "…", font=font) > w - 16:
                t = t[:-1]
            return t + "…"
        x = x0
        for c, w in zip(cols, widths):
            d.text((x + 8, 6), clip(str(c), th, w), font=th, fill=self.MUT)
            x += w
        d.line((x0, rh, x0 + sum(widths), rh), fill=self.LINE, width=2)
        for i, r in enumerate(data):
            y = rh * (i + 1)
            if i % 2:
                d.rectangle((x0, y + 1, x0 + sum(widths), y + rh), fill=self.ROW_ALT)
            x = x0
            for v, w in zip(r, widths):
                d.text((x + 8, y + 7), clip(v, td, w), font=td, fill=self.INK)
                x += w
        self._add(im)

    def images(self, paths, per_row=2, gap=16):
        from PIL import Image
        paths = [p for p in paths if Path(p).exists()]
        if not paths:
            return
        w = (self.W - 2 * self.M - gap * (per_row - 1)) // per_row
        for i in range(0, len(paths), per_row):
            row = [Image.open(p).convert("RGB") for p in paths[i:i + per_row]]
            row = [im.resize((w, int(im.height * w / im.width))) for im in row]
            h = max(im.height for im in row) + gap
            canvas, _ = self._canvas(h)
            for j, im in enumerate(row):
                canvas.paste(im, (self.M + j * (w + gap), gap // 2))
            self._add(canvas)

    def index(self, groups, head_grow=0):
        """Clickable contents: each page in bold, its sections as chips underneath. head_grow: bigger page names."""
        from PIL import Image, ImageDraw
        d0 = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        hf, cf = self.f["h3"], self.f["small"]
        if head_grow and hasattr(hf, "font_variant"):
            hf = hf.font_variant(size=hf.size + head_grow)
        x_max = self.W - self.M - 24
        layout, y = [], 22
        for title, anchor, secs in groups:
            layout.append(("head", self.M + 24, y, title, anchor, hf))
            y += hf.size + 12
            x = self.M + 40
            for txt, a in secs:
                w = d0.textlength(txt, font=cf) + 22
                if x + w > x_max:
                    x, y = self.M + 40, y + cf.size + 16
                layout.append(("chip", x, y, txt, a, cf))
                x += w + 8
            y += (cf.size + 16 if secs else 0) + 10
        h = y + 12
        im, d = self._canvas(h)
        d.rounded_rectangle((self.M, 6, self.W - self.M, h - 6), radius=12, fill=self.CARD, outline=self.LINE)
        links = []
        for kind, x, y_, txt, a, font in layout:
            w = d.textlength(txt, font=font)
            if kind == "head":
                d.text((x, y_), txt, font=font, fill=self.ACC)
                links.append((x - 4, y_ - 2, x + w + 4, y_ + font.size + 6, a))
            else:
                d.rounded_rectangle((x, y_ - 3, x + w + 18, y_ + font.size + 7), radius=9, fill=self.TAG_BG)
                d.text((x + 9, y_), txt, font=font, fill=self.INK)
                links.append((x, y_ - 3, x + w + 18, y_ + font.size + 7, a))
        self._add(im, links=links)

    def pages(self, max_h=15000, corner_logo=None, corner_h=96):
        """Stack the blocks into one or more pages: [{"img", "anchors", "links"}] with page-absolute positions.
        corner_logo: image put in the top right corner of every page (pages that don't start with a title get a
        header band for it, so it never covers a table or heatmap)."""
        from PIL import Image
        logo = None
        if corner_logo and Path(corner_logo).exists():
            try:
                logo = Image.open(corner_logo).convert("RGBA")
                logo = logo.resize((max(1, int(logo.width * corner_h / logo.height)), corner_h))
                if logo.width > 320:
                    logo = logo.resize((320, max(1, int(logo.height * 320 / logo.width))))
            except Exception:
                logo = None
        out, cur, h = [], [], 0
        for p, m in zip(self.parts, self.meta):
            if cur and h + p.height > max_h:
                out.append((cur, h))
                cur, h = [], 0
            cur.append((p, m))
            h += p.height
        if cur:
            out.append((cur, h))
        res = []
        for parts, h in out:
            starts_with_title = bool(parts) and parts[0][1].get("title")
            band = (logo.height + 24) if logo is not None and not starts_with_title else 0
            page = Image.new("RGB", (self.W, h + 40 + band), self.BG)
            anchors, links = {}, []
            y = 20 + band
            for p, m in parts:
                page.paste(p, (0, y))
                anchors.update({k: y + v for k, v in m["anchors"].items()})
                links += [(x0, y0 + y, x1, y1 + y, t) for x0, y0, x1, y1, t in m["links"]]
                y += p.height
            if logo is not None:
                page.paste(logo, (self.W - self.M - logo.width, 22), logo)
            res.append({"img": page, "anchors": anchors, "links": links})
        return res


def report_fmt(f):
    f = (f or "pdf").strip().lower()
    return "pdf" if f in ("jpg", "jpeg", "image", "images", "") else f


def save_report_pdf(pages, path: Path, groups=None):
    """All pages into one PDF (one PDF page per section; very long sections continue on the next page),
    with a clickable contents block and sidebar bookmarks. Written directly - no PDF library needed."""
    rendered = []
    for _, blocks in pages:
        rendered.extend(render_page_jpg(blocks))
    if not rendered:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(rendered, path, groups or [])
    return len(rendered)


def write_pdf(rendered, path: Path, groups, dpi=150.0, quality=88):
    """Minimal PDF writer: one JPEG image per page, link annotations, and an outline (bookmarks) tree."""
    k = 72.0 / dpi
    objs = {}          # object number -> bytes
    nxt = [3]          # 1 = catalog, 2 = page tree

    def new():
        nxt[0] += 1
        return nxt[0] - 1

    def pdf_str(t):    # UTF-16BE hex string: safe for any characters
        return "<FEFF" + str(t).encode("utf-16-be").hex().upper() + ">"

    page_ids, page_h = [], []
    for r in rendered:
        page_ids.append(new())
        page_h.append(r["img"].height * k)
    where = {a: (i, y) for i, r in enumerate(rendered) for a, y in r["anchors"].items()}

    def dest(anchor):
        pi, y = where[anchor]
        top = page_h[pi] - max(0.0, y - 10) * k
        return f"[{page_ids[pi]} 0 R /XYZ 0 {top:.2f} null]"

    for i, r in enumerate(rendered):
        img = r["img"]
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        jpg = buf.getvalue()
        img_id, cont_id = new(), new()
        objs[img_id] = (f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} /ColorSpace /DeviceRGB "
                        f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpg)} >>\nstream\n").encode() + jpg + b"\nendstream"
        w_pt, h_pt = img.width * k, page_h[i]
        content = f"q {w_pt:.2f} 0 0 {h_pt:.2f} 0 0 cm /Im0 Do Q".encode()
        objs[cont_id] = f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
        annots = []
        for x0, y0, x1, y1, target in r["links"]:
            if str(target).startswith(("http://", "https://")):
                a_id = new()
                uri = str(target).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                objs[a_id] = (f"<< /Type /Annot /Subtype /Link /Rect [{x0 * k:.2f} {h_pt - y1 * k:.2f} {x1 * k:.2f} {h_pt - y0 * k:.2f}] "
                              f"/Border [0 0 0] /A << /S /URI /URI ({uri}) >> >>").encode("latin-1", "replace")
                annots.append(f"{a_id} 0 R")
            elif target in where:
                a_id = new()
                objs[a_id] = (f"<< /Type /Annot /Subtype /Link /Rect [{x0 * k:.2f} {h_pt - y1 * k:.2f} {x1 * k:.2f} {h_pt - y0 * k:.2f}] "
                              f"/Border [0 0 0] /Dest {dest(target)} >>").encode()
                annots.append(f"{a_id} 0 R")
        objs[page_ids[i]] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w_pt:.2f} {h_pt:.2f}] "
                             f"/Resources << /XObject << /Im0 {img_id} 0 R >> >> /Contents {cont_id} 0 R"
                             + (f" /Annots [{' '.join(annots)}]" if annots else "") + " >>").encode()

    # bookmarks: pages, with their sections underneath
    outline_id = None
    tops = [(t, a, [(st, sa) for st, sa in secs if sa in where]) for t, a, secs in groups if a in where]
    if tops:
        outline_id = new()
        top_ids = [new() for _ in tops]
        for n, ((title, anchor, secs), oid) in enumerate(zip(tops, top_ids)):
            kid_ids = [new() for _ in secs]
            for m, ((st, sa), kid) in enumerate(zip(secs, kid_ids)):
                objs[kid] = (f"<< /Title {pdf_str(st)} /Parent {oid} 0 R /Dest {dest(sa)}"
                             + (f" /Prev {kid_ids[m - 1]} 0 R" if m else "")
                             + (f" /Next {kid_ids[m + 1]} 0 R" if m + 1 < len(kid_ids) else "") + " >>").encode()
            objs[oid] = (f"<< /Title {pdf_str(title)} /Parent {outline_id} 0 R /Dest {dest(anchor)}"
                         + (f" /Prev {top_ids[n - 1]} 0 R" if n else "")
                         + (f" /Next {top_ids[n + 1]} 0 R" if n + 1 < len(top_ids) else "")
                         + (f" /First {kid_ids[0]} 0 R /Last {kid_ids[-1]} 0 R /Count -{len(kid_ids)}" if kid_ids else "")
                         + " >>").encode()
        objs[outline_id] = (f"<< /Type /Outlines /First {top_ids[0]} 0 R /Last {top_ids[-1]} 0 R "
                            f"/Count {len(top_ids)} >>").encode()
    objs[1] = (f"<< /Type /Catalog /Pages 2 0 R"
               + (f" /Outlines {outline_id} 0 R /PageMode /UseOutlines" if outline_id else "") + " >>").encode()
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(page_ids)} >>".encode()

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in range(1, nxt[0]):
        offsets[num] = out.tell()
        out.write(f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {nxt[0]}\n0000000000 65535 f \n".encode())
    for num in range(1, nxt[0]):
        out.write(f"{offsets[num]:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {nxt[0]} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(out.getvalue())


def add_index(pages, cover=True):
    """Give every page and h2 section an anchor and add the contents. cover=True: a separate first page with the
    report title, the team logo centered under it and the contents links under that (the first page's own title
    becomes "Summary"). cover=False: contents block under the first page's title.
    Returns the contents as [(page title, anchor, [(section, anchor)])]."""
    cover_title = None
    if cover and pages:
        first = pages[0][1]
        ti = next((j for j, b in enumerate(first) if b[0] == "title"), None)
        if ti is not None:
            cover_title = (first[ti][1], first[ti][2])
            first[ti] = ("title", "Summary", first[ti][2]) + tuple(first[ti][3:])
    groups = []
    for i, (name, blocks) in enumerate(pages):
        page_anchor, secs, title = f"p{i}", [], name
        for j, b in enumerate(blocks):
            if b[0] == "title":
                blocks[j] = ("title", b[1], b[2], page_anchor)
                title = b[1]
            elif b[0] == "h2":
                a_ = f"p{i}s{j}"
                blocks[j] = ("h2", b[1], a_)
                secs.append((re.sub(r"\s*\(.*?\)", "", b[1]).strip() or b[1], a_))
        groups.append((title, page_anchor, secs))
    if cover_title:
        pages.insert(0, ("cover", [("cover", cover_title[0], cover_title[1], "cover"), ("index", groups)]))
    elif pages:
        first = pages[0][1]
        pos = next((j + 1 for j, b in enumerate(first) if b[0] == "title"), 0)
        first[pos:pos] = [("h2", "Contents"), ("index", groups)]
    return groups


def render_page_jpg(blocks):
    pg = _Page()
    first_title = True
    is_cover_page = bool(blocks) and blocks[0][0] == "cover"
    for b in blocks:
        if b[0] == "cover":
            pg.cover(b[1], b[2], THEME.get("team_logo") or (THEME.get("logo") if THEME.get("logo") else None), b[3])
            if THEME.get("logo") and THEME.get("team_logo") and Path(THEME["logo"]).exists() and THEME["logo"] != THEME["team_logo"]:
                pg.logo(THEME["logo"])  # your branding, top right of the cover
            continue
        if b[0] == "title":
            pg.text("title", b[1], pad=(24, 4), anchor=b[3] if len(b) > 3 else None)
            # branding logo only on the report's first page (when there's a cover page it goes there instead)
            is_cover = (b[3] if len(b) > 3 else "p0") == "p0" and b[1] != "Summary"
            if first_title and is_cover and THEME.get("logo") and Path(THEME["logo"]).exists() and THEME.get("logo") != THEME.get("team_logo"):
                pg.logo(THEME["logo"], right_pad=(400 if THEME.get("team_logo") else 0))
            first_title = False
            pg.text("small", b[2], pad=(2, 14))
        elif b[0] == "index":
            pg.index(b[1], head_grow=4 if is_cover_page else 0)
        elif b[0] in ("h2", "h3"):
            pg.text(b[0], b[1], color=THEME.get("head", _Page.INK) if b[0] == "h2" else _Page.MUT, pad=(26 if b[0] == "h2" else 14, 8),
                    anchor=b[2] if len(b) > 2 else None)
        elif b[0] == "bullets":
            pg.bullets(b[1])
        elif b[0] == "table":
            pg.table(b[1], b[2])
        elif b[0] == "links":
            pg.weblinks(b[1])
        elif b[0] == "images":
            pg.images(b[1])
    if THEME.get("footer"):
        pg.text("small", THEME["footer"], pad=(30, 18))
    return pg.pages(corner_logo=None if is_cover_page else THEME.get("team_logo"))


def img_tag(path):
    data = base64.b64encode(Path(path).read_bytes()).decode()
    return f"<img loading='lazy' src='data:image/png;base64,{data}' alt='{html.escape(Path(path).stem)}'>"


# =============================================================================
#  Pro reference library (HLTV demos you download) + signature plays matching
# =============================================================================
PRO_ARCHIVE_RE = re.compile(r"\.(rar|zip|7z)$", re.I)
MAP_WORDS = {"mirage": "de_mirage", "inferno": "de_inferno", "dust2": "de_dust2", "dust_2": "de_dust2", "nuke": "de_nuke",
             "ancient": "de_ancient", "anubis": "de_anubis", "vertigo": "de_vertigo", "overpass": "de_overpass",
             "train": "de_train"}


def map_from_name(name):
    low = name.lower()
    for w, m in MAP_WORDS.items():
        if re.search(rf"(^|[^a-z]){w}([^a-z]|$)", low):
            return m
    return ""


def file_fingerprint(p: Path):
    """Cheap identity for a demo: size + hash of the first and last MB (same demo = same fingerprint)."""
    import hashlib
    h = hashlib.sha1()
    size = p.stat().st_size
    with open(p, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), 2)
            h.update(f.read())
    return f"{size}:{h.hexdigest()[:16]}"


def extract_archive(arc: Path, dest: Path, log=print):
    """Unpack an HLTV .rar/.zip/.7z. Tries Python's zipfile, then Windows' built-in tar, then 7-Zip, then rarfile."""
    dest.mkdir(parents=True, exist_ok=True)
    errs = []
    if arc.suffix.lower() == ".zip":
        import zipfile
        try:
            with zipfile.ZipFile(arc) as z:
                z.extractall(dest)
            return True
        except Exception as e:
            errs.append(f"zip: {e}")
    tools = []
    if shutil.which("tar"):
        tools.append([shutil.which("tar"), "-xf", str(arc), "-C", str(dest)])
    for seven in [shutil.which("7z"), shutil.which("7za"), r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"]:
        if seven and Path(seven).exists():
            tools.append([seven, "x", "-y", f"-o{dest}", str(arc)])
            break
    for t in tools:
        try:
            r = subprocess.run(t, capture_output=True, text=True, timeout=900)
            if r.returncode == 0 and any(dest.rglob("*.dem")):
                return True
            errs.append(f"{Path(t[0]).name}: {(r.stderr or r.stdout).strip()[:160]}")
        except Exception as e:
            errs.append(f"{Path(t[0]).name}: {e}")
    try:
        import rarfile  # needs unrar/bsdtar on PATH as well
        with rarfile.RarFile(arc) as rf:
            rf.extractall(dest)
        return True
    except ImportError:
        errs.append("rarfile not installed")
    except Exception as e:
        errs.append(f"rarfile: {e}")
    log(f"could not unpack {arc.name} ({'; '.join(errs)}). Install 7-Zip (7-zip.org) and try again, "
        f"or unpack it yourself and run `pro import <folder>`.")
    return False


def flip_perspective(res):
    """parse_demo() result seen from the other team: swap tgt flags/sides so the same code reads either team."""
    R = res["rounds"].copy()
    tn = (5 - R["tgt_team_num"]).astype("float")
    R["tgt_team_num"] = tn
    R["tgt_side"] = tn.map(norm_side)
    R["tgt_won"] = R["winner_side"] == R["tgt_side"]
    R["tgt_equip"], R["opp_equip"] = res["rounds"]["opp_equip"].values, res["rounds"]["tgt_equip"].values
    tmap = R.set_index("round")["tgt_team_num"]
    pos = res["pos"].copy()
    pos["tgt"] = ~pos["tgt"]
    K = res["kills"].copy()
    if len(K):
        K["tgt_att"] = K["att_tn"] == K["round"].map(tmap)
        K["tgt_vic"] = K["vic_tn"] == K["round"].map(tmap)
    N = res["nades"].copy()
    if len(N):
        N["tgt"] = N["team_num"] == N["round"].map(tmap)
    other = [s for s in pos.loc[pos["tgt"], "steamid"].unique()]
    return dict(res, rounds=R, pos=pos, kills=K, nades=N, core=sorted(other))


def team_clan_names(path: Path):
    """{team_num: clan name} from the demo if the parser exposes it (pro demos usually have it)."""
    try:
        from demoparser2 import DemoParser
        p = DemoParser(str(path))
        df = p.parse_ticks(["team_clan_name", "team_num"])
        if df is not None and len(df) > 200000:
            df = df[df["tick"].isin(df["tick"].drop_duplicates().sample(200, random_state=1))]
        if df is None or not len(df) or "team_clan_name" not in df:
            return {}
        df = df[df["team_clan_name"].astype(str).str.strip() != ""]
        name_col = pick(df, "name", "player_name")
        return {str(n): g["team_clan_name"].mode().iloc[0] for n, g in df.groupby(name_col)} if name_col else {}
    except Exception:
        return {}


class ProLibrary:
    """Your folder of pro demos (from HLTV) - parsed so every T round of both teams can be matched against the
    team you scout. Layout: <PRO_DIR>/demos/<map>/*.dem, cache/, library.json, metastack_links.json."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.team_root = self.root
        self.demos = self.root / "demos"
        self.cache = self.root / "cache"
        for d in (self.root, self.demos, self.cache):
            d.mkdir(parents=True, exist_ok=True)
        self.lib_path = self.root / "library.json"
        self.links_path = self.root / "metastack_links.json"

    def manifest(self):  # Dataset compatibility: pro demos have no FACEIT match list
        return []

    # ---- library bookkeeping -----------------------------------------------------
    def library(self):
        try:
            return json.loads(self.lib_path.read_text())
        except Exception:
            return []

    def save_library(self, lib):
        self.lib_path.write_text(json.dumps(lib, indent=2))

    def demo_files(self):
        return sorted(self.demos.rglob("*.dem"))

    def per_map(self):
        c = Counter()
        for d in self.demo_files():
            c[d.parent.name] += 1
        return c

    def links(self):
        try:
            return json.loads(self.links_path.read_text())
        except Exception:
            return {}

    def save_links(self, links):
        self.links_path.write_text(json.dumps(links, indent=2))

    @staticmethod
    def link_key(demo_stem_, rnd):
        return f"{demo_stem_}#{int(rnd)}"

    # ---- adding demos ---------------------------------------------------------------
    def add(self, files, pool, target, force=False, log=print):
        """Copy demos (unpacking archives) into demos/<map>/, skipping duplicates and maps that already have their
        share of the TARGET (so the library spreads over the map pool). Returns (added, skipped reasons)."""
        lib = self.library()
        have = {e.get("fp") for e in lib}
        counts = self.per_map()
        cap = math.ceil(target / max(1, len(pool))) if pool else target
        added, skipped = [], []
        work = self.root / "_unpack"
        dems = []
        for f in files:
            f = Path(f)
            if PRO_ARCHIVE_RE.search(f.name):
                tmp = work / safe_name(f.stem)
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
                if extract_archive(f, tmp, log):
                    inner = sorted(tmp.rglob("*.dem"))
                    log(f"  unpacked {f.name}: {len(inner)} demo(s)")
                    dems += [(d, f.name) for d in inner]
            elif f.suffix.lower() in (".dem", ".gz", ".zst"):
                dems.append((f, f.name))
        for d, src in dems:
            try:
                if d.suffix.lower() in (".gz", ".zst"):
                    out = work / (demo_stem(d) + ".dem")
                    decompress(d, out)
                    d = out
                check_demo_file(d)
            except Exception as e:
                skipped.append((d.name, f"not a CS2 demo ({e})"))
                continue
            fp = file_fingerprint(d)
            if fp in have:
                skipped.append((d.name, "already in the library"))
                continue
            m = ""
            try:
                from demoparser2 import DemoParser
                m = str((DemoParser(str(d)).parse_header() or {}).get("map_name") or "")
            except Exception:
                pass
            m = m or map_from_name(d.name) or "unknown_map"
            if pool and m not in pool and not force:
                skipped.append((d.name, f"{m} is not in your map pool (PRO_POOL)"))
                continue
            if not force and counts[m] >= cap:
                skipped.append((d.name, f"already {counts[m]} on {m} (target {cap} per map) - `pro import force` to add anyway"))
                continue
            if not force and sum(counts.values()) >= target:
                skipped.append((d.name, f"library already has {target} demos (PRO_TARGET)"))
                continue
            dst = self.demos / m / (safe_name(demo_stem(d)) + ".dem")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(d, dst)
            counts[m] += 1
            have.add(fp)
            lib.append({"file": str(dst.relative_to(self.root)), "map": m, "fp": fp, "source": src,
                        "added": datetime.now().strftime("%Y-%m-%d %H:%M")})
            added.append((dst.name, m))
        shutil.rmtree(work, ignore_errors=True)
        self.save_library(lib)
        return added, skipped

    # ---- parsing --------------------------------------------------------------------
    def parse(self, sample_s=2.0, force=False, log=print):
        done = failed = 0
        for d in self.demo_files():
            stem = demo_stem(d)
            caches = [self.cache / f"{stem}~{s}.pkl" for s in ("A", "B")]
            if not force and all(c.exists() and c.stat().st_mtime >= d.stat().st_mtime for c in caches):
                try:
                    if pd.read_pickle(caches[0]).get("version") == PARSE_VERSION:
                        continue
                except Exception:
                    pass
            log(f"Parsing pro demo {d.name}")
            t0 = time.time()
            try:
                res = parse_demo(d, sample_s, set(), set(), choose_team=lambda m, g: sorted(g)[0], log=log)
            except Exception as e:
                bad(f"  {d.name}: {type(e).__name__}: {e}")
                failed += 1
                continue
            by_player = team_clan_names(d)  # player -> clan name (clan names follow the players across halftime)
            first_tn = sorted(res["groups"])[0]

            def label(tn):
                names_ = res["groups"].get(tn, [])
                c = Counter(by_player[n] for n in names_ if n in by_player).most_common(1)
                return c[0][0] if c else "team of " + ", ".join(sorted(names_)[:2])
            date = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d")
            for side_key, r_, tn in (("A", res, first_tn), ("B", flip_perspective(res), 5 - first_tn)):
                meta = dict(demo=f"{stem}~{side_key}", map=res["map"], match_id=f"{stem}~{side_key}", stem=stem, date=date,
                            opponent=label(5 - tn), team=label(tn), file=d.name, rounds=len(r_["rounds"]))
                pd.to_pickle(dict(version=PARSE_VERSION, meta=meta, rounds=r_["rounds"], kills=r_["kills"],
                                  nades=r_["nades"], pos=r_["pos"], dmg=r_["dmg"], names=r_["names"]),
                             self.cache / f"{stem}~{side_key}.pkl")
            good(f"  {res['map']}: {label(first_tn)} vs {label(5 - first_tn)}, {len(res['rounds'])} rounds  [{time.time() - t0:.0f}s]")
            done += 1
        # drop caches of demos you removed
        stems = {demo_stem(d) for d in self.demo_files()}
        for c in self.cache.glob("*.pkl"):
            if c.stem.rsplit("~", 1)[0] not in stems:
                c.unlink()
        return done, failed


# ---- play fingerprints -------------------------------------------------------------
NADE_WEIGHT = {"smoke": 1.0, "molotov": 1.0, "flash": 0.5, "he": 0.35}


def t_round_prints(ds, demos=None, buys=("full", "force")):
    """One fingerprint per T round of the dataset's target team: utility (kind@landing area, first 50 s),
    where the five were 8-40 s in, which site it went to and when it hit."""
    R = ds.rounds
    if R is None or not len(R):
        return []
    R = R[(R["tgt_side"] == "T") & R["buy"].isin(buys)]
    if demos is not None:
        R = R[R["demo"].isin(demos)]
    if not len(R):
        return []
    keys = set(R["key"])
    N = ds.nades
    N = N[N["tgt"] & N["key"].isin(keys) & (N["t"] <= 50) & ~N["post_plant"]] if N is not None and len(N) else pd.DataFrame()
    P = ds.pos
    P = P[P["tgt"] & P["alive"] & P["key"].isin(keys) & P["t"].between(8, 40)]
    K = ds.kills
    first_k = K[K["key"].isin(keys)].groupby("key")["t"].min() if K is not None and len(K) else pd.Series(dtype=float)
    util = defaultdict(dict)
    if len(N):
        for k, kind, pl, t in zip(N["key"], N["kind"], N["l_place"].fillna(""), N["t"]):
            if pl:
                tok = f"{kind}@{pl}"
                util[k][tok] = min(util[k].get(tok, 999), float(t))
    pres = {}
    for k, g in P.groupby("key"):
        c = g["place"].replace("", np.nan).dropna().value_counts(normalize=True)
        pres[k] = c.to_dict()
    team_of = ds.matches.set_index("demo")["team"].to_dict() if "team" in ds.matches else {}
    file_of = ds.matches.set_index("demo")["file"].to_dict() if "file" in ds.matches else {}
    stem_of = ds.matches.set_index("demo")["stem"].to_dict() if "stem" in ds.matches else {}
    out = []
    for r in R.to_dict("records"):
        k = r["key"]
        hit = r["plant_t"] if r.get("planted") else first_k.get(k, np.nan)
        out.append(dict(key=k, map=r["map"], demo=r["demo"], match_id=r["match_id"], stem=stem_of.get(r["demo"], r["demo"]),
                        round=int(r["round"]),
                        opponent=r["opponent"], date=r["date"], team=team_of.get(r["demo"], ""),
                        file=file_of.get(r["demo"], ""), site=r.get("t_target", "?") or "?", won=bool(r["tgt_won"]),
                        planted=bool(r.get("planted")), hit_t=float(hit) if hit == hit else np.nan,
                        util=util.get(k, {}), pres=pres.get(k, {})))
    return out


def play_similarity(a, b):
    """0..1: how alike two T rounds are (same map assumed)."""
    ua, ub = a["util"], b["util"]
    if ua or ub:
        toks = set(ua) | set(ub)
        w = lambda t: NADE_WEIGHT.get(t.split("@")[0], 0.5)
        inter = sum(w(t) for t in toks if t in ua and t in ub)
        union = sum(w(t) for t in toks)
        u = inter / union if union else 0.0
    else:
        u = 0.0
    pa, pb = a["pres"], b["pres"]
    p = sum(min(v, pb.get(k, 0)) for k, v in pa.items()) if pa and pb else 0.0
    s = 1.0 if a["site"] == b["site"] and a["site"] not in ("?", "") else 0.0
    ta, tb = a["hit_t"], b["hit_t"]
    t = math.exp(-abs(ta - tb) / 15.0) if ta == ta and tb == tb else 0.5
    return 0.40 * u + 0.30 * p + 0.20 * s + 0.10 * t


def cluster_plays(prints, threshold=0.55):
    """Group a team's T rounds into recurring plays (greedy, per map). Returns [[print, ...], ...] biggest first."""
    clusters = []
    for m in sorted({p["map"] for p in prints}):
        mine = [p for p in prints if p["map"] == m]
        groups = []
        for p in mine:
            best, bs = None, threshold
            for g in groups:
                s = sum(play_similarity(p, q) for q in g) / len(g)
                if s >= bs:
                    best, bs = g, s
            if best is None:
                groups.append([p])
            else:
                best.append(p)
        clusters += groups
    return sorted(clusters, key=len, reverse=True)


def describe_play(group):
    """Readable summary of a recurring play: site, the utility in most of its rounds, timing, record."""
    m = group[0]["map"]
    n = len(group)
    site = Counter(p["site"] for p in group).most_common(1)[0][0]
    tok = Counter(t for p in group for t in p["util"])
    common = [(t, c) for t, c in tok.most_common() if c >= max(2, 0.5 * n)][:6]
    util_txt = ", ".join(f"{t.split('@')[0]} {show_place(m, t.split('@')[1])}" for t, _ in common) or "little utility"
    route = Counter()
    for p in group:
        for pl, v in p["pres"].items():
            route[pl] += v
    lanes = [show_place(m, pl) for pl, _ in route.most_common() if "spawn" not in pl.lower()][:3]
    hits = [p["hit_t"] for p in group if p["hit_t"] == p["hit_t"]]
    hit = f"{np.median(hits):.0f}s" if hits else "-"
    wins = sum(p["won"] for p in group)
    title = f"{'Mid' if site == 'Mid' else site + ' site' if site in ('A', 'B') else 'Unclear target'} via {' / '.join(lanes[:2]) or '?'}"
    return dict(map=m, title=title, site=site, rounds=n, won=wins, win_pct=round(100 * wins / n), hit=hit,
                utility=util_txt, lanes=", ".join(lanes), tokens=[t for t, _ in common])


def match_pros(group, pro_prints, top=3, floor=0.35, linked=()):
    """Closest pro T rounds to a play (average similarity to the play's rounds). Rounds you've saved a MetaStack
    link for win near-ties (within 5 points), so the report points at a video when one is close enough."""
    m = group[0]["map"]
    cands = [q for q in pro_prints if q["map"] == m]
    scored = []
    for q in cands:
        s = sum(play_similarity(p, q) for p in group) / len(group)
        if s >= floor:
            scored.append((s, q))
    scored.sort(key=lambda x: -(x[0] + (0.05 if f"{x[1]['stem']}#{x[1]['round']}" in linked else 0)))
    out, seen = [], set()
    for s, q in scored:  # at most one round per pro match & team, so the list isn't one match repeated
        k = q["stem"]
        if k in seen:
            continue
        seen.add(k)
        out.append((s, q))
        if len(out) >= top:
            break
    return out


# =============================================================================
#  Console
# =============================================================================
VIEWS = [
    ("overview", "Overview (per map, matches, results)"),
    ("openings", "Opening duels - FK / FD (+ heatmap)"),
    ("players", "Player profiles & roles"),
    ("smoke", "Smokes - landing spots & lineups (+ map)"),
    ("flash", "Flashes - landing spots & effectiveness (+ map)"),
    ("molotov", "Molotovs / incendiaries (+ map)"),
    ("he", "HE grenades (+ map)"),
    ("pos_early", "Positioning - early round (+ heatmap)"),
    ("pos_mid", "Positioning - mid round (+ heatmap)"),
    ("pos_late", "Positioning - late round (+ heatmap)"),
    ("setups", "CT setups"),
    ("executes", "T side - site hits & timings"),
    ("economy", "Economy"),
    ("trends", "Trends over time"),
    ("anomalies", "Anomalies"),
    ("counter", "Counter-play suggestions"),
    ("vetos", "Map vetos - played / banned / picked (most to least)"),
    ("postplant", "Post-plant setups & CT retakes (+ heatmap)"),
    ("rotations", "Rotations per player (+ heatmaps)"),
    ("roles", "Player roles - AWPer / entry / support / lurker / IGL"),
    ("lowbuy", "Eco & force rounds - where they play / path (+ heatmap)"),
]
# views that work by name (`show power`, `heatmap power`, reports) but aren't in the numbered list
HIDDEN_VIEWS = [("power", "Power positions - each player's spot per map and half (+ map)")]
VIEW_ALIASES = {"fk": "openings", "fd": "openings", "fkfd": "openings", "opening": "openings", "smokes": "smoke",
                "flashes": "flash", "molly": "molotov", "mollies": "molotov", "molotovs": "molotov", "inc": "molotov",
                "nade": "he", "hes": "he", "early": "pos_early", "mid": "pos_mid", "late": "pos_late",
                "setup": "setups", "ct": "setups", "execute": "executes", "t": "executes", "sites": "executes",
                "eco": "economy", "trend": "trends", "anomaly": "anomalies", "counterplay": "counter",
                "suggestions": "counter", "player": "players", "veto": "vetos", "bans": "vetos", "picks": "vetos",
                "pp": "postplant", "post-plant": "postplant", "post": "postplant", "retake": "postplant", "retakes": "postplant",
                "rotation": "rotations", "rot": "rotations", "rotate": "rotations", "role": "roles",
                "ecos": "lowbuy", "force": "lowbuy", "forces": "lowbuy", "lowbuys": "lowbuy", "antieco": "lowbuy",
                "anti-eco": "lowbuy", "ecoforce": "lowbuy", "eco-force": "lowbuy",
                "powerpositions": "power", "power-positions": "power", "pp-players": "power", "spots": "power"}
SEARCH_ALIASES = {"molly": "molotov", "mollies": "molotov", "inc": "molotov", "incendiary": "molotov",
                  "smokes": "smoke", "flashes": "flash", "kills": "kill", "deaths": "death", "nade": "he",
                  "openingkill": "fk", "openingdeath": "fd", "entry": "fk"}

HOME_WORDS = ("home", "main", "menu", "m")

MAIN_MENU = [
    ("1", "Set team (CSC team name)", "team"),
    ("2", "Find & download the team's CSC demos", "download"),
    ("3", "Import demos I downloaded by hand", "import"),
    ("4", "Parse demos", "parse"),
    ("5", "Browse data (numbered views)", "show"),
    ("6", "Search (e.g. smoke window)", "search"),
    ("7", "Heatmaps", "heatmap"),
    ("8", "Anomalies", "anomalies"),
    ("9", "Counter-play suggestions", "counter"),
    ("10", "Full analyst report (PDF / HTML / both)", "report"),
    ("11", "Options & filters", "options"),
    ("12", "Status", "status"),
    ("13", "Set map (work on one map only)", "setmap"),
    ("14", "Roster - mains, subs, dropped players", "roster"),
    ("15", "Map vetos - played / banned / picked", "vetos"),
    ("16", "Purge ALL demo files", "purge"),
    ("17", "Player roles (AWPer / entry / support / lurker / IGL)", "role"),
    ("18", "Clear cache (parsed data / pro data / images - demos are kept)", "clearcache"),
    ("19", "All CSC teams (by tier) - pick one to scout", "teams"),
    ("20", "Report style - themes, colors, logo, footer", "style"),
    ("0", "Exit", "exit"),
]

BANNER = r"""
    ___ ___  ___        _        _     _      _    
   / __/ __|/ __|    _ | | __ _ | |__ (_) __ | |_  
  | (__\__ \ (__    | || |/ _` || '_ \| |/ _|| ' \ 
   \___|___/\___|    \__/ \__,_||_.__/|_|\__||_||_|
                                             v{v}
"""


class Console(cmd.Cmd):
    def __init__(self, ws: Workspace, interactive=True, assume_yes=False):
        super().__init__()
        self.ws = ws
        self.cfg = Config(ws.root)
        C.on = self.cfg.bool("COLOR") and (sys.stdout.isatty() or os.environ.get("FORCE_COLOR"))
        try:
            ws.set_demo_dir(self.cfg["DEMO_DIR"])
        except OSError as e:
            warn(f"DEMO_DIR {self.cfg['DEMO_DIR']} isn't available ({e.strerror or e}); using {ws.default_demos}. "
                 f"Plug the drive in or `set DEMO_DIR` again.")
            ws.set_demo_dir("")
        global DISPLAY
        self.names = DISPLAY = CalloutNames(ws.root, self.cfg)
        self.ds = Dataset(ws, self.cfg)
        self.radar = Radar(ws, self.cfg)
        self.an = Analyst(self.ds, self.cfg, self.radar)
        self.interactive = interactive
        self.assume_yes = assume_yes
        self.module = None          # msf-style `use`
        self.last = None            # last View or search result for `plot` / `export`
        self.last_search = None
        self.team_name = self._team_name_cached()
        if self.team_name:
            self.cfg.labels["TEAM_URL"] = self.team_name
        self._tidy()
        self.ds.load(quiet=True)

    # ---------------------------------------------------------------- plumbing
    @property
    def prompt(self):
        team = self.team_name or "no team"
        bits = [self.cfg[k] for k in ("MAP", "SIDE", "PHASE", "PLAYER") if self.cfg[k]]
        if self.cfg["ROSTER"] in ("main", "lineup"):
            bits.append("mains" if self.cfg["ROSTER"] == "main" else "main 5")
        if self.cfg["SEASON"]:
            bits.insert(0, "S" + self.cfg["SEASON"].replace(",", "+S"))
        pm = self.cfg["PARSE_MAP"]
        if pm and pm != self.cfg["MAP"]:
            bits.insert(0, "only " + pm)
        filt = ",".join(bits)
        mod = f" {C.s(self.module, 'r', 'bold')}" if self.module else ""
        f = f" {C.s('[' + filt + ']', 'y')}" if filt else ""
        return f"{C.s('csc', 'bold', 'c')}({C.s(team, 'w')}){mod}{f} > "

    def _tidy(self):
        try:
            moved = self.ws.tidy_team_data()
        except Exception as e:
            warn(f"Couldn't tidy the match list: {e}")
            return
        for team, n in moved.items():
            info(f"Moved {n} match(es) that belong to {team} out of {self.team_name or 'this team'}'s data "
                 f"(with their demos and parsed data).")

    def _team_name_cached(self):
        p = self.ws.team_root / "team.json"
        try:
            return json.loads(p.read_text()).get("name", "")
        except Exception:
            return ""

    def ask(self, q, default=""):
        if not self.interactive:
            return default
        try:
            a = input(C.s("[?] ", "m", "bold") + q + (f" [{default}]" if default else "") + ": ").strip()
        except EOFError:
            return default
        if a.lower() in HOME_WORDS:
            raise GoHome()
        return a or default

    def emptyline(self):
        pass

    def precmd(self, line):
        return line.strip()

    def onecmd(self, line):
        try:
            return super().onecmd(line)
        except GoHome:
            self.do_home("")
        except KeyboardInterrupt:
            print()
            warn("Interrupted.")
        except (FaceitError, CscError, DemoParseError, ValueError, KeyError) as e:
            bad(str(e).strip("'\""))
        except Exception as e:
            bad(f"{type(e).__name__}: {e}")
            if os.environ.get("ESEA_DEBUG"):
                traceback.print_exc()
            else:
                print(C.s("    (set ESEA_DEBUG=1 for the full traceback)", "d"))
        return False

    def default(self, line):
        tok = line.split()[0]
        if tok.isdigit():
            byn = {n: c for c, n in self.command_numbers().items()}
            if int(tok) in byn:
                return self.onecmd(byn[int(tok)] + " " + " ".join(line.split()[1:]))
        bad(f"Unknown command: {tok}. Type `help` or `menu`.")

    def need_data(self):
        if not self.ds.loaded and not self.ds.load():
            return False
        return True

    def completenames(self, text, *ignored):
        return [c for c in self.get_names_cmds() if c.startswith(text)]

    def get_names_cmds(self):
        return sorted(n[3:] for n in self.get_names() if n.startswith("do_"))

    # ---------------------------------------------------------------- menu & help
    def do_home(self, arg):
        """home - back to the numbered main menu (also leaves a `use` module)."""
        self.module = None
        self.do_menu("")

    do_main = do_home
    do_m = do_home

    def do_menu(self, arg):
        """menu - show the numbered main menu."""
        print()
        for k, label, _ in MAIN_MENU:
            print(f"  {C.s('[' + k.rjust(2) + ']', 'c', 'bold')} {label}")
        print(C.s("\n  Type a number or a command (`commands` lists them all). `home` brings you back here.", "d"))

    def command_numbers(self):
        """Every command gets a number: menu commands keep theirs, the rest follow from 20 (alphabetical)."""
        menu = {c: int(k) for k, _, c in MAIN_MENU}
        names = []
        seen = set()
        for name in sorted(self.get_names()):
            if not name.startswith("do_") or name == "do_EOF":
                continue
            fn = getattr(type(self), name)
            if fn in seen:
                continue
            seen.add(fn)
            names.append(fn.__name__[3:])
        nums = dict(menu)
        nxt = max(menu.values()) + 1
        for n in sorted(names):
            if n not in nums:
                nums[n] = nxt
                nxt += 1
        return nums

    def do_commands(self, arg):
        """commands [word] - numbered list of every command; type a number to run it (optionally filter by a word)."""
        funcs = {}
        for name in self.get_names():
            if not name.startswith("do_") or name in ("do_EOF",):
                continue
            fn = getattr(self, name)
            key = getattr(fn, "__func__", fn)
            funcs.setdefault(key, []).append(name[3:])
        menu = {c: str(n) for c, n in self.command_numbers().items()}
        rows = []
        for fn, names in funcs.items():
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            main = next((n for n in names if fn.__name__ == "do_" + n), sorted(names, key=len)[-1])
            aliases = sorted(n for n in names if n != main)
            usage, _, desc = doc.partition(" - ")
            if not desc:
                usage, desc = main, doc
            rows.append((main, usage.strip() or main, desc.strip(), aliases, menu.get(main, "")))
        word = arg.strip().lower()
        if word:
            rows = [r for r in rows if word in " ".join([r[0], r[1], r[2], *r[3]]).lower()]
        rows.sort(key=lambda r: int(r[4]) if str(r[4]).isdigit() else 999)
        if not rows:
            warn(f"No commands mention '{word}'.")
            return
        width = shutil.get_terminal_size((120, 20)).columns
        uw = min(34, max(len(r[1]) for r in rows))
        print(C.s(f"\n  {len(rows)} command(s)" + (f" matching '{word}'" if word else ""), "bold"))
        for main, usage, desc, aliases, num in rows:
            tag = C.s(f"[{num:>2}] ", "c") if num else "     "
            extra = C.s(f"  (also: {', '.join(aliases)})", "d") if aliases else ""
            lines = textwrap.wrap(desc, width=max(40, width - uw - 12)) or [""]
            if len(usage) > uw:  # long usage: description on the next line
                print(f"  {tag}{C.s(usage, 'c')}{extra}")
                extra = ""
                for ln in lines:
                    print(" " * (uw + 8) + ln)
                continue
            print(f"  {tag}{C.s(usage.ljust(uw), 'c')} {lines[0]}{extra}")
            for ln in lines[1:]:
                print(" " * (uw + 8) + ln)
        print(C.s("\n  Type a number to run that command.  `help <command>` for details, `home` for the menu.", "d"))

    do_cmds = do_commands

    def do_help(self, arg):
        """help [command] - list commands or explain one."""
        if arg:
            f = getattr(self, "do_" + arg, None)
            print((f.__doc__ or "No help.") if f else f"Unknown command {arg}")
            return
        groups = [
            ("Team & demos", ["team", "teams", "logos", "setmap", "matches", "download", "getdemos", "import", "parse", "movedemos", "delete", "purge", "status", "inspect", "radars"]),
            ("Explore", ["show", "search", "rounds", "spot", "callouts", "rename", "plot", "heatmap", "player", "role", "roster", "maps"]),
            ("Analyst", ["vetos", "anomalies", "counter", "report", "style"]),
            ("Pro library & signature plays", ["pro", "signature"]),
            ("Options (msfconsole style)", ["options", "set", "unset", "use", "run", "back", "filters", "reset"]),
            ("Other", ["commands", "home", "export", "clearcache", "menu", "clear", "exit"]),
        ]
        for g, cmds in groups:
            print(C.s(f"\n{g}", "bold"))
            print(C.s("  " + "=" * len(g), "d"))
            for c in cmds:
                f = getattr(self, "do_" + c, None)
                doc = (f.__doc__ or "").strip().split("\n")[0] if f else ""
                print(f"  {C.s(c.ljust(11), 'c')} {doc}")
        print()

    # ---------------------------------------------------------------- options
    def do_options(self, arg):
        """options - show all options and current values (same as `show options`)."""
        groups = defaultdict(list)
        for k, d, desc, g in OPTION_DEFS:
            groups[g].append((k, desc))
        for g in ("Team", "Pro library", "Filters", "Analysis", "Output"):
            print(C.s(f"\n{g} options", "bold"))
            print(f"  {'Name':<16} {'Current':<38} Description")
            print(f"  {'-' * 16} {'-' * 38} {'-' * 11}")
            for k, desc in groups[g]:
                v = self.cfg.shown(k)
                print(f"  {C.s(k.ljust(16), 'c')} {str(v)[:38]:<38} {C.s(desc, 'd')}")
        print(C.s("\n  set <NAME> <value>   |   unset <NAME>   |   filters", "d"))

    def do_set(self, arg):
        """set <OPTION> <value> - e.g. set MAP mirage, set SIDE T, set PHASE early"""
        parts = arg.split(None, 1)
        if not parts:
            return self.do_options("")
        if len(parts) == 1:
            print(f"{parts[0].upper()} => {self.cfg.shown(parts[0])}")
            return
        k, v = parts[0].upper(), parts[1]
        if k == "DEMO_DIR":
            return self._move_demo_dir(v)
        val = self.cfg.set(k, v)
        print(f"{k} => {self.cfg.shown(k)}")
        if k == "COLOR":
            C.on = self.cfg.bool("COLOR")
        if k in ("PHASE_SPLITS", "PLAYER_NAMES") and self.ds.loaded:
            self.ds.load(quiet=True)
        if k == "TEAM_URL" and val:
            self._resolve_team(quiet=False)
            print(f"TEAM_URL => {self.cfg.shown('TEAM_URL')}")
        if k == "MAP" and val and self.ds.loaded and val not in set(self.ds.matches["map"]):
            warn(f"No parsed demos on {val} yet (have: {', '.join(sorted(self.ds.matches['map'].unique()))}).")
        if k == "PLAYER" and val and self.ds.loaded:
            names = self.ds.tgt_names()
            match = [n for n in names if n.lower() == val.lower()] or [n for n in names if val.lower() in n.lower()]
            if match and match[0] != val:
                self.cfg.set("PLAYER", match[0])
                print(f"PLAYER => {match[0]}")
            elif not match:
                warn(f"No scouted player named {val}. Players: {', '.join(names)}")

    def complete_set(self, text, line, *a):
        parts = line.split()
        if len(parts) <= 2 and not line.endswith(" ") or len(parts) == 1:
            return [k for k, *_ in OPTION_DEFS if k.startswith(text.upper())]
        k = parts[1].upper()
        opts = {"SIDE": ["T", "CT"], "PHASE": ["early", "mid", "late"], "OUTPUT": ["table", "json", "csv"],
                "ROSTER": ["all", "main", "lineup"],
                "HEATMAP_STYLE": ["density", "points", "both"], "OPEN_FILES": ["true", "false"]}.get(k, [])
        if k == "MAP" and self.ds.loaded:
            opts = sorted(self.ds.matches["map"].unique())
        if k == "PLAYER" and self.ds.loaded:
            opts = self.ds.tgt_names()
        return [o for o in opts if o.lower().startswith(text.lower())]

    def do_setmap(self, arg):
        """setmap <map[,map]|all> - only download/import/parse this map, and show only it. e.g. setmap mirage"""
        a = arg.strip() or self.ask("Map to work on (e.g. mirage, inferno; `all` for every map)",
                                    ",".join(sorted(self.cfg.parse_maps())) or "all")
        if not a:
            return
        if a.lower() in ("all", "*", "any", "none", "off"):
            self.cfg.set("PARSE_MAP", "")
            self.cfg.set("MAP", "")
            good("Working on all maps again.")
            return
        val = self.cfg.set("PARSE_MAP", a)
        maps = val.split(",")
        self.cfg.set("MAP", maps[0] if len(maps) == 1 else "")
        good(f"Map set to {', '.join(maps)}: download, import and parse will skip other maps"
             + (f", and views show only {maps[0]}." if len(maps) == 1 else "."))
        if self.ds.loaded:
            have = set(self.ds.matches["map"])
            if not have & set(maps):
                info("Nothing parsed on that map yet - run `download`/`import`, then `parse`.")
        man = [r for r in self.ws.manifest() if r["Map"] in maps]
        if self.ws.manifest():
            info(f"{len(man)} league demo(s) on {', '.join(maps)} in the match list.")

    def complete_setmap(self, text, *a):
        opts = ["all"] + sorted(MAP_DATA)
        return [o for o in opts if o.startswith(text) or o[3:].startswith(text)]

    def demo_map(self, d: Path):
        """Map of a demo file without fully parsing it: match list, then folder, then demo header."""
        row = self.ws.manifest_for(d)
        if row and row.get("Map") and row["Map"] != "unknown_map":
            return row["Map"]
        if d.parent.name.startswith(("de_", "cs_")):
            return d.parent.name
        try:
            from demoparser2 import DemoParser
            return str(DemoParser(str(d)).parse_header().get("map_name") or "")
        except Exception:
            return ""

    def do_movedemos(self, arg):
        """movedemos <folder> - move all demos to a new folder (any drive) and keep using it, e.g. movedemos D:\\CS\\demos"""
        dest = arg.strip().strip('"').strip("'") or self.ask(f"Move demos from {self.ws.demos} to which folder")
        if not dest:
            return
        if dest.lower() in ("default", "reset"):
            dest = ""
        self._move_demo_dir(dest)

    do_movedemo = do_movedemos

    def _move_demo_dir(self, v):
        old = self.ws.demo_base
        val = self.cfg.set("DEMO_DIR", v)
        new = Path(val) if val else self.ws.demo_base_default
        print(f"DEMO_DIR => {new}")
        existing = [p for p in old.rglob("*") if p.is_file()] if old.exists() and old.resolve() != new.resolve() else []
        self.ws.set_demo_dir(val)
        if not existing:
            return
        size = sum(p.stat().st_size for p in existing)
        ans = self.ask(f"Move the {len(existing)} existing demo file(s) ({human_size(size)}) from {old} to {new}? (Y/n)", "y")
        if not ans.lower().startswith("y"):
            info(f"Left them in {old}. Only demos in {new} will be used from now on.")
            return
        moved = 0
        for f in existing:
            dst = new / f.relative_to(old)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.move(str(f), dst)
                moved += 1
        for d in sorted((p for p in old.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            try:
                d.rmdir()
            except OSError:
                pass
        good(f"Moved {moved} file(s) to {new}.")

    def do_unset(self, arg):
        """unset <OPTION|all> - clear an option (unset all clears every filter)."""
        if arg.strip().lower() in ("all", "filters"):
            return self.do_reset("")
        if arg.strip().upper() == "DEMO_DIR":
            return self._move_demo_dir("")
        default = next((d for k, d, *_ in OPTION_DEFS if k == arg.strip().upper()), "")
        self.cfg.set(arg.strip(), default)
        print(f"{arg.strip().upper()} => {default}")

    complete_unset = complete_set

    def do_reset(self, arg):
        """reset - clear all filters (MAP, SIDE, PHASE, PLAYER, LAST_N, OPPONENT, SEASON, ROSTER)."""
        for k in ("MAP", "SIDE", "PHASE", "PLAYER", "LAST_N", "OPPONENT", "SEASON"):
            self.cfg.set(k, "")
        self.cfg.set("ROSTER", "all")
        good("Filters cleared.")

    def do_filters(self, arg):
        """filters - show the active filters."""
        for k in ("MAP", "SIDE", "PHASE", "PLAYER", "LAST_N", "OPPONENT", "SEASON", "ROSTER"):
            print(f"  {k:<9} {self.cfg[k] or C.s('(any)', 'd')}")

    # ---------------------------------------------------------------- msf-style modules
    def do_use(self, arg):
        """use <view> - select a view module (e.g. use smoke), then `run`, `set`, `back`."""
        key = self._view_key(arg.strip())
        if not key:
            bad("Unknown module. `show` lists them.")
            return
        self.module = key
        info(f"Using {key}. Set filters with `set`, then `run` (or `plot`).")

    def complete_use(self, text, *a):
        return [k for k, _ in VIEWS if k.startswith(text)]

    def do_run(self, arg):
        """run - run the selected module (after `use`)."""
        if not self.module:
            bad("Nothing selected. `use <view>` first, or `show <number>`.")
            return
        self.show_view(self.module, plot="plot" in arg)

    do_exploit = do_run

    def do_back(self, arg):
        """back - leave the current module."""
        self.module = None

    # ---------------------------------------------------------------- team & demos
    def saved_teams(self):
        return self.ws.saved_teams()

    def _remember_team(self, team, url):
        teams = [t for t in self.saved_teams() if t.get("team_id") != team["team_id"]]
        teams.insert(0, {"name": team.get("name", "?"), "team_id": team["team_id"],
                         "url": team.get("name", url), "tier": team.get("tier", ""),
                         "used": datetime.now().strftime("%Y-%m-%d")})
        (self.ws.root / "teams.json").write_text(json.dumps(teams, indent=2))
        self.cfg.labels["TEAM_URL"] = team.get("name", "")

    def csc(self):
        return Csc(self.cfg["CSC_API"])

    def _resolve_team(self, quiet=True):
        try:
            team = self.csc().resolve_team(self.cfg["TEAM_URL"])
        except CscError as e:
            bad(str(e))
            raise
        switching = safe_name(team.get("name", "")) != self.ws.team_key
        self._remember_team(team, self.cfg["TEAM_URL"])
        self.team_name = team.get("name", "")
        if switching:
            self.ws.set_team(self.team_name)
            for k in ("PLAYER", "OPPONENT"):
                if self.cfg[k]:
                    self.cfg.set(k, "")
            self.last, self.last_search, self.module = None, None, None
            self._tidy()
            self.ds.clear()
            self.ds.load(quiet=True)
            n = len(self.ws.manifest())
            info(f"Switched to {self.team_name}'s data ({self.ws.team_root})"
                 + (f" - {n} demo(s) in its match list." if n else " - no match list yet, run `download`."))
        (self.ws.team_root / "team.json").write_text(json.dumps(
            {"team_id": team["team_id"], "name": self.team_name, "members": team["members"],
             "leader": team.get("leader_name", ""), "tier": team.get("tier", ""), "franchise": team.get("franchise", "")},
            indent=2))
        if not quiet:
            good(f"Team: {self.team_name} ({team.get('franchise') or '-'}, {team.get('tier') or '?'} tier) - roster: "
                 f"{', '.join(m['nickname'] for m in team['members'])}"
                 + (f"  | captain: {team['leader_name']}" if team.get("leader_name") else ""))
        return team

    def do_teams(self, arg):
        """teams [tier|franchise|text] [all] - every CSC team, grouped by tier; pick one by number to scout it.
        `all` includes inactive teams."""
        words = arg.split()
        inactive = "all" in [w.lower() for w in words]
        flt = " ".join(w for w in words if w.lower() != "all").lower()
        try:
            ts = self.csc().q("{ teams(includeInactive: %s) { id name active tier { name } franchise { name } } }"
                              % ("true" if inactive else "false")).get("teams") or []
        except CscError as e:
            bad(str(e))
            return
        rows = [{"team": t["name"], "tier": (t.get("tier") or {}).get("name", ""),
                 "franchise": (t.get("franchise") or {}).get("name", ""), "active": t.get("active", True)} for t in ts]
        if flt:
            rows = [r for r in rows if flt in r["team"].lower() or flt in r["tier"].lower() or flt in r["franchise"].lower()]
        if not rows:
            warn("No teams match that.")
            return
        rows.sort(key=lambda r: (r["tier"], r["team"].lower()))
        for i, r in enumerate(rows, 1):
            r["#"] = i
        df = pd.DataFrame(rows)[["#", "tier", "team", "franchise"] + (["active"] if inactive else [])]
        self.print_table(df, f"CSC teams ({len(rows)})", rows=1000)
        if self.interactive:
            a = self.ask("Number to scout (Enter to skip)", "")
            if a.isdigit() and 1 <= int(a) <= len(rows):
                self.do_team(rows[int(a) - 1]["team"])
        else:
            print(C.s("  `team <name>` to scout one.", "d"))

    def do_team(self, arg):
        """team [name|playcsc.com team page|number] - set the CSC team to scout; with nothing, pick from saved teams."""
        choice = arg.strip()
        saved = self.saved_teams()
        if not choice:
            if saved:
                print(C.s("\n  Saved teams", "bold"))
                for i, t in enumerate(saved, 1):
                    cur = C.s("  (current)", "g") if t["name"] == self.team_name else ""
                    print(f"  {C.s('[' + str(i) + ']', 'c', 'bold')} {t['name']}" + C.s(f"  {t.get('tier', '')}", "d") + cur)
                choice = self.ask("Number or CSC team name (`teams` lists every team)", self.team_name)
            else:
                choice = self.ask("CSC team name (e.g. Sirens) or its playcsc.com page (`teams` lists every team)")
        if not choice:
            return
        if choice.isdigit() and saved and 1 <= int(choice) <= len(saved):
            choice = saved[int(choice) - 1]["name"]
        old = self.cfg["TEAM_URL"]
        self.cfg.set("TEAM_URL", Csc.team_name_from(choice))
        try:
            team = self._resolve_team(quiet=False)
            self.cfg.set("TEAM_URL", team["name"])  # store the exact spelling CSC uses
        except CscError:
            self.cfg.set("TEAM_URL", old)
            if old:
                info(f"Still scouting {self.team_name or old}.")

    def do_matches(self, arg):
        """matches - list the team's CSC matches and whether you have each demo."""
        rows = self.ws.manifest()
        if not rows:
            warn("No matches scanned yet. Run `download`.")
            return
        idx = self.ws.have_index()
        only = self.cfg.parse_maps()
        if only:
            rows = [r for r in rows if r["Map"] in only]
        se = season_set(self.cfg["SEASON"])
        if se:
            rows = [r for r in rows if season_of(r.get("Competition")) in se]
        state = {"file": "yes", "data": "parsed only"}
        df = pd.DataFrame([{"#": i + 1, "date": r["Date"], "opponent": r["Opponent"], "map": r["Map"],
                            "demo": state.get(idx.get((r["MatchId"], r["Map"]), ("-",))[0], "-"),
                            "match": r["Competition"]} for i, r in enumerate(rows)])
        self.print_table(df, "League matches", rows=200)

    def csc_seasons(self, api):
        val = self.cfg["CSC_SEASONS"].strip().lower()
        if val == "all":
            return api.all_seasons()
        if val:
            return sorted(season_set(val.replace("s", "")))
        return [api.latest_season()]

    def do_download(self, arg):
        """download [force] - list the team's CSC matches and download every demo you don't have yet (force = re-download all)."""
        try:
            team = self._resolve_team(quiet=False)
            api = self.csc()
            seasons = self.csc_seasons(api)
        except CscError:
            return
        types = tuple(t.strip().lower() for t in self.cfg["MATCH_TYPES"].split(",") if t.strip()) or ("regular", "playoff")
        info(f"Looking up {team['name']}'s matches in season(s) {', '.join(map(str, seasons))}...")
        rows = api.manifest_rows(team, seasons, types, log=lambda x: print(C.s("    " + x, "d")))
        rows = rows[: self.cfg.int("MAX_MATCHES", 60) * 3]
        if not rows:
            warn("No finished matches with a demo yet." + ("" if self.cfg["CSC_SEASONS"] else
                 " Earlier seasons: `set CSC_SEASONS all` (or e.g. 20,21) and `download` again."))
            return
        self.ws.save_manifest(rows)
        good(f"{len(rows)} map demo(s) from {len({r['MatchId'] for r in rows})} match(es) listed in {self.ws.manifest_path.name}")
        only = self.cfg.parse_maps()
        if only:
            skipped = sum(r["Map"] not in only for r in rows)
            rows = [r for r in rows if r["Map"] in only]
            info(f"Map set to {', '.join(sorted(only))}: {skipped} demo(s) on other maps skipped.")
        force = "force" in arg
        copies = self.ws.all_copies() if force else {}
        rows = self._skip_existing(rows, force=force)
        ok = 0
        for r in rows:
            stem = demo_stem(Path(r["FileName"]))
            tmp = self.ws.downloads / r["FileName"]
            dst = self.ws.demos / safe_name(r["Map"]) / (stem + ".dem")
            try:
                info(f"Downloading {r['Date']} vs {r['Opponent']} ({r['Map']})")
                http_download(r["ResourceUrl"], tmp, r["Map"])
                decompress(tmp, dst)
                tmp.unlink(missing_ok=True)
                check_demo_file(dst)
                ok += 1
                for old in copies.get((r["MatchId"], r["Map"]), []):
                    if old.resolve() != dst.resolve():
                        old.unlink(missing_ok=True)
            except Exception as e:
                bad(f"{r['FileName']}: {e}")
                for leftover in (tmp, dst):
                    leftover.unlink(missing_ok=True)
        good(f"Downloaded {ok} new demo(s). Next: `parse`.")

    def do_getdemos(self, arg):
        """getdemos - same as `download` (CSC demos download directly)."""
        return self.do_download(arg)

    def _skip_existing(self, rows, force=False):
        """Drop matches we already have (any filename, old demo folder, or parsed data). Returns what's left."""
        if force:
            info(f"`download force`: re-downloading all {len(rows)} demo(s).")
            return rows
        idx = self.ws.have_index()
        todo, have_n, data_n, moved, broken_n = [], 0, 0, 0, 0
        for r in rows:
            hit = idx.get((r["MatchId"], r["Map"]))
            if hit and hit[0] == "broken":
                hit[1].unlink(missing_ok=True)  # incomplete earlier download
                broken_n += 1
                hit = None
            if not hit:
                todo.append(r)
                continue
            kind, path = hit
            if kind == "data":
                data_n += 1
                continue
            have_n += 1
            if self.ws.demos.resolve() not in path.resolve().parents:  # sitting in the old demo folder
                dst = self.ws.demos / safe_name(r["Map"]) / path.name
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), dst)
                    moved += 1
        msg = f"Already have {have_n} of {len(rows)} demo(s)"
        if data_n:
            msg += f", {data_n} more kept as parsed data (deleted demo files)"
        info(msg + f" - downloading {len(todo)} new." + (" (`download force` re-downloads everything)" if have_n + data_n else ""))
        if moved:
            info(f"Moved {moved} demo(s) from your old demo folder into {self.ws.demos}.")
        if broken_n:
            warn(f"{broken_n} incomplete demo file(s) found - downloading them again.")
        dups = self.ws.duplicate_files()
        if dups:
            warn(f"{len(dups)} duplicate demo file(s) on disk (same match saved twice). "
                 f"`delete duplicates` removes the extra copies ({human_size(sum(f.stat().st_size for f in dups))}).")
        return todo

    DEMO_RE = re.compile(r"\.dem(\.gz|\.zst)?$", re.I)

    def do_import(self, arg):
        """import [folder] | import watch [folder] - bring in demos from a folder (default SORT_FROM, your Downloads).
        `import watch` keeps watching and imports each demo as soon as it finishes downloading."""
        parts = arg.strip().split(None, 1)
        if parts and parts[0].lower() == "watch":
            return self.watch_imports(parts[1] if len(parts) > 1 else "")
        src = Path(arg.strip().strip('"') or self.cfg["SORT_FROM"]).expanduser()
        if not src.exists():
            bad(f"Folder not found: {src}")
            return
        files = [f for f in src.iterdir() if f.is_file() and self.DEMO_RE.search(f.name)]
        if not files:
            warn(f"No demo files in {src}")
            return
        self._routed = 0
        n, dupes, other, _ = self._import_files(files)
        if self._routed:
            info(f"Filed {self._routed} demo(s) under your other team(s) - switch with `team` to use them.")
        if dupes:
            info(f"Skipped {dupes} demo(s) you already have.")
        if other:
            info(f"Skipped {other} demo(s) on other maps (map set to {self.cfg['PARSE_MAP']}; `setmap all` to include them).")
        good(f"Imported {n} demo(s). Next: `parse` (unsorted demos are sorted by map while parsing).")

    def _import_files(self, files, idx=None):
        """Copy/unpack demo files into the demo folder. Returns (imported, duplicates, other maps, imported keys)."""
        man = self.ws.manifest()
        n = skipped_other = dupes = 0
        keys = []
        only = self.cfg.parse_maps()
        idx = self.ws.have_index() if idx is None else idx
        sizes = {p.stat().st_size for d in self.ws.demo_dirs() for p in d.rglob("*.dem")}
        others = self.ws.other_team_rows()
        for f in files:
            row = next((r for r in man if Path(r["ResourceUrl"]).name == f.name), None)
            by_match_id = row is None
            if row is None:
                row = next((r for r in man if r["MatchId"] in f.name), None)
            if row is None and others:  # a demo of another team you scout: file it under that team
                owner = next(((t, r) for t, rows in others.items() for r in rows
                              if Path(r["ResourceUrl"]).name == f.name or (r["MatchId"] and r["MatchId"] in f.name)), None)
                if owner:
                    t, r = owner
                    dst = self.ws.demo_base / safe_name(t) / safe_name(r["Map"]) / (demo_stem(Path(r["FileName"])) + ".dem")
                    if dst.exists() and dst.stat().st_size >= MIN_DEMO_BYTES:
                        dupes += 1
                        continue
                    try:
                        decompress(f, dst)
                        self._routed = getattr(self, "_routed", 0) + 1
                        print(f"  {C.s('+', 'c')} {f.name} -> {t}: {r['Date']} vs {r['Opponent']} ({r['Map']})")
                    except Exception as e:
                        bad(f"{f.name}: {e}")
                    continue
            # series: matched only by match id, so pick the map from FACEIT's filename 1-<match>-1-<n>.dem.gz
            if row is not None and by_match_id:
                mi = re.search(r"-(\d+)\.dem", f.name)
                cands = [r for r in man if r["MatchId"] == row["MatchId"]]
                if mi and len(cands) > 1:
                    row = next((r for r in cands if r["MapIndex"] == mi.group(1)), row)
            if row and (row["MatchId"], row["Map"]) in idx and idx[(row["MatchId"], row["Map"])][0] != "broken":
                dupes += 1
                continue
            if row is None and f.suffix.lower() == ".dem" and f.stat().st_size in sizes:
                dupes += 1  # byte-for-byte same size as a demo we have: same file
                continue
            if row and only and row["Map"] not in only:
                skipped_other += 1
                continue
            if row:
                stem = demo_stem(Path(row["FileName"]))
                dst = self.ws.demos / safe_name(row["Map"]) / (stem + ".dem")
            else:
                dst = self.ws.demos / "_unsorted" / (demo_stem(f) + ".dem")
            if dst.exists() and dst.stat().st_size >= MIN_DEMO_BYTES:
                continue
            try:
                decompress(f, dst)
                n += 1
                if row:
                    idx[(row["MatchId"], row["Map"])] = ("file", dst)
                    keys.append((row["MatchId"], row["Map"]))
                label = f"{row['Date']} vs {row['Opponent']} ({row['Map']})" if row else "not in the match list"
                print(f"  {C.s('+', 'g')} {f.name} -> {label}")
            except Exception as e:
                bad(f"{f.name}: {e}")
        return n, dupes, skipped_other, keys

    def missing_matches(self):
        """League match rows with a demo on CSC that we don't have yet (respecting setmap)."""
        idx = self.ws.have_index()
        only = self.cfg.parse_maps()
        rows = [r for r in self.ws.manifest() if (r["MatchId"], r["Map"]) not in idx
                or idx[(r["MatchId"], r["Map"])][0] == "broken"]
        if only:
            rows = [r for r in rows if r["Map"] in only]
        se = season_set(self.cfg["SEASON"])
        if se:
            rows = [r for r in rows if season_of(r.get("Competition")) in se]
        return rows

    def open_rooms(self, limit=0):
        import webbrowser
        rows = self.missing_matches()
        rooms = list(dict.fromkeys(r["RoomUrl"] for r in rows))
        if not rooms:
            good("You have every league demo in the match list" + (f" on {self.cfg['PARSE_MAP']}" if self.cfg.parse_maps() else "") + ".")
            return []
        if limit:
            rooms = rooms[:limit]
        elif len(rooms) > 12 and self.interactive:
            a = self.ask(f"{len(rooms)} rooms to open. Open all, or how many? (all / number)", "all")
            if a.isdigit():
                rooms = rooms[:int(a)]
        info(f"Opening {len(rooms)} match room(s) in your browser - click WATCH DEMO in each tab.")
        for i, url in enumerate(rooms, 1):
            r = next(x for x in rows if x["RoomUrl"] == url)
            maps = ", ".join(x["Map"].replace("de_", "") for x in rows if x["RoomUrl"] == url)
            print(f"   {i:>2}. {r['Date']} vs {r['Opponent']} ({maps})")
            try:
                webbrowser.open_new_tab(url)
            except Exception as e:
                bad(f"Couldn't open the browser ({e}). Link: {url}")
            time.sleep(0.4)
        return rooms

    def watch_imports(self, folder="", stop_when=0, idle_minutes=15):
        """Watch a folder and import each demo as it finishes downloading (Ctrl+C to stop)."""
        src = Path(folder.strip().strip('"') or self.cfg["SORT_FROM"]).expanduser()
        if not src.exists():
            bad(f"Folder not found: {src}")
            return
        def snapshot():
            return {f: f.stat().st_size for f in src.iterdir() if f.is_file() and self.DEMO_RE.search(f.name)}
        done = set(snapshot())  # files already there are handled by plain `import`
        pending, total, last_new = {}, 0, time.time()
        idx = self.ws.have_index()
        info(f"Watching {src} for new demos - press Ctrl+C when you're done"
             + (f" (stops by itself after {stop_when})" if stop_when else "") + ".")
        try:
            while True:
                now = snapshot()
                for f, size in now.items():
                    if f in done:
                        continue
                    if pending.get(f) == size and size > 0:  # size unchanged since last check: download finished
                        n, dupes, other, _ = self._import_files([f], idx)
                        done.add(f)
                        pending.pop(f, None)
                        total += n
                        last_new = time.time()
                        if dupes:
                            print(C.s(f"  = {f.name}: already have it", "d"))
                        if other:
                            print(C.s(f"  = {f.name}: other map, skipped", "d"))
                        if n and stop_when:
                            print(C.s(f"    {total}/{stop_when}", "d"))
                    else:
                        pending[f] = size
                if stop_when and total >= stop_when:
                    break
                if time.time() - last_new > idle_minutes * 60:
                    info(f"No new demos for {idle_minutes} minutes - stopping.")
                    break
                time.sleep(2)
        except KeyboardInterrupt:
            print()
        good(f"Imported {total} demo(s) while watching.")
        if total and self.interactive and self.ask("Parse them now? (Y/n)", "y").lower().startswith("y"):
            self.do_parse("")
        elif total:
            print(C.s("    Next: `parse`", "d"))
        return total

    def do_parse(self, arg):
        """parse [force] - parse new demos into the analysis cache (force = re-parse everything)."""
        if missing_modules(["demoparser2"]) and not pip_install(["demoparser2"], self.assume_yes, "demo parsing"):
            bad("demoparser2 is required to parse demos.")
            return
        force = "force" in arg
        demos = self.ws.demo_files()
        if not demos:
            warn(f"No demos in {self.ws.demos}. Use `download` or `import`.")
            return
        only = self.cfg.parse_maps()
        if only:
            keep = [d for d in demos if self.demo_map(d) in only]
            if len(keep) < len(demos):
                info(f"Map set to {', '.join(sorted(only))}: skipping {len(demos) - len(keep)} demo(s) on other maps.")
            demos = keep
            if not demos:
                warn(f"No demos on {', '.join(sorted(only))}. `setmap all` to parse everything.")
                return
        targets = self.ws.targets()
        fallback = {p.strip() for p in self.cfg["PLAYERS"].split(",") if p.strip()}
        man = self.ws.manifest()
        roster_sids = {x for r in man for x in r.get("TeamSteamIds", "").split(";") if x}
        roster_nicks = {x.lower() for r in man for x in r.get("TeamNicks", "").split(";") if x}
        try:
            roster_nicks |= {m["nickname"].lower() for m in json.loads((self.ws.team_root / "team.json").read_text())["members"]}
        except Exception:
            pass
        done = failed = 0
        for d in demos:
            cache = self.ds.cache_file(d)
            if cache.exists() and not force and cache.stat().st_mtime >= d.stat().st_mtime:
                try:
                    if pd.read_pickle(cache).get("version") == PARSE_VERSION:
                        continue
                except Exception:
                    pass
            row = self.ws.manifest_for(d)
            stem = demo_stem(d)
            sids = set(targets.get(stem, [])) or {s for s in (row or {}).get("TeamSteamIds", "").split(";") if s}
            sids |= {p for p in fallback if p.isdigit() and len(p) == 17}
            nicks = {n.lower() for n in (row or {}).get("TeamNicks", "").split(";") if n}
            nicks |= {p.lower() for p in fallback if not (p.isdigit() and len(p) == 17)}
            if not sids and not nicks:  # demo not in the match list: use everyone known on the roster
                sids = roster_sids
                nicks = roster_nicks
            info(f"Parsing {d.name}")
            t0 = time.time()
            try:
                res = parse_demo(d, self.cfg.float("SAMPLE_SECONDS", 2), sids, nicks, self._choose_team)
            except DemoParseError as e:
                bad(f"  {d.name}: {e}")
                failed += 1
                continue
            except Exception as e:
                bad(f"  {d.name}: {type(e).__name__}: {e}")
                if os.environ.get("ESEA_DEBUG"):
                    traceback.print_exc()
                failed += 1
                continue
            self.ws.save_target(stem, res["core"])
            # move unsorted demos into their map folder
            if d.parent.name in ("_unsorted", "unknown_map") and res["map"]:
                nd = self.ws.demos / res["map"] / d.name
                nd.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), nd)
                d = nd
                cache = self.ds.cache_file(d)
            ts = d.stat().st_mtime
            meta = dict(demo=stem, map=res["map"], match_id=(row or {}).get("MatchId", stem),
                        date=(row or {}).get("Date") or datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
                        opponent=(row or {}).get("Opponent") or self._opp_name(res),
                        rounds=len(res["rounds"]))
            pd.to_pickle(dict(version=PARSE_VERSION, meta=meta, rounds=res["rounds"], kills=res["kills"],
                              nades=res["nades"], pos=res["pos"], dmg=res["dmg"], names=res["names"]), cache)
            cache.with_suffix(".meta.json").write_text(json.dumps(meta, default=str))
            R = res["rounds"]
            good(f"  {res['map']}: {len(R)} rounds ({int(R['tgt_won'].sum())}-{int((~R['tgt_won']).sum())}), "
                 f"{len(res['kills'])} kills, {len(res['nades'])} nades  [{time.time() - t0:.0f}s]")
            done += 1
        if done == 0 and failed == 0:
            info("Everything is already parsed (use `parse force` to redo).")
        self.ds.load()

    def _opp_name(self, res):
        """Best guess for demos imported without match info: name the opponent after its players."""
        core = {res["names"].get(s, s) for s in res["core"]}
        for names in res["groups"].values():
            if not core & set(names):
                return "team of " + ", ".join(sorted(names)[:2])
        return "Unknown"

    def _choose_team(self, map_name, groups):
        if not self.interactive:
            return None
        print(C.s(f"\n  Which team are you scouting in this {map_name} demo?", "bold"))
        keys = list(groups)
        for i, tn in enumerate(keys, 1):
            print(f"   [{i}] {', '.join(groups[tn][1])}")
        a = self.ask("Number", "1")
        try:
            return keys[int(a) - 1]
        except Exception:
            return None

    def do_inspect(self, arg):
        """inspect <demo name> - dump what the parser sees in a demo (for troubleshooting)."""
        if missing_modules(["demoparser2"]):
            bad("demoparser2 not installed.")
            return
        demos = [d for d in self.ws.demo_files() if arg.strip().lower() in d.name.lower()] or self.ws.demo_files()[:1]
        if not demos:
            bad("No demos.")
            return
        from demoparser2 import DemoParser
        d = demos[0]
        p = DemoParser(str(d))
        print(C.s(d.name, "bold"))
        print("header:", json.dumps(p.parse_header(), default=str)[:600])
        try:
            evs = p.list_game_events()
            print("events:", ", ".join(sorted(evs)))
        except Exception as e:
            print("events: n/a", e)
        for e, pl in (("player_death", ["X", "team_num", "last_place_name"]), ("round_end", []), ("player_blind", ["team_num"])):
            try:
                df = p.parse_event(e, player=pl, other=[])
                print(f"{e}: {len(df)} rows, columns: {list(df.columns)}")
            except Exception as ex:
                print(f"{e}: error {ex}")
        try:
            g = p.parse_grenades()
            print(f"grenades: {len(g)} rows, columns: {list(g.columns)}; types: {sorted(g.iloc[:, :].filter(like='type').iloc[:, 0].astype(str).unique())[:8]}")
        except Exception as ex:
            print("grenades: error", ex)

    def do_radars(self, arg):
        """radars [update] - download the official CS2 radar images used behind every heatmap."""
        maps = self.radar.download(force="update" in arg or "force" in arg)
        self.radar = Radar(self.ws, self.cfg)
        self.an.radar = self.radar
        found = [m for m in sorted(self.radar.meta) if self.radar.find(m) is not None]
        if found:
            good(f"Radar images for: {', '.join(found)}")
            ver = self.radar.dir / "VERSION"
            if ver.exists():
                print(C.s(f"    CS2 build {ver.read_text().strip()} - `radars update` after a map update", "d"))
        else:
            print(f"    You can also drop radar PNGs named like de_mirage.png into {self.radar.dir}")

    # ---------------------------------------------------------------- delete
    def _demo_inventory(self):
        """Every demo we know about: files on disk and/or parsed data, numbered newest first."""
        items = {}
        meta = {}
        if self.ds.loaded:
            for r in self.ds.matches.to_dict("records"):
                meta[r["demo"]] = r
        for d in self.ws.demo_files():
            st = demo_stem(d)
            row = self.ws.manifest_for(d) or {}
            m = meta.get(st, {})
            items[st] = dict(stem=st, file=d, size=d.stat().st_size,
                             map=m.get("map") or row.get("Map") or (d.parent.name if d.parent.name.startswith("de_") else "?"),
                             date=m.get("date") or row.get("Date") or datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d"),
                             opponent=m.get("opponent") or row.get("Opponent") or "?")
        for c in self.ws.cache.glob("*.pkl"):
            st = c.stem
            it = items.setdefault(st, dict(stem=st, file=None, size=0, map=meta.get(st, {}).get("map", "?"),
                                           date=meta.get(st, {}).get("date", "?"),
                                           opponent=meta.get(st, {}).get("opponent", "?")))
            it["cache"] = c
        out = sorted(items.values(), key=lambda x: (str(x["date"]), x["stem"]), reverse=True)
        for i, it in enumerate(out, 1):
            it["n"] = i
            it.setdefault("cache", None)
        return out

    def _select(self, items, spec):
        """'3', '1-4,7', 'mirage', 'all', 'data-only', 'unparsed', 'older 30' -> chosen items."""
        spec = spec.strip().lower()
        if spec in ("all", "*"):
            return list(items)
        if spec in ("data-only", "dataonly"):
            return [i for i in items if i["file"] is None]
        if spec == "unparsed":
            return [i for i in items if i["cache"] is None]
        m = re.match(r"older\s+(\d+)", spec)
        if m:
            cutoff = datetime.now().timestamp() - int(m.group(1)) * 86400
            res = []
            for i in items:
                try:
                    if datetime.strptime(str(i["date"]), "%Y-%m-%d").timestamp() < cutoff:
                        res.append(i)
                except ValueError:
                    pass
            return res
        chosen = set()
        for tok in re.split(r"[,\s]+", spec):
            if not tok:
                continue
            if re.fullmatch(r"\d+-\d+", tok):
                a, b = map(int, tok.split("-"))
                chosen |= set(range(min(a, b), max(a, b) + 1))
            elif tok.isdigit():
                chosen.add(int(tok))
            else:
                mp = norm_map(tok)
                chosen |= {i["n"] for i in items if i["map"] == mp}
        return [i for i in items if i["n"] in chosen]

    def do_delete(self, arg):
        """delete [what] [--data|--keep-data] [--yes] - delete demos. what: 3 | 1-4,7 | mirage | all | older 30 | unparsed | data-only | duplicates | images"""
        parts = shlex.split(arg) if arg else []
        flags = {p.lower() for p in parts if p.startswith("-")}
        spec = " ".join(p for p in parts if not p.startswith("-"))
        if spec.lower() in ("images", "heatmaps", "output"):
            return self._delete_images(flags)
        if spec.lower() in ("duplicates", "dupes", "dups"):
            return self._delete_duplicates(flags)
        items = self._demo_inventory()
        if not items:
            warn("No demos or parsed data to delete.")
            return
        if not spec:
            df = pd.DataFrame([{"#": i["n"], "date": i["date"], "map": i["map"], "opponent": i["opponent"],
                                "file": f"{i['size'] / 1e6:.0f} MB" if i["file"] else "-",
                                "parsed": "yes" if i["cache"] else "-"} for i in items])
            self.print_table(df, f"Demos ({human_size(sum(i['size'] for i in items))} in {self.ws.demos})", rows=500)
            print(C.s("  Choose: 3 | 1-4,7 | mirage | all | older 30 | unparsed | data-only | duplicates | images"
                      "   (`purge` wipes the whole demo folder)", "d"))
            spec = self.ask("Delete which")
            if not spec:
                return
            if spec.lower() in ("images", "heatmaps", "output"):
                return self._delete_images(flags)
            if spec.lower() in ("duplicates", "dupes", "dups"):
                return self._delete_duplicates(flags)
        chosen = self._select(items, spec)
        if not chosen:
            warn(f"Nothing matches '{spec}'.")
            return
        files = [i for i in chosen if i["file"]]
        caches = [i for i in chosen if i["cache"]]
        size = sum(i["size"] for i in files) / 1e9
        print(C.s(f"\n  Selected {len(chosen)}:", "bold"))
        for i in chosen[:12]:
            print(f"    #{i['n']}  {i['date']}  {i['map']:<11} vs {i['opponent']}"
                  + ("" if i["file"] else C.s("  (data only)", "d")))
        if len(chosen) > 12:
            print(C.s(f"    ... and {len(chosen) - 12} more", "d"))
        # remove parsed data as well?
        if "--data" in flags or "--all" in flags:
            drop_data = True
        elif "--keep-data" in flags:
            drop_data = False
        elif not files:  # only data-only entries chosen: deleting data is the point
            drop_data = True
        else:
            drop_data = self.ask("Also delete their parsed data? Keeping it keeps them in the analysis (y/N)",
                                 "n").lower().startswith("y")
        what = []
        if files:
            what.append(f"{len(files)} demo file(s), {human_size(size * 1e9)}")
        if drop_data and caches:
            what.append(f"parsed data for {len(caches)}")
        if not what:
            warn("Nothing to delete (those have no demo file; add --data to remove their parsed data).")
            return
        if "--yes" not in flags and "-y" not in flags:
            if not self.interactive:
                bad("Refusing to delete without confirmation in scripted mode - add --yes.")
                return
            if self.ask(f"Permanently delete {' and '.join(what)}? Type yes to confirm").lower() != "yes":
                info("Nothing deleted.")
                return
        freed, nd, nc = 0, 0, 0
        for i in chosen:
            if i["file"]:
                try:
                    freed += i["size"]
                    i["file"].unlink()
                    nd += 1
                except OSError as e:
                    bad(f"{i['file'].name}: {e.strerror or e}")
            if drop_data and i["cache"]:
                try:
                    i["cache"].unlink()
                    i["cache"].with_suffix(".meta.json").unlink(missing_ok=True)
                    nc += 1
                except OSError as e:
                    bad(f"{i['cache'].name}: {e.strerror or e}")
                t = self.ws.targets()
                if i["stem"] in t:
                    t.pop(i["stem"])
                    self.ws.targets_path.write_text(json.dumps(t, indent=2))
        for d in sorted((p for p in self.ws.demos.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            try:
                d.rmdir()  # only removes empty map folders
            except OSError:
                pass
        good(f"Deleted {nd} demo file(s) ({human_size(freed)} freed)" + (f" and parsed data for {nc}." if nc else "."))
        if nd and not drop_data:
            info("Their parsed data is kept, so they still count in views and the report.")
        if nc:
            self.ds.load(quiet=True)

    def do_purge(self, arg):
        """purge [--data] [--yes] - delete ALL demo files in the demo folder (--data also wipes parsed data)."""
        flags = {x.lower() for x in arg.split()}
        exts = (".dem", ".gz", ".zst", ".part", ".bz2", ".zip")
        files = []
        for d in self.ws.demo_dirs():
            files += [f for f in d.rglob("*") if f.is_file() and (f.name.lower().endswith(exts) or "_downloads" in f.parts)]
        caches = list(self.ws.cache.glob("*.pkl")) if "--data" in flags else []
        if not files and not caches:
            good(f"Nothing to purge - no demo files in {self.ws.demos}.")
            return
        size = sum(f.stat().st_size for f in files)
        print(C.s("\n  PURGE", "r", "bold"))
        for d in self.ws.demo_dirs():
            n = sum(1 for f in files if d in f.parents)
            if n:
                print(f"    {n} demo file(s) in {d}")
        print(f"    {human_size(size)} total")
        if caches:
            print(f"    + parsed data for {len(caches)} demo(s) (all views, heatmaps and the report will be empty)")
        else:
            print(C.s("    Parsed data is kept, so your views and report still work. Add --data to wipe it too.", "d"))
        if "--yes" not in flags and "-y" not in flags:
            if not self.interactive:
                bad("Refusing to purge without confirmation in scripted mode - add --yes.")
                return
            if self.ask("This can't be undone. Type purge to confirm").strip().lower() != "purge":
                info("Nothing deleted.")
                return
        failed = 0
        for f in files:
            try:
                f.unlink()
            except OSError as e:
                failed += 1
                bad(f"{f.name}: {e.strerror or e}")
        for d in self.ws.demo_dirs():
            for sub in sorted((x for x in d.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)):
                try:
                    sub.rmdir()
                except OSError:
                    pass
        for c in caches:
            c.unlink(missing_ok=True)
            c.with_suffix(".meta.json").unlink(missing_ok=True)
        if caches:
            self.ws.targets_path.unlink(missing_ok=True)
        good(f"Purged {len(files) - failed} demo file(s), {human_size(size)} freed"
             + (f", and parsed data for {len(caches)} demo(s)." if caches else "."))
        self.ds.load(quiet=True)
        if caches:
            self.ds.loaded = False

    def _delete_duplicates(self, flags):
        dups = self.ws.duplicate_files()
        if not dups:
            good("No duplicate demos - every match/map is stored once.")
            return
        size = sum(f.stat().st_size for f in dups)
        print(C.s(f"\n  {len(dups)} extra copies ({human_size(size)}):", "bold"))
        for f in dups[:15]:
            print(f"    {f}")
        if "--yes" not in flags and "-y" not in flags:
            if not self.interactive:
                bad("Refusing to delete without confirmation in scripted mode - add --yes.")
                return
            if self.ask("Delete these extra copies (one copy of each match is kept)? Type yes to confirm").lower() != "yes":
                info("Nothing deleted.")
                return
        for f in dups:
            f.unlink(missing_ok=True)
            c = self.ds.cache_file(f)
            if c.exists():  # its parsed data duplicates the kept copy's
                c.unlink()
                c.with_suffix(".meta.json").unlink(missing_ok=True)
        good(f"Removed {len(dups)} duplicate(s), {human_size(size)} freed.")
        self.ds.load(quiet=True)

    def _delete_images(self, flags):
        folders = [self.ws.out / "heatmaps", self.ws.out / "report" / "img"]
        files = [f for d in folders if d.exists() for f in d.glob("*.png")]
        if not files:
            info("No generated images to delete.")
            return
        size = sum(f.stat().st_size for f in files) / 1e6
        if "--yes" not in flags and "-y" not in flags:
            if not self.interactive:
                bad("Refusing to delete without confirmation in scripted mode - add --yes.")
                return
            if self.ask(f"Delete {len(files)} generated heatmap image(s) ({size:.0f} MB)? Type yes to confirm").lower() != "yes":
                info("Nothing deleted.")
                return
        for f in files:
            f.unlink(missing_ok=True)
        good(f"Deleted {len(files)} image(s). Reports already built keep their embedded copies.")

    def complete_delete(self, text, *a):
        opts = ["all", "images", "duplicates", "unparsed", "data-only", "older", "--data", "--keep-data", "--yes"] + [m[3:] for m in MAP_DATA]
        return [o for o in opts if o.startswith(text)]

    def do_status(self, arg):
        """status - workspace, team, demos and parse state."""
        print(C.s("\n  Workspace  ", "bold") + str(self.ws.root))
        print(C.s("  Team data  ", "bold") + str(self.ws.team_root))
        print(C.s("  Demos      ", "bold") + str(self.ws.demos))
        print(C.s("  Team       ", "bold") + (self.team_name or C.s("not set (`team <url>`)", "y")))
        man = self.ws.manifest()
        demos = self.ws.demo_files()
        parsed = {f.stem for f in self.ws.cache.glob("*.pkl")}
        size = sum(d.stat().st_size for d in demos) / 1e9
        data_only = len(parsed - {demo_stem(d) for d in demos})
        print(C.s("  Matches    ", "bold") + f"{len(man)} listed, {len(demos)} demo file(s) ({human_size(size * 1e9)}), "
              f"{sum(demo_stem(d) in parsed for d in demos)} parsed"
              + (f", +{data_only} kept as data only" if data_only else ""))
        if self.ds.loaded:
            by = self.ds.matches.groupby("map").size()
            print(C.s("  Maps       ", "bold") + ", ".join(f"{m} ({n})" for m, n in by.items()))
            print(C.s("  Players    ", "bold") + ", ".join(self.ds.label(n) for n in self.ds.tgt_names(all_players=True)))
        found = [m for m in MAP_DATA if self.radar.find(m) is not None]  # find() never triggers a download
        print(C.s("  Radars     ", "bold") + (", ".join(found) if found else C.s("none (`radars`)", "y")))
        self.do_filters("")
        print()

    def do_maps(self, arg):
        """maps - parsed maps and how many demos each."""
        if not self.need_data():
            return
        R = self.ds.rounds
        df = R.groupby("map").agg(demos=("demo", "nunique"), rounds=("round", "size"),
                                  win=("tgt_won", "mean")).reset_index()
        df["win%"] = (100 * df.pop("win")).round(0)
        self.print_table(df, "Parsed maps")

    # ---------------------------------------------------------------- views
    def _view_key(self, s):
        s = s.strip().lower()
        if not s:
            return None
        if s.isdigit() and 1 <= int(s) <= len(VIEWS):
            return VIEWS[int(s) - 1][0]
        s = VIEW_ALIASES.get(s, s)
        return s if s in {k for k, _ in VIEWS + HIDDEN_VIEWS} else None

    def build_view(self, key):
        a = self.an
        if key == "overview":
            return a.v_overview()
        if key == "openings":
            return a.v_openings()
        if key == "players":
            return a.v_players()
        if key in ("smoke", "flash", "molotov", "he"):
            return a.v_utility(key)
        if key.startswith("pos_"):
            return a.v_positions(key[4:])
        if key == "setups":
            return a.v_setups()
        if key == "executes":
            return a.v_executes()
        if key == "economy":
            return a.v_economy()
        if key == "trends":
            return a.v_trends()
        if key == "anomalies":
            v = View("anomalies", "Anomalies")
            v.table("What stands out", pd.DataFrame(a.anomalies()).drop(columns=["score"], errors="ignore"))
            return v
        if key == "vetos":
            return self.veto_view()
        if key == "postplant":
            return a.v_postplant()
        if key == "roles":
            return a.v_roles()
        if key == "lowbuy":
            return a.v_lowbuy()
        if key == "power":
            return a.v_power()
        if key == "rotations":
            return a.v_rotations(team_only=getattr(self, "_in_report", False))
        if key == "counter":
            v = View("counter", "Counter-play suggestions")
            v.table("Suggestions (highest priority first)", pd.DataFrame(a.counterplay()))
            return v
        raise KeyError(key)

    def do_show(self, arg):
        """show [n|name|options|views] - numbered data views; `show 4` or `show smokes` opens one."""
        a = arg.strip().lower()
        if a in ("options", "opts"):
            return self.do_options("")
        if a in ("", "views", "modules"):
            print(C.s(f"\n  Data views  ({self.an.scope_label()})", "bold"))
            for i, (k, label) in enumerate(VIEWS, 1):
                print(f"  {C.s('[' + str(i).rjust(2) + ']', 'c', 'bold')} {label}  {C.s(k, 'd')}")
            if not a and self.interactive:
                choice = self.ask("Choose a number to show (blank to cancel, home = main menu)")
                if choice:
                    self.show_view(self._view_key(choice) or choice, ask_plot=True)
            return
        parts = a.split()
        plot = "plot" in parts[1:]
        key = self._view_key(parts[0])
        if not key:
            bad(f"Unknown view {parts[0]}. `show` lists them.")
            return
        self.show_view(key, plot=plot, ask_plot=not plot)

    def complete_show(self, text, *a):
        return [k for k in [k for k, _ in VIEWS] + ["options"] if k.startswith(text)]

    def show_view(self, key, plot=False, ask_plot=False):
        if key not in {k for k, _ in VIEWS + HIDDEN_VIEWS}:
            bad(f"Unknown view {key}")
            return
        if key == "vetos":
            return self.do_vetos("")
        if not self.need_data():
            return
        v = self.build_view(key)
        self.last = v
        if self.cfg["OUTPUT"] in ("json", "csv"):
            self.emit_machine(v)
        else:
            print(C.s(f"\n=== {v.title} ===", "bold", "c") + C.s(f"   ({self.an.scope_label()})", "d"))
            for sub, df in v.tables:
                self.print_table(df, sub)
            for n in v.notes:
                print(C.s("  * ", "y") + show_text(n))
        if v.plots:
            if plot:
                self.do_plot("")
            elif ask_plot and self.interactive and self.cfg["OUTPUT"] == "table":
                if self.ask("Draw the heatmap? (y/N)", "n").lower().startswith("y"):
                    self.do_plot("")
            elif self.cfg["OUTPUT"] == "table":
                print(C.s("  (type `plot` for the heatmap)", "d"))

    def emit_machine(self, v):
        if self.cfg["OUTPUT"] == "json":
            obj = {"view": v.key, "title": v.title, "scope": self.an.scope_label(), "notes": [show_text(n) for n in v.notes],
                   "tables": {sub: json.loads(show_names(df).to_json(orient="records")) if len(df) else [] for sub, df in v.tables}}
            print(json.dumps(obj, indent=2, default=str))
        else:
            for sub, df in v.tables:
                print(f"# {sub}")
                print(show_names(df).to_csv(index=False), end="")

    def print_table(self, df, title="", rows=None):
        rows = rows or self.cfg.int("ROWS", 15)
        if title:
            print(C.s(f"\n  {title}", "bold"))
        if df is None or not len(df):
            print(C.s("    (no data)", "d"))
            return
        d = show_names(df.head(rows)).copy()
        for c in d.columns:
            if d[c].dtype.kind == "f":
                d[c] = d[c].map(lambda x: "-" if pd.isna(x) else (f"{x:.0f}" if abs(x) >= 100 or float(x).is_integer() else f"{x:.1f}"))
            else:
                d[c] = d[c].map(lambda x: "-" if x is None or (isinstance(x, float) and math.isnan(x)) else str(x))
        width = shutil.get_terminal_size((140, 20)).columns
        cols = list(d.columns)
        w = {c: min(48, max(len(str(c)), d[c].str.len().max() if len(d) else 0)) for c in cols}
        long_cols = [c for c in cols if c in ("suggestion", "text", "evidence", "veto")]
        if long_cols:  # wrap the prose columns
            for _, r in d.iterrows():
                head = "  ".join(f"{c}: {r[c]}" for c in cols if c not in long_cols)
                print("  " + C.s("- ", "c") + C.s(head, "d"))
                for c in long_cols:
                    for line in textwrap.wrap(str(r[c]), width=max(60, width - 8)):
                        print("      " + line)
            if len(df) > rows:
                print(C.s(f"    ... {len(df) - rows} more (set ROWS {len(df)} or export)", "d"))
            return
        line = "  ".join(str(c).ljust(w[c])[:w[c]] for c in cols)
        print("    " + C.s(line, "c"))
        print("    " + "  ".join("-" * w[c] for c in cols))
        for _, r in d.iterrows():
            print("    " + "  ".join(str(r[c]).ljust(w[c])[:w[c]] for c in cols))
        if len(df) > rows:
            print(C.s(f"    ... {len(df) - rows} more rows (set ROWS {len(df)}, or `export`)", "d"))

    def do_plot(self, arg):
        """plot - draw the heatmap(s) for the last view or search."""
        if self.last is None:
            bad("Nothing to plot yet. `show <n>` or `search ...` first.")
            return
        if not self.last.plots:
            warn("This view has no map plot.")
            return
        out = self.ws.out / "heatmaps"
        made = []
        for label, fn in self.last.plots:
            made += fn(out)
        if not made:
            warn("Nothing to draw with the current filters.")
            return
        good(f"{len(made)} heatmap(s) saved: " + ", ".join(Path(p).name for p in made))
        print(C.s("[+] ", "g", "bold") + f"Heatmaps can be found at {out}")

    def do_heatmap(self, arg):
        """heatmap <fkfd|smoke|flash|molotov|he|early|mid|late|postplant|rotations [player]|ecos|kills|deaths> - draw one."""
        a = arg.strip().lower() or self.ask("Which (fkfd, smoke, flash, molotov, he, early, mid, late, postplant, rotations, ecos, kills, deaths)", "fkfd")
        if a in ("kills", "deaths", "kill", "death"):
            return self.do_search(a.rstrip("s") + " plot")
        key = self._view_key(a)
        parts = a.split(None, 1)
        if parts and parts[0] in ("rotations", "rotation", "rot", "rotate") and len(parts) > 1:
            if not self.need_data():
                return
            n = self._pick_player(parts[1])
            if not n:
                return
            old = self.cfg.v["PLAYER"]
            self.cfg.v["PLAYER"] = n
            try:
                self.last = self.build_view("rotations")
                self.do_plot("")
            finally:
                self.cfg.v["PLAYER"] = old
            return
        if not key or key not in ("openings", "smoke", "flash", "molotov", "he", "pos_early", "pos_mid", "pos_late",
                                  "postplant", "rotations", "lowbuy", "power"):
            bad("Choose: fkfd, smoke, flash, molotov, he, early, mid, late, postplant, rotations [player], ecos, kills, deaths")
            return
        if not self.need_data():
            return
        self.last = self.build_view(key)
        self.do_plot("")

    def complete_heatmap(self, text, *a):
        return [k for k in ("fkfd", "smoke", "flash", "molotov", "he", "early", "mid", "late", "postplant", "rotations", "ecos", "power", "kills", "deaths") if k.startswith(text)]

    # ---------------------------------------------------------------- search
    def do_search(self, arg):
        """search <words> [key:value] - e.g. search smoke window | search fd mirage ct | search molly side:t phase:early | add `plot`"""
        if not self.need_data():
            return
        q = arg.strip() or self.ask("Search (e.g. smoke window, fk mirage, molly banana)")
        if not q:
            return
        I = self.ds.index
        if I is None or not len(I):
            warn("Nothing indexed.")
            return
        I = I[I["demo"].isin(self.ds.demos_in_scope())]
        if self.cfg["SIDE"]:
            I = I[I["side"] == self.cfg["SIDE"]]
        if self.cfg["PHASE"]:
            I = I[I["phase"] == self.cfg["PHASE"]]
        if self.cfg["PLAYER"]:
            I = I[I["player"].str.lower() == self.cfg["PLAYER"].lower()]
        do_plot = False
        types = {"smoke", "flash", "molotov", "he", "kill", "death", "fk", "fd"}
        for tok in shlex.split(q):
            t = tok.lower()
            if t == "plot":
                do_plot = True
                continue
            if ":" in t:
                k, v = t.split(":", 1)
                colmap = {"side": "side", "phase": "phase", "map": "map", "player": "player", "place": "place",
                          "from": "from", "type": "type", "vs": "opponent", "round": "round"}
                c = colmap.get(k)
                if not c:
                    bad(f"Unknown key {k}: (use side, phase, map, player, place, from, type, vs, round)")
                    return
                if c == "side":
                    I = I[I["side"].fillna("").str.lower() == v]
                elif c == "round":
                    I = I[I["round"].astype(str) == v]
                else:
                    I = I[I[c].fillna("").astype(str).str.lower().str.contains(v, regex=False)]
                continue
            t = SEARCH_ALIASES.get(t, t)
            if t in types:
                I = I[I["type"] == t] if t not in ("kill", "death") else I[I["type"].isin([t, "fk" if t == "kill" else "fd"])]
            elif t in ("t", "ct"):
                I = I[I["side"].fillna("").str.lower() == t]
            else:
                I = I[I["text"].str.contains(t, regex=False)]
        if not len(I):
            warn("No matches.")
            return
        if self.cfg["ROSTER"] in ("main", "lineup"):
            mains = set(self.ds.tgt_names())
            I = I[I["type"].isin(["kill", "death", "fk", "fd", "smoke", "flash", "molotov", "he"]) & I["player"].isin(mains)]
            if not len(I):
                warn("No matches (ROSTER filter is on).")
                return
        I = I.assign(roster=I["player"].map(lambda n: self.ds.status_of(n)))
        cols = ["type", "map", "side", "phase", "round", "t", "player", "roster", "from", "place", "detail", "opponent", "date"]
        summary = (I.groupby(["type", "map", "side", "place"]).agg(n=("round", "size"), t_med=("t", "median"),
                                                                     players=("player", lambda s: ", ".join(s.value_counts().index[:3])))
                   .reset_index().sort_values("n", ascending=False))
        summary["t_med"] = summary["t_med"].round(0)
        v = View("search", f"Search: {q}")
        v.table(f"{len(I)} result(s) - grouped", summary)
        v.table("Results", I.sort_values(["date", "round", "t"], ascending=[False, True, True])[cols])
        self._attach_search_plot(v, I, " ".join(t for t in shlex.split(q) if t.lower() != "plot"))
        self.last = v
        self.last_search = I
        if self.cfg["OUTPUT"] in ("json", "csv"):
            self.emit_machine(v)
        else:
            for sub, df in v.tables:
                self.print_table(df, sub)
            print(C.s("  (`plot` draws these on the map, `export search` saves them)", "d"))
        if do_plot:
            self.do_plot("")

    def _attach_search_plot(self, v, I, q):
        """Utility is coloured by side (T red, CT blue, shape = grenade type); kills/deaths stay green/red."""
        def style_of(typ, side):
            if typ in NADE_MARK:
                return SIDE_COL.get(side, "#cccccc"), NADE_MARK[typ], f"{typ} - {side}"
            return COL.get(typ, "#4dabf7"), ("X" if typ in ("fd", "death") else "o"), {
                "fk": "FK - opening kill", "fd": "FD - opening death", "kill": "kill", "death": "death"}.get(typ, typ)

        def plot(out):
            paths = []
            for m, g in I.groupby("map"):
                layers, dens = [], []
                for (typ, side), s_ in g.groupby(["type", g["side"].fillna("?")]):
                    color, marker, label = style_of(typ, side)
                    layers.append(dict(x=s_["x"], y=s_["y"], z=s_["z"], color=color, label=label, marker=marker,
                                       size=40 if marker == "X" else 28))
                    dens.append(((s_["x"], s_["y"], s_["z"]), color))
                arrows = [(r.fx, r.fy, r.fz, r.x, r.y, r.z, style_of(r.type, r.side)[0]) for r in g.head(60).itertuples()
                          if not pd.isna(r.fx)]
                many = len(dens) > 4
                paths.append(map_plot(self.radar, m, f"{m} - search: {q}", out / f"search_{m}_{safe_name(q)[:40].replace(' ', '_')}.png",
                                      layers=layers, density=(g["x"], g["y"], g["z"]) if many else None, arrows=arrows,
                                      densities=() if many else dens,
                                      style=self.cfg["HEATMAP_STYLE"], subtitle=f"{len(g)} results  |  {self.an.scope_label()}"))
            return paths
        v.plots.append(("search", plot))

    def do_role(self, arg):
        """role [set <player> <AWPer|Entry|Support|Lurker|Rifler|IGL|not IGL|CT <text>|auto> [map]] - player roles per map."""
        if not self.need_data():
            return
        parts = shlex.split(arg) if arg else []
        if not parts:
            return self.show_view("roles")
        if parts[0].lower() != "set" or len(parts) < 3:
            bad("Usage: role set <player> <AWPer|Entry|Support|Lurker|Rifler|IGL|not IGL|CT <text>|auto>")
            return
        n = self._pick_player(parts[1])
        if not n:
            return
        rmap = ""
        if len(parts) > 3 or (len(parts) == 4):
            cand = norm_map(parts[-1])
            if cand in set(self.ds.matches["map"]) or parts[-1].lower().startswith(("de_", "cs_")) or parts[-1].lower() in MAP_ALIASES:
                rmap, parts = cand, parts[:-1]
        what = " ".join(parts[2:]).strip()
        path = self.ws.team_root / "roles.json"
        try:
            ov = json.loads(path.read_text())
        except Exception:
            ov = {}
        base = ov.setdefault(n.lower(), {})
        o = base.setdefault("maps", {}).setdefault(rmap, {}) if rmap else base
        wl = what.lower()
        if wl == "auto":
            if rmap:
                base.get("maps", {}).pop(rmap, None)
            else:
                ov.pop(n.lower(), None)
        elif wl == "igl":
            o["igl"] = True
        elif wl in ("not igl", "no igl", "!igl"):
            o["igl"] = False
        elif wl.startswith("ct "):
            o["ct"] = what[3:].strip()
        elif wl in Analyst.ROLE_NAMES:
            o["role"] = Analyst.ROLE_NAMES[wl]
        else:
            bad("Role must be AWPer, Entry, Support, Lurker, Rifler, IGL, not IGL, CT <text> or auto")
            return
        path.write_text(json.dumps(ov, indent=2))
        good(f"{n}: {what}" + (f" on {rmap}" if rmap else " (all maps)"))

    do_roles = do_role

    def do_roster(self, arg):
        """roster [set <player> main|sub|dropped|auto] - who is main roster, sub, or has been dropped."""
        if not self.need_data():
            return
        parts = shlex.split(arg) if arg else []
        if parts and parts[0].lower() == "set":
            if len(parts) < 3:
                bad("Usage: roster set <player> main|sub|dropped|auto")
                return
            who, st = " ".join(parts[1:-1]), parts[-1].lower()
            st = self.ds.STATUS_ALIASES.get(st, st)
            R = self.ds.roster()
            hit = R[R["player"].str.lower() == who.lower()]
            if not len(hit):
                hit = R[R["player"].str.lower().str.contains(who.lower(), regex=False)]
            if len(hit) != 1:
                bad(f"No single player matches '{who}'. Players: {', '.join(R['player'])}")
                return
            if st not in self.ds.STATUSES + ("auto",):
                bad("Status must be main, sub, dropped or auto")
                return
            path = self.ws.team_root / "roster.json"
            try:
                ov = json.loads(path.read_text())
            except Exception:
                ov = {}
            sid = hit["steamid"].iloc[0]
            if st == "auto":
                ov.pop(sid, None)
            else:
                ov[sid] = st
            path.write_text(json.dumps(ov, indent=2))
            self.ds._roster = None
            good(f"{hit['player'].iloc[0]} => {st if st != 'auto' else 'automatic (' + hit['auto'].iloc[0] + ')'}")
            return
        R = self.ds.roster()
        if not len(R):
            warn("No players yet.")
            return
        view = R.drop(columns=["steamid", "auto"]).rename(columns={"share": "% maps", "on_team": "on CSC roster",
                                                                     "recent": "last 5 maps", "last": "last played",
                                                                     "now": "where he is now"})
        self.print_table(view, f"Roster ({len(self.ds.matches)} maps parsed)", rows=50)
        print(C.s("  main = on the CSC roster, plays 50%+ of maps or most recent ones | sub = on the roster, plays less |\n"
                  "  dropped = played for them in these demos but isn't on their CSC roster any more\n"
                  "  Fix one: roster set <player> main|sub|dropped|auto\n"
                  "  Filter: set ROSTER main (main players only) | set ROSTER lineup (only maps the main 5 played) | set ROSTER all", "d"))
        if self.cfg["ROSTER"] != "all":
            info(f"ROSTER filter is on: {self.cfg['ROSTER']}")

    def complete_roster(self, text, line, *a):
        parts = line.split()
        if len(parts) <= 2 and not line.endswith(" "):
            return [o for o in ("set",) if o.startswith(text)]
        if len(parts) >= 3 or (len(parts) == 2 and line.endswith(" ") and parts[1] == "set"):
            if len(parts) >= 3 and line.endswith(" ") or len(parts) >= 4:
                return [o for o in self.ds.STATUSES + ("auto",) if o.startswith(text)]
            return [n for n in self.ds.tgt_names(all_players=True) if n.lower().startswith(text.lower())]
        return []

    def load_vetos(self, refresh=False, quiet=False):
        """Veto records for every league match in the match list (cached in vetos.json)."""
        path = self.ws.team_root / "vetos.json"
        try:
            cache = json.loads(path.read_text())
        except Exception:
            cache = {}
        man = self.ws.manifest()
        ids = list(dict.fromkeys(r["MatchId"] for r in man))
        if not ids:
            if not quiet:
                warn("No league matches listed yet. Run `download` first (no download token needed for this).")
            return []
        day = 86400
        need = [i for i in ids if refresh or i not in cache
                or (not cache[i].get("steps") and time.time() - cache[i].get("fetched", 0) > day)]
        if need:
            try:
                team = json.loads((self.ws.team_root / "team.json").read_text())
            except Exception:
                team = self._resolve_team()
            api = self.csc()
            if not quiet:
                info(f"Fetching vetos for {len(need)} match(es)...")
            for i, mid in enumerate(need, 1):
                try:
                    cache[mid] = dict(csc_veto(api, mid, team.get("name", self.team_name)), fetched=time.time())
                except CscError as e:
                    bad(f"{mid}: {e}")
                if sys.stdout.isatty() and not quiet:
                    print(f"\r    {i}/{len(need)}", end="", flush=True)
            if sys.stdout.isatty() and not quiet:
                print("\r" + " " * 20 + "\r", end="")
            path.write_text(json.dumps(cache, indent=2))
        vetos = [cache[i] for i in ids if i in cache]
        vetos = [dict(v) for v in vetos]
        if self.ds.loaded and len(self.ds.rounds):  # results from the parsed demos
            R_ = self.ds.rounds
            for v in vetos:
                for mp, g in R_[R_["match_id"] == v["match_id"]].groupby("map"):
                    w, l_ = int(g["tgt_won"].sum()), int((~g["tgt_won"]).sum())
                    v["results"][mp] = dict(won=w > l_, score=f"{w}-{l_}")
        se = season_set(self.cfg["SEASON"])
        if se:
            comp = {r["MatchId"]: r.get("Competition", "") for r in man}
            vetos = [v for v in vetos if season_of(v.get("competition") or comp.get(v["match_id"])) in se]
        n = self.cfg.int("LAST_N", 0)
        if n:
            vetos = sorted(vetos, key=lambda v: v["date"])[-n:]
        if self.cfg["OPPONENT"]:
            vetos = [v for v in vetos if self.cfg["OPPONENT"].lower() in v["opponent"].lower()]
        return vetos

    def veto_view(self, refresh=False):
        v = View("vetos", "Map veto")
        vetos = self.load_vetos(refresh=refresh)
        if not vetos:
            v.note("No veto data.")
            return v
        tabs, have_steps, n = veto_tables(vetos)
        v.table("Maps they end up playing (most to least)", tabs.get("played", pd.DataFrame()))
        if "bans" in tabs:
            v.table("Maps THEY ban (most to least)", tabs["bans"])
            v.table("Maps they pick (most to least)", tabs["picks"])
            v.table("Maps opponents ban against them (most to least)", tabs["opp_bans"])
        else:
            v.table("Likely bans: in the pool but rarely/never played (most to least)", tabs.get("likely_bans", pd.DataFrame()))
            v.note("CSC didn't return the ban order for these matches, so bans are inferred from maps they never play.")
        tag = (self.team_name or "team").split()[0][:10]
        hist = []
        for x in sorted(vetos, key=lambda x: x["date"], reverse=True):
            order = " > ".join(f"{'+' if st['action'] == 'pick' else '-'}{st['map'].replace('de_', '')}"
                               f"({tag if st['by'] == 'us' else 'opp'}{'*' if st['random'] else ''})"
                               for st in x["steps"]) or "(ban order not available)"
            res = ", ".join(f"{mp.replace('de_', '')} {'W' if r['won'] else 'L'} {r['score']}" for mp, r in x["results"].items())
            hist.append({"date": x["date"], "opponent": x["opponent"], "bo": x["best_of"], "veto": order, "result": res})
        v.table(f"Veto history (- ban, + pick, ({tag}) = their move, (opp) = opponent's, * = auto-ban on timeout)",
                pd.DataFrame(hist))
        for t in veto_suggestions(tabs, have_steps, n):
            v.note(t)
        v.note(f"{n} league match(es); ban order available for {have_steps}.")
        return v

    # ---------------------------------------------------------------- round query
    def spots(self):
        try:
            return json.loads((self.ws.root / "spots.json").read_text())
        except Exception:
            return {}

    def do_spot(self, arg):
        """spot [<map> <name> = <callout>[,callout]] | [del <map> <name>] - name your own spots, e.g. spot anubis ebox = Walkway"""
        sp = self.spots()
        a = arg.strip()
        if not a:
            if not sp:
                info("No custom spots yet. e.g.  spot anubis ebox = Walkway   (callouts: `callouts anubis`)")
            for m, d in sorted(sp.items()):
                for name, places in sorted(d.items()):
                    print(f"  {m:<12} {C.s(name, 'c'):<20} = {', '.join(places)}")
            return
        if a.lower().startswith("del "):
            parts = a.split()
            if len(parts) == 3 and norm_map(parts[1]) in sp and parts[2].lower() in sp[norm_map(parts[1])]:
                sp[norm_map(parts[1])].pop(parts[2].lower())
                (self.ws.root / "spots.json").write_text(json.dumps(sp, indent=2))
                good("Removed.")
            else:
                bad("Usage: spot del <map> <name>")
            return
        if "=" not in a or len(a.split("=")[0].split()) != 2:
            bad("Usage: spot <map> <name> = <callout>[,callout]   e.g. spot anubis ebox = Walkway")
            return
        left, right = a.split("=", 1)
        m, name = left.split()
        m = norm_map(m)
        places = [x.strip() for x in right.split(",") if x.strip()]
        known = set(self.ds.pos[self.ds.pos["map"] == m]["place"].unique()) if self.ds.loaded else set()
        unknown = [x for x in places if known and x not in known]
        if unknown:
            warn(f"Not a callout seen on {m}: {', '.join(unknown)}. `callouts {m.replace('de_', '')}` lists them.")
        sp.setdefault(m, {})[name.lower()] = places
        (self.ws.root / "spots.json").write_text(json.dumps(sp, indent=2))
        good(f"{m}: '{name}' = {', '.join(places)}")

    def do_rename(self, arg):
        """rename <map> <Valve name> = <your name> | rename export | rename import [file] - your own callout names."""
        a = arg.strip()
        if a.lower().startswith("export"):
            return self._rename_export(a[6:].strip())
        if a.lower().startswith("import"):
            return self._rename_import(a[6:].strip())
        if not a:
            if not self.names.user:
                info("No renames yet. e.g.  rename anubis Walkway = E-Box     (`callouts anubis` lists the names)")
            for m, d in sorted(self.names.user.items()):
                for raw, nice in sorted(d.items()):
                    print(f"  {m:<12} {raw:<20} -> {C.s(nice, 'c')}")
            return
        if "=" not in a or len(a.split("=")[0].split()) < 2:
            bad("Usage: rename <map> <Valve name> = <your name>    e.g. rename anubis Walkway = E-Box")
            return
        left, nice = a.split("=", 1)
        m, raw = left.split(None, 1)
        m, raw, nice = norm_map(m), raw.strip(), nice.strip()
        known = set(self.ds.pos[self.ds.pos["map"] == m]["place"].unique()) if self.ds.loaded and self.ds.pos is not None else set()
        if known and raw not in known:
            hit = [k for k in known if k.lower() == raw.lower() or tidy_place(k).lower() == raw.lower()]
            if len(hit) == 1:
                raw = hit[0]
            else:
                bad(f"'{raw}' isn't an area on {m}. `callouts {m.replace('de_', '')}` lists them.")
                return
        if nice:
            self.names.user.setdefault(m, {})[raw] = nice
            good(f"{m}: {raw} will show as '{nice}'")
        else:
            self.names.user.get(m, {}).pop(raw, None)
            good(f"{m}: {raw} back to '{tidy_place(raw)}'")
        self.names.save()
        if self.ds.loaded:
            self.ds._build_index()

    def _rename_export(self, path=""):
        """Every Valve area name seen in your demos (plus the built-in list), per map, with its current name."""
        out = Path(path.strip('"')) if path else self.ws.root / "callout_names.csv"
        rows = []
        seen = {}
        if self.ds.loaded or self.ds.load(quiet=True):
            for m, g in self.ds.pos.groupby("map"):
                seen[m] = g["place"].value_counts()
        maps = sorted(set(seen) | set(DEFAULT_ALIASES) | set(self.names.user))
        for m in maps:
            places = set(seen.get(m, pd.Series(dtype=int)).index) | set(DEFAULT_ALIASES.get(m, {})) | set(self.names.user.get(m, {}))
            for p_ in sorted(x for x in places if x):
                rows.append({"map": m, "valve_name": p_, "display_name": self.names.pretty(m, p_),
                             "source": self.names.source(m, p_) or "valve",
                             "samples_in_your_demos": int(seen.get(m, {}).get(p_, 0)) if m in seen else 0})
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["map", "valve_name", "display_name", "source", "samples_in_your_demos"])
            w.writeheader()
            w.writerows(rows)
        good(f"Wrote {len(rows)} callouts to {out}")
        print(C.s("    Edit the display_name column (Excel is fine), then: rename import", "d"))

    def _rename_import(self, path=""):
        src = Path(path.strip('"')) if path else self.ws.root / "callout_names.csv"
        if not src.exists():
            bad(f"{src} not found. `rename export` creates it.")
            return
        changed = 0
        with open(src, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                m, raw, nice = norm_map(r.get("map", "")), (r.get("valve_name") or "").strip(), (r.get("display_name") or "").strip()
                if not raw:
                    continue
                default = DEFAULT_ALIASES.get(m, {}).get(raw) or tidy_place(raw)
                cur = self.names.user.get(m, {}).get(raw)
                if nice and nice != default and nice != cur:
                    self.names.user.setdefault(m, {})[raw] = nice
                    changed += 1
                elif (not nice or nice == default) and cur is not None:
                    self.names.user[m].pop(raw)
                    changed += 1
        self.names.save()
        if self.ds.loaded:
            self.ds._build_index()
        good(f"Imported {changed} change(s) from {src}")

    def do_callouts(self, arg):
        """callouts [map] | callouts learn <map> | callouts name <map> <#> <name> | callouts add <map> <name> setpos x y z
        | callouts fine [map] | callouts del <map> <name> | callouts clear <map> - area names; `learn` sets up specific
        callouts (Firebox, Triple, ...) so positions show as those instead of Valve's broad areas."""
        sub = arg.strip().split(None, 1)
        if sub and sub[0].lower() in ("learn", "name", "add", "fine", "del", "clear", "points"):
            return self.fine_callouts(sub[0].lower(), sub[1] if len(sub) > 1 else "")
        if not self.need_data():
            return
        m = norm_map(arg.strip()) if arg.strip() else (self.cfg["MAP"] or None)
        maps = [m] if m else sorted(self.ds.matches["map"].unique())
        for mp in maps:
            P = self.ds.pos[self.ds.pos["map"] == mp]
            N = self.ds.nades[(self.ds.nades["map"] == mp) & self.ds.nades["tgt"]] if len(self.ds.nades) else pd.DataFrame()
            if not len(P):
                warn(f"No data on {mp}.")
                continue
            sm = N[N["kind"] == "smoke"]["l_place"].value_counts() if len(N) else pd.Series(dtype=int)
            places = sorted(p for p in P["place"].unique() if p)
            df = pd.DataFrame({"Valve name": places,
                               "shown as": [DISPLAY.pretty(mp, p) for p in places],
                               "from": [{"yours": "your rename", "default": "built-in alias", "auto": "tidied"}.get(
                                   DISPLAY.source(mp, p), "") for p in places],
                               "their smokes": [int(sm.get(p, 0)) for p in places]})
            self.print_table(df.sort_values("their smokes", ascending=False), f"{mp} callouts", rows=200)
            print(C.s(f"    rename {mp.replace('de_', '')} <Valve name> = <your name>    to change one", "d"))

    # ---- specific callouts (your named spots) -----------------------------------------------
    def _fine_save(self, pts):
        (self.ws.root / FINE_CALLOUT_FILE).write_text(json.dumps(pts, indent=2), encoding="utf-8")
        if self.ds.loaded:
            self.ds.load(quiet=True)  # re-label every position with the new names

    def fine_callouts(self, sub, rest):
        pts = self.ds.fine_points()
        parts = shlex.split(rest) if rest else []
        if sub in ("fine", "points"):
            maps = [norm_map(parts[0])] if parts else sorted(pts)
            if not any(pts.get(m) for m in maps):
                info("No specific callouts yet. `callouts learn mirage` to set them up.")
                return
            for m in maps:
                rows = [{"callout": p_["name"], "x": round(p_["x"]), "y": round(p_["y"]), "z": round(p_["z"]),
                         "size": round(p_.get("r", 250))} for p_ in pts.get(m, [])]
                self.print_table(pd.DataFrame(rows).sort_values("callout"), f"{m} - your callouts ({len(rows)})", rows=200)
            return
        if sub == "learn":
            if not self.need_data():
                return
            if not parts:
                bad("callouts learn <map>   e.g. callouts learn mirage")
                return
            return self.learn_callouts(norm_map(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 32)
        if sub == "name":
            if len(parts) < 3 or not all(x.isdigit() for x in parts[1].split(",")):
                bad("callouts name <map> <#>[,#...] <name>   (numbers from `callouts learn <map>`)")
                return
            m = norm_map(parts[0])
            return self._name_clusters(m, [int(x) for x in parts[1].split(",")], " ".join(parts[2:]))
        if sub == "add":
            txt = " ".join(parts[1:]) if parts else ""
            mm = re.search(r"^(.*?)\s*setpos\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)(?:.*?\br\s*=\s*(\d+))?", txt, re.I)
            if not parts or not mm or not mm.group(1).strip():
                bad("callouts add <map> <name> setpos <x> <y> <z> [r=250]   (paste what `getpos` prints in the CS2 console)")
                return
            m = norm_map(parts[0])
            name = mm.group(1).strip()
            x, y, z = (float(mm.group(i)) for i in (2, 3, 4))
            z -= 64  # getpos gives eye height; positions in demos are at the feet
            pts.setdefault(m, []).append({"name": name, "x": x, "y": y, "z": z, "r": float(mm.group(5) or 250)})
            self._fine_save(pts)
            good(f"{m}: {name} added")
            return
        if sub == "del":
            if len(parts) < 2:
                bad("callouts del <map> <name>")
                return
            m, name = norm_map(parts[0]), " ".join(parts[1:]).lower()
            before = len(pts.get(m, []))
            pts[m] = [p_ for p_ in pts.get(m, []) if p_["name"].lower() != name]
            self._fine_save(pts)
            good(f"Removed {before - len(pts[m])} spot(s) called '{name}' on {m}.")
            return
        if sub == "clear":
            if not parts:
                bad("callouts clear <map>")
                return
            m = norm_map(parts[0])
            pts.pop(m, None)
            self._fine_save(pts)
            good(f"Cleared your callouts on {m} - back to Valve's area names there.")
            return

    def learn_callouts(self, m, k=32):
        """Split everywhere players stand on a map into numbered spots, draw them on the radar, and let you name them."""
        P = self.ds.pos[self.ds.pos["map"] == m]
        if not len(P):
            bad(f"No parsed data on {m}.")
            return
        P = P[P["alive"] & P["t"].between(5, 100)]
        area_col = "valve_place" if "valve_place" in P else "place"
        P = P[~P[area_col].astype(str).str.lower().str.contains("spawn")]
        if len(P) > 25000:
            P = P.sample(25000, random_state=1)
        X = np.c_[P["x"].to_numpy(float), P["y"].to_numpy(float), P["z"].to_numpy(float) * 1.5]
        k = max(8, min(k, len(X) // 150 or 8))
        info(f"Finding {k} spots on {m} from {len(X)} positions...")
        cen, lab = kmeans_np(X, k)
        rows = []
        for j in range(k):
            sel = lab == j
            if sel.sum() < 20:
                continue
            d = np.sqrt(((X[sel] - cen[j]) ** 2).sum(1))
            area = P[area_col].to_numpy()[sel]
            area = pd.Series(area).mode().iloc[0] if len(area) else ""
            rows.append({"x": float(cen[j][0]), "y": float(cen[j][1]), "z": float(cen[j][2] / 1.5),
                         "r": float(np.clip(np.percentile(d, 75), 120, 420)), "area": area,
                         "share": round(100 * sel.sum() / len(X), 1)})
        rows.sort(key=lambda r: (str(r["area"]), -r["share"]))
        for i, r in enumerate(rows, 1):
            r["n"] = i
        (self.ws.root / f"callout_spots_{m}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        out = self.ws.out / "callouts"
        out.mkdir(parents=True, exist_ok=True)
        img = map_plot(self.radar, m, f"{m} - numbered spots: give each one a callout name",
                       out / f"spots_{m}.png",
                       layers=[dict(x=P["x"], y=P["y"], z=P["z"], color="#888888", size=2, alpha=0.08, label="where players stand",
                                    count=False),
                               dict(x=[r["x"] for r in rows], y=[r["y"] for r in rows], z=[r["z"] for r in rows],
                                    color="#ffd43b", size=60, label="spots", count=False)],
                       labels=[(r["x"], r["y"], r["z"], str(r["n"])) for r in rows], style="points",
                       subtitle="type:  <number> <name>   e.g.  3 Firebox   (3,4 Firebox = two spots, one name)")
        good(f"Spot map: {img}")
        open_path(img, self.cfg)
        self.print_table(pd.DataFrame([{"#": r["n"], "inside Valve area": show_place(m, r["area"]), "% of time here": r["share"]}
                                       for r in rows]), f"{m} - {len(rows)} spots", rows=200)
        used = {p_["name"].lower() for p_ in self.ds.fine_points().get(m, [])}
        sug = [x for x in FINE_CALLOUT_SUGGESTIONS.get(m, []) if x.lower() not in used]
        if sug:
            print(C.s("  Common names for this map: ", "bold") + ", ".join(sug))
        if not self.interactive:
            print(C.s(f"  Name them with:  callouts name {m.replace('de_', '')} <#> <name>", "d"))
            return
        print(C.s("  Type  <#> <name>  for each spot you want named (e.g.  3 Firebox  or  3,4 Firebox). "
                  "Skip spots you don't care about. Blank line = done.", "d"))
        while True:
            a = self.ask("Spot", "")
            if not a:
                break
            mm = re.match(r"^\s*([\d,\s]+?)\s*[=:]?\s+(.+)$", a)
            if not mm:
                warn("Format:  3 Firebox")
                continue
            nums = [int(x) for x in re.split(r"[,\s]+", mm.group(1)) if x.isdigit()]
            self._name_clusters(m, nums, mm.group(2).strip(), reload=False)
        self._fine_save(self.ds.fine_points())
        good(f"Done - {m} positions now show your callout names. `callouts fine {m.replace('de_', '')}` lists them.")

    def _name_clusters(self, m, nums, name, reload=True):
        try:
            spots = {r["n"]: r for r in json.loads((self.ws.root / f"callout_spots_{m}.json").read_text(encoding="utf-8"))}
        except Exception:
            bad(f"Run `callouts learn {m.replace('de_', '')}` first - it makes the numbered spots.")
            return
        pts = self.ds.fine_points()
        lst = pts.setdefault(m, [])
        missing = [n for n in nums if n not in spots]
        if missing:
            bad(f"No spot {', '.join(map(str, missing))} on {m}.")
            return
        for n in nums:
            r = spots[n]
            lst[:] = [p_ for p_ in lst if not (abs(p_["x"] - r["x"]) < 1 and abs(p_["y"] - r["y"]) < 1)]  # re-naming a spot
            lst.append({"name": name, "x": r["x"], "y": r["y"], "z": r["z"], "r": r["r"]})
        (self.ws.root / FINE_CALLOUT_FILE).write_text(json.dumps(pts, indent=2), encoding="utf-8")
        good(f"{m}: spot {', '.join(map(str, nums))} = {name}")
        if reload and self.ds.loaded:
            self.ds.load(quiet=True)

    def _place_match(self, m_series, place_series, want):
        """Does each place match `want`? Uses your `spot` names first, then a loose name match (e-box = ebox = E Box)."""
        key = re.sub(r"[^a-z0-9]", "", want.lower())
        sp = self.spots()
        maps = np.asarray(m_series)
        places = np.asarray(place_series).astype(str)
        out = np.zeros(len(places), dtype=bool)
        for m in set(maps):
            sel = maps == m
            alias = {x.lower() for x in sp.get(m, {}).get(want.lower(), [])}
            norm = np.array([re.sub(r"[^a-z0-9]", "", p.lower()) for p in places[sel]])
            if alias:
                out[sel] = np.isin(np.char.lower(places[sel].astype(str)), list(alias))
            else:
                out[sel] = np.array([self.names.matches(m, p_, want) for p_ in places[sel]], dtype=bool) \
                    if len(norm) else np.array([], bool)
        return out

    def do_rounds(self, arg):
        """rounds <conditions> - which rounds match, and what happened. e.g. rounds anubis t smoke:ebox then:B"""
        if not self.need_data():
            return
        q = arg.strip() or self.ask("Conditions (e.g. anubis t smoke:ebox then:B)")
        if not q:
            return
        R = self.ds.f("rounds", side=False, phase=False)
        side, then, conds, flags = self.cfg["SIDE"] or None, None, [], []
        before = after = None
        nade_kinds = {"smoke": "smoke", "smokes": "smoke", "flash": "flash", "molly": "molotov", "molotov": "molotov",
                      "inc": "molotov", "he": "he", "nade": "he"}
        for tok in shlex.split(q):
            t = tok.lower()
            neg = t.startswith("!") or t.startswith("no-")
            t = t.lstrip("!").removeprefix("no-")
            k, _, v = t.partition(":")
            if not v:
                if t in ("t", "ct"):
                    side = t.upper()
                elif t in ("won", "win", "wins"):
                    flags.append(("won", True))
                elif t in ("lost", "loss", "lose"):
                    flags.append(("won", False))
                elif t in ("planted", "plant"):
                    flags.append(("planted", not neg))
                elif t in ("pistol", "eco", "force", "full", "half-eco"):
                    flags.append(("buy", t))
                elif t.removeprefix("de_") in MAP_ALIASES or t.startswith("de_") or norm_map(t) in set(R["map"]):
                    R = R[R["map"] == norm_map(t)]
                else:
                    bad(f"Don't understand '{tok}'. Try: map, t/ct, smoke:<spot>, flash:, molly:, he:, from:<spot>, "
                        f"fk:<spot>, fd:<spot>, player:<name>, before:<s>, after:<s>, then:A|B|mid, won/lost, planted, buy")
                    return
                continue
            if k in nade_kinds:
                conds.append(dict(kind=nade_kinds[k], place=v, neg=neg, src="land"))
            elif k == "from":
                if conds and conds[-1]["kind"] in nade_kinds.values():
                    conds[-1]["from"] = v
                else:
                    conds.append(dict(kind="any", place=v, neg=neg, src="throw"))
            elif k in ("fk", "fd"):
                conds.append(dict(kind=k, place=v, neg=neg))
            elif k == "player":
                conds.append(dict(kind="player", place=v, neg=neg))
            elif k == "before":
                before = float(v)
            elif k == "after":
                after = float(v)
            elif k in ("then", "went", "site"):
                then = {"a": "A", "b": "B", "mid": "Mid", "m": "Mid"}.get(v, v.upper())
            elif k == "buy":
                flags.append(("buy", v))
            elif k == "map":
                R = R[R["map"] == norm_map(v)]
            elif k == "side":
                side = v.upper()
            else:
                bad(f"Unknown condition {k}:")
                return
        if side:
            R = R[R["tgt_side"] == side]
        for f, val in flags:
            if f == "won":
                R = R[R["tgt_won"] == val]
            elif f == "planted":
                R = R[R["planted"] == val]
            elif f == "buy":
                R = R[R["buy"] == val]
        base = R
        if not len(base):
            warn("No rounds match the map/side filters.")
            return
        N = self.ds.f("nades", side=False, phase=False, player=False)
        N = N[N["tgt"] & N["key"].isin(base["key"])] if len(N) else N
        K = self.ds.f("kills", side=False, phase=False, player=False)
        K = K[K["opening"] & K["key"].isin(base["key"])] if len(K) else K
        if before is not None:
            N, K = N[N["t"] <= before], K[K["t"] <= before]
        if after is not None:
            N, K = N[N["t"] >= after], K[K["t"] >= after]
        player = next((c["place"] for c in conds if c["kind"] == "player" and not c["neg"]), None)
        if player:
            N = N[N["name"].str.lower() == player.lower()]
        keys = set(base["key"])
        hits_nades = []
        for c in conds:
            if c["kind"] == "player":
                continue
            if c["kind"] in ("fk", "fd"):
                k = K[K["tgt_att"]] if c["kind"] == "fk" else K[K["tgt_vic"]]
                col = "a_place" if c["kind"] == "fk" else "v_place"
                ok = set(k[self._place_match(k["map"], k[col], c["place"])]["key"]) if len(k) else set()
            else:
                n = N if c["kind"] == "any" else N[N["kind"] == c["kind"]]
                col = "t_place" if c["src"] == "throw" else "l_place"
                m = self._place_match(n["map"], n[col], c["place"]) if len(n) else np.array([], bool)
                if c.get("from") and len(n):
                    m &= self._place_match(n["map"], n["t_place"], c["from"])
                hit = n[m] if len(n) else n
                ok = set(hit["key"])
                if not c["neg"]:
                    hits_nades.append(hit)
                if not len(hit) and not c["neg"]:
                    self._suggest_places(base, n, col, c)
            keys = keys - ok if c["neg"] else keys & ok
        M = base[base["key"].isin(keys)]
        desc = q
        print(C.s(f"\n=== Rounds: {desc} ===", "bold", "c") + C.s(f"   ({self.an.scope_label()})", "d"))
        if not len(M):
            warn(f"0 of {len(base)} rounds match.")
            return
        def dist(df):
            vc = df["t_target"].value_counts()
            return ", ".join(f"{k} {100 * v / len(df):.0f}%" for k, v in vc.items())
        print(f"  {C.s(str(len(M)), 'bold')} of {len(base)} rounds match ({100 * len(M) / len(base):.0f}%).")
        if then:
            hit = M[M["t_target"] == then]
            p_hit = 100 * len(hit) / len(M)
            p_base = 100 * (base["t_target"] == then).mean()
            print(C.s(f"  -> went {then} in {len(hit)}/{len(M)} of those rounds ({p_hit:.0f}%)", "g", "bold")
                  + f"  vs {p_base:.0f}% of all rounds in scope"
                  + (C.s(f"  ({p_hit - p_base:+.0f} pts)", "y") if abs(p_hit - p_base) >= 10 else ""))
            if len(hit):
                print(f"     won {hit['tgt_won'].mean():.0%} of them"
                      + (f", median plant {hit['plant_t'].median():.0f}s" if hit["planted"].any() else ""))
        print(f"  Where they went: {dist(M)}   (all rounds: {dist(base)})")
        print(f"  Won {M['tgt_won'].mean():.0%} of matching rounds (all rounds: {base['tgt_won'].mean():.0%})")
        H = pd.concat(hits_nades) if hits_nades else pd.DataFrame()
        if len(H):
            ft = H.groupby("key")["t"].min()
            print(f"  Utility lands at ~{ft.median():.0f}s (median); thrown by {', '.join(H['name'].value_counts().index[:3])}"
                  f"; from {', '.join(show_place(H['map'].iloc[0], p_) for p_ in H['t_place'].value_counts().index[:2])}")
        rows = []
        for r in M.sort_values(["date", "round"], ascending=[False, True]).itertuples():
            ut = H[H["key"] == r.key]["t"].min() if len(H) else np.nan
            rows.append({"date": r.date, "opponent": r.opponent, "map": r.map, "round": r.round, "side": r.tgt_side,
                         "utility t": None if pd.isna(ut) else round(ut), "went": r.t_target,
                         "plant t": None if pd.isna(r.plant_t) else round(r.plant_t), "buy": r.buy,
                         "result": "W" if r.tgt_won else "L"})
        self.print_table(pd.DataFrame(rows), "Matching rounds", rows=max(self.cfg.int("ROWS", 15), 20))
        v = View("rounds", f"Rounds: {desc}").table("Matching rounds", pd.DataFrame(rows))
        Mk = set(M["key"])

        def plot(out):
            paths = []
            for m in sorted(M["map"].unique()):
                h = H[(H["map"] == m) & H["key"].isin(Mk)] if len(H) else H
                pl = M[(M["map"] == m) & M["planted"]]
                layers = []
                if len(h):
                    for (kind, side), hs in h.groupby(["kind", h["tgt_side"].fillna("?")]):
                        layers.append(dict(x=hs["lx"], y=hs["ly"], z=hs["lz"], color=SIDE_COL.get(side, "#ccc"),
                                           marker=NADE_MARK.get(kind, "o"), label=f"{kind} - {side}"))
                if len(pl):
                    layers.append(dict(x=pl["plant_x"], y=pl["plant_y"], z=pl["plant_z"], color="#ffd43b",
                                       label="bomb plant after", marker="*", size=110))
                arrows = [(a.tx, a.ty, a.tz, a.lx, a.ly, a.lz, SIDE_COL.get(a.tgt_side, "#fff")) for a in h.head(40).itertuples()
                          if not pd.isna(a.tx)] if len(h) else []
                paths.append(map_plot(self.radar, m, f"{m} - {desc}", out / f"rounds_{m}_{safe_name(desc)[:40].replace(' ', '_')}.png",
                                      layers=layers, arrows=arrows, style="points",
                                      subtitle=f"{len(M[M['map'] == m])} rounds  |  {self.an.scope_label()}"))
            return paths
        v.plots.append(("rounds", plot))
        self.last = v
        print(C.s("  (`plot` shows the utility and where they planted afterwards; `export` saves the list)", "d"))

    def _suggest_places(self, base, n, col, c):
        """No callout matched: show what their utility of that kind actually lands on, so you can map a name."""
        maps = sorted(base["map"].unique())
        if not len(n):
            warn(f"No {c['kind']} thrown by them in these rounds.")
            return
        vc = n[col].value_counts().head(12)
        warn(f"No callout matches '{c['place']}' on {', '.join(maps)}. Their {c['kind']}s land on:")
        print("    " + ", ".join(f"{show_place(maps[0] if len(maps) == 1 else None, p)} ({k})" for p, k in vc.items()))
        if len(maps) == 1:
            print(C.s(f"    Name one yourself:  spot {maps[0].replace('de_', '')} {c['place']} = <callout from the list>", "d"))

    def do_vetos(self, arg):
        """vetos [refresh] - their map vetos: what they play, ban and pick, most to least."""
        v = self.veto_view(refresh="refresh" in arg)
        self.last = v
        if self.cfg["OUTPUT"] in ("json", "csv"):
            return self.emit_machine(v)
        print(C.s(f"\n=== {v.title} ===", "bold", "c") + C.s(f"   ({self.team_name})", "d"))
        for sub, df in v.tables:
            self.print_table(df, sub, rows=max(self.cfg.int("ROWS", 15), 12))
        for n_ in v.notes:
            print(C.s("  * ", "y") + show_text(n_))

    do_veto = do_vetos

    def do_player(self, arg):
        """player <name> [report [pdf|html|both]] - one player's profile; add `report` for his full report."""
        if not self.need_data():
            return
        m_ = re.match(r"^(.*?)\s+report(?:\s+(pdf|jpg|html|both))?\s*$", arg.strip(), re.I)
        if m_:
            return self.player_report(m_.group(1).strip(), (m_.group(2) or "").lower())
        names = self.ds.tgt_names(all_players=True)
        if not arg.strip():
            for i, n in enumerate(names, 1):
                print(f"  [{i}] {self.ds.label(n)}")
            arg = self.ask("Player number or name")
        a = arg.strip()
        n = names[int(a) - 1] if a.isdigit() and 0 < int(a) <= len(names) else next((x for x in names if a.lower() in x.lower()), None)
        if not n:
            bad("Unknown player.")
            return
        old = self.cfg["PLAYER"]
        self.cfg.v["PLAYER"] = n
        try:
            st = self.ds.status_of(n)
            print(C.s(f"\n=== {n} ===", "bold", "c") + (C.s(f"  [{st}]", "y") if st else ""))
            P = self.an.v_players().tables
            if not len(P[0][1]) or "player" not in P[0][1]:
                warn(f"No data for {n} with the current filters ({self.an.scope_label()}). `reset` clears them.")
                return
            self.print_table(P[0][1][P[0][1]["player"] == n], "Stats")
            if len(P) > 1 and len(P[1][1]) and "player" in P[1][1]:
                self.print_table(P[1][1][P[1][1]["player"] == n], "Where he plays")
            self.cfg.v["PLAYER"] = ""
            rl = self.an.roles_by_map() if not self.cfg["MAP"] else self.an.player_roles().assign(map=self.cfg["MAP"])
            self.cfg.v["PLAYER"] = n
            for _, rr in (rl[rl["player"] == n].iterrows() if len(rl) else []):
                print(C.s(f"  Role on {rr['map']}: ", "bold") + f"{rr['role']}" + (f" | CT: {rr['CT role']}" if rr["CT role"] else "")
                      + (f" | IGL: {rr['IGL']}" if rr["IGL"] else "") + C.s(f"  ({rr['why']})", "dim"))
            pw = self.an.power_positions()
            if len(pw):
                self.print_table(pw[["map", "half", "power position", "% of rounds", "K-D there", "next most", "rounds"]],
                                 "Power positions", rows=20)
            o = self.an.v_openings()
            for sub, df in o.tables[1:]:
                self.print_table(df, sub, rows=6)
            N = self.ds.f("nades")
            N = N[N["tgt"]]
            if len(N):
                self.print_table(N.groupby(["map", "tgt_side", "kind", "l_place"]).size().rename("n").reset_index()
                                 .sort_values("n", ascending=False), "Utility (lands at)", rows=12)
            self.last = o
        finally:
            self.cfg.v["PLAYER"] = old
        print(C.s("  (`set PLAYER " + n + "` then any view or heatmap to focus on him)", "d"))

    def complete_player(self, text, *a):
        return [n for n in (self.ds.tgt_names() if self.ds.loaded else []) if n.lower().startswith(text.lower())]

    # ---------------------------------------------------------------- analyst
    def do_anomalies(self, arg):
        """anomalies - things that stand out (player outliers, odd rounds, extreme results, recent changes)."""
        self.show_view("anomalies")

    def do_counter(self, arg):
        """counter - counter-play suggestions with the evidence behind each one."""
        self.show_view("counter")

    # ---- pro reference library + signature plays -------------------------------------
    PRO_SUBS = ("status", "dir", "get", "import", "parse", "list", "rounds", "remove", "link", "unlink", "links", "plan")

    def pro_lib(self):
        root = Path(self.cfg["PRO_DIR"]).expanduser() if self.cfg["PRO_DIR"] else self.ws.root / "pro"
        if getattr(self, "_pro", None) is None or self._pro.root != root:
            self._pro = ProLibrary(root)
            self._pro_ds = None
        return self._pro

    def pro_data(self, quiet=True):
        lib = self.pro_lib()
        if getattr(self, "_pro_ds", None) is None or not self._pro_ds.loaded:
            ds = Dataset(lib, self.cfg)
            if not ds.load(quiet=True):
                if not quiet:
                    warn("No parsed pro demos yet: `pro get` / `pro import`, then `pro parse`.")
                return None
            self._pro_ds = ds
        return self._pro_ds

    def pro_pool(self):
        pool = [m for m in self.cfg["PRO_POOL"].split(",") if m]
        if not pool and self.ds.loaded:
            pool = sorted(self.ds.matches["map"].unique())
        return pool

    def _pro_demo(self, ref):
        files = self.pro_lib().demo_files()
        ref = str(ref).strip()
        if ref.isdigit() and 0 < int(ref) <= len(files):
            return files[int(ref) - 1]
        hit = [f for f in files if ref.lower() in f.name.lower()]
        if len(hit) == 1:
            return hit[0]
        bad(f"No single pro demo matches '{ref}'. `pro list` shows the numbers.")
        return None

    def do_pro(self, arg):
        """pro [status|dir <folder>|plan|get [hltv links]|import [folder|file] [force]|parse [force]|list|rounds <#>|
        remove <#>|link <#> <round> <metastack url> [name]|unlink <#> <round>|links [import <csv>|export]]
        - your library of pro demos (from HLTV) that `signature` compares the scouted team's plays against."""
        parts = shlex.split(arg) if arg.strip() else []
        sub = parts[0].lower() if parts else "status"
        rest = parts[1:]
        lib = self.pro_lib()
        target = self.cfg.int("PRO_TARGET", 25)
        if sub in ("status", "plan"):
            pool = self.pro_pool()
            counts = lib.per_map()
            cap = math.ceil(target / max(1, len(pool))) if pool else target
            print(C.s("\n  Pro library", "bold") + f"  {lib.root}")
            if not pool:
                warn("No map pool yet: `set PRO_POOL mirage,inferno,...` (or parse your scouted team first and it uses their maps).")
            rows = [{"map": m, "have": counts.get(m, 0), "target": cap, "still need": max(0, cap - counts.get(m, 0))}
                    for m in sorted(set(pool) | set(counts))]
            if rows:
                self.print_table(pd.DataFrame(rows), f"{sum(counts.values())}/{target} demos, spread over the map pool")
            pds = self.pro_data()
            links = lib.links()
            print(f"  parsed: {0 if pds is None else pds.matches['stem'].nunique()}  |  MetaStack links saved: {len(links)}")
            need = [r for r in rows if r["still need"] and r["map"] in pool]
            if need:
                print(C.s("\n  Next: pick matches on HLTV for: ", "bold")
                      + ", ".join(f"{r['map'].replace('de_', '')} x{r['still need']}" for r in need))
                print(C.s("    Prefer matches whose rounds are on MetaStack, so `signature` can link to them.\n"
                          "    `pro get <hltv match link> ...` opens them; click GOTV Demo and the download is filed automatically.", "d"))
            return
        if sub == "dir":
            if not rest:
                print(f"PRO_DIR => {lib.root}")
                return
            new = Path(" ".join(rest)).expanduser()
            old = lib.root
            try:
                new.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                bad(f"Can't use {new}: {e}")
                return
            if old.exists() and old != new and any(old.rglob("*.dem")) and not any(new.rglob("*.dem")):
                if not self.interactive or self.ask(f"Move the existing library from {old} to {new}? (Y/n)", "y").lower().startswith("y"):
                    for item in old.iterdir():
                        shutil.move(str(item), new / item.name)
                    good(f"Moved the library to {new}")
            self.cfg.set("PRO_DIR", str(new))
            self.cfg.save()
            self._pro = None
            good(f"Pro demos will be stored in {new}")
            return
        if sub == "get":
            import webbrowser
            urls = [u for u in rest if u.startswith("http")]
            if not urls:
                urls = ["https://www.hltv.org/results"]
                info("Opening HLTV results - filter by map/event, open a match, and use `pro get <match link>` "
                     "(or just click GOTV Demo while `pro import watch` runs).")
            for u in urls:
                if "hltv.org" not in u:
                    warn(f"{u} isn't an HLTV link - opening it anyway.")
                webbrowser.open_new_tab(u)
                print(f"  {C.s('>', 'c')} {u}")
            if not self.interactive:
                info("Download the GOTV demo(s), then run `pro import`.")
                return
            info("Click 'GOTV Demo' on each match page. Each download is unpacked and filed when it finishes.")
            return self._pro_watch(len([u for u in urls if "/matches/" in u or "/download/" in u]) or 0)
        if sub == "import":
            force = "force" in [x.lower() for x in rest]
            rest = [x for x in rest if x.lower() != "force"]
            if rest and rest[0].lower() == "watch":
                return self._pro_watch(0, force=force)
            src = Path(" ".join(rest) or self.cfg["SORT_FROM"]).expanduser()
            if not src.exists():
                bad(f"Not found: {src}")
                return
            if src.is_file():
                files = [src]
            else:
                files = [f for f in src.rglob("*") if f.is_file() and self._is_pro_file(f)]
            if not files:
                warn(f"No HLTV demos/archives in {src} (looking for .rar/.zip/.7z, or .dem named like team-vs-team).")
                return
            self._pro_add(files, force)
            return
        if sub == "parse":
            if missing_modules(["demoparser2"]) and not pip_install(["demoparser2"], self.assume_yes, "demo parsing"):
                return
            done, failed = lib.parse(self.cfg.float("SAMPLE_SECONDS", 2), force="force" in rest, log=info)
            self._pro_ds = None
            if not done and not failed:
                info("All pro demos are already parsed.")
            pds = self.pro_data(quiet=False)
            if pds is not None:
                good(f"Pro library: {pds.matches['stem'].nunique()} demos, "
                     f"{int((pds.rounds['tgt_side'] == 'T').sum())} T rounds to compare against. Next: `signature`")
            return
        if sub == "list":
            files = lib.demo_files()
            if not files:
                warn("No pro demos yet: `pro get` or `pro import <folder>`.")
                return
            pds = self.pro_data()
            links = lib.links()
            rows = []
            for i, f in enumerate(files, 1):
                stem = demo_stem(f)
                teams = ""
                if pds is not None:
                    mm = pds.matches[pds.matches["stem"] == stem]
                    teams = " vs ".join(mm["team"].tolist()) if len(mm) and "team" in mm else ""
                rows.append({"#": i, "map": f.parent.name, "file": f.name, "teams": teams or "(not parsed)",
                             "MetaStack links": sum(1 for k in links if k.startswith(stem + "#"))})
            self.print_table(pd.DataFrame(rows), "Pro demos", rows=200)
            return
        if sub == "rounds":
            if not rest:
                bad("pro rounds <#>")
                return
            d = self._pro_demo(rest[0])
            pds = self.pro_data(quiet=False)
            if d is None or pds is None:
                return
            stem = demo_stem(d)
            pr = [p for p in t_round_prints(pds, buys=("full", "force", "half-eco", "eco", "pistol")) if p["stem"] == stem]
            links = lib.links()
            rows = [{"round": p["round"], "T side": p["team"], "site": p["site"], "hit (s)": round(p["hit_t"]) if p["hit_t"] == p["hit_t"] else "",
                     "won": "yes" if p["won"] else "", "utility": ", ".join(sorted(t.replace("@", " ") for t in p["util"]))[:70],
                     "MetaStack": links.get(lib.link_key(stem, p["round"]), {}).get("name") or ("link" if lib.link_key(stem, p["round"]) in links else "")}
                    for p in sorted(pr, key=lambda p: p["round"])]
            self.print_table(show_names(pd.DataFrame(rows)), d.name, rows=40)
            return
        if sub == "remove":
            if not rest:
                bad("pro remove <#>")
                return
            d = self._pro_demo(rest[0])
            if d is None:
                return
            if self.interactive and not self.ask(f"Delete {d.name}? (y/N)", "n").lower().startswith("y"):
                return
            d.unlink()
            lib.save_library([e for e in lib.library() if Path(e["file"]).name != d.name])
            for c in lib.cache.glob(demo_stem(d) + "~*.pkl"):
                c.unlink()
            self._pro_ds = None
            good(f"Removed {d.name}")
            return
        if sub in ("link", "unlink"):
            need = 3 if sub == "link" else 2
            if len(rest) < need:
                bad("pro link <#> <round> <metastack url> [name]   |   pro unlink <#> <round>")
                return
            d = self._pro_demo(rest[0])
            if d is None or not rest[1].isdigit():
                bad("Round must be a number (see `pro rounds <#>`).")
                return
            links = lib.links()
            key = lib.link_key(demo_stem(d), rest[1])
            if sub == "unlink":
                links.pop(key, None)
                lib.save_links(links)
                good(f"Removed the link for {d.name} round {rest[1]}")
                return
            url = rest[2]
            if not url.startswith("http"):
                bad("The link must start with http(s)://")
                return
            links[key] = {"url": url, "name": " ".join(rest[3:]), "added": datetime.now().strftime("%Y-%m-%d")}
            lib.save_links(links)
            good(f"{d.name} round {rest[1]} -> {url}")
            return
        if sub == "links":
            links = lib.links()
            if rest and rest[0].lower() == "export":
                out = lib.root / "metastack_links.csv"
                with open(out, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["demo", "round", "url", "name"])
                    for k, v in links.items():
                        dm, rn = k.rsplit("#", 1)
                        w.writerow([dm, rn, v["url"], v.get("name", "")])
                good(f"Saved {out}")
                return
            if rest and rest[0].lower() == "import":
                src = Path(" ".join(rest[1:])).expanduser()
                try:
                    with open(src, newline="", encoding="utf-8-sig") as f:
                        n = 0
                        for r in csv.DictReader(f):
                            d = self._pro_demo(r.get("demo", "")) if r.get("demo") else None
                            if d and str(r.get("round", "")).isdigit() and str(r.get("url", "")).startswith("http"):
                                links[lib.link_key(demo_stem(d), r["round"])] = {"url": r["url"], "name": r.get("name", ""),
                                                                                  "added": datetime.now().strftime("%Y-%m-%d")}
                                n += 1
                    lib.save_links(links)
                    good(f"Imported {n} link(s).")
                except Exception as e:
                    bad(f"Couldn't read {src}: {e}  (columns: demo,round,url,name)")
                return
            if not links:
                info("No MetaStack links saved yet. `pro link <#> <round> <url> [name]` (numbers from `pro list`).")
                return
            self.print_table(pd.DataFrame([{"demo": k.rsplit("#", 1)[0], "round": k.rsplit("#", 1)[1], "name": v.get("name", ""),
                                            "url": v["url"]} for k, v in links.items()]), "MetaStack links", rows=200)
            return
        bad("pro " + " | ".join(self.PRO_SUBS))

    def _is_pro_file(self, f: Path):
        n = f.name.lower()
        if n.endswith((".crdownload", ".part", ".tmp")):
            return False
        if PRO_ARCHIVE_RE.search(n):
            return True
        return n.endswith(".dem") and ("-vs-" in n or bool(map_from_name(n))) and not re.match(r"^1-[0-9a-f]{8}-", n)

    def _pro_add(self, files, force=False):
        lib = self.pro_lib()
        if missing_modules(["demoparser2"]):
            pip_install(["demoparser2"], self.assume_yes, "reading demo headers")
        added, skipped = lib.add(files, self.pro_pool(), self.cfg.int("PRO_TARGET", 25), force, log=info)
        for name, m in added:
            print(f"  {C.s('+', 'g')} {name} -> {m}")
        for name, why in skipped:
            print(C.s(f"  = {name}: {why}", "d"))
        if added:
            self._pro_ds = None
            good(f"Added {len(added)} pro demo(s) to {lib.root}. Next: `pro parse`")
        return len(added)

    def _pro_watch(self, expect=0, force=False, idle_minutes=20):
        src = Path(self.cfg["SORT_FROM"]).expanduser()
        if not src.exists():
            bad(f"Download folder not found: {src} (set SORT_FROM)")
            return
        snap = lambda: {f: f.stat().st_size for f in src.iterdir() if f.is_file() and self._is_pro_file(f)}
        done, pending, total, last = set(snap()), {}, 0, time.time()
        info(f"Watching {src} for HLTV demo downloads - Ctrl+C when you're done.")
        try:
            while True:
                for f, size in snap().items():
                    if f in done:
                        continue
                    if pending.get(f) == size and size > 0:
                        done.add(f)
                        pending.pop(f, None)
                        total += self._pro_add([f], force)
                        last = time.time()
                    else:
                        pending[f] = size
                if expect and total >= expect:
                    break
                if time.time() - last > idle_minutes * 60:
                    info(f"Nothing new for {idle_minutes} minutes - stopping.")
                    break
                time.sleep(2)
        except KeyboardInterrupt:
            print()
        if total and self.interactive and self.ask("Parse the new pro demos now? (Y/n)", "y").lower().startswith("y"):
            self.do_pro("parse")
        return total

    def signature_plays(self):
        """Scouted team's recurring T plays (current filters) + the closest pro rounds for each."""
        prints = t_round_prints(self.ds, demos=self.ds.demos_in_scope())
        min_n = max(2, self.cfg.int("MIN_SAMPLES", 4) - 1)
        groups = [g for g in cluster_plays(prints) if len(g) >= min_n]
        pds = self.pro_data()
        pro_prints = t_round_prints(pds) if pds is not None else []
        lib = self.pro_lib()
        links = lib.links()
        plays = []
        for g in groups:
            d = describe_play(g)
            d["examples"] = sorted(g, key=lambda p: p["date"], reverse=True)[:4]
            d["pros"] = []
            for s, q in match_pros(g, pro_prints, linked=set(links)):
                lk = links.get(lib.link_key(q["stem"], q["round"]))
                d["pros"].append(dict(sim=round(100 * s), team=q["team"], opponent=q["opponent"], round=q["round"],
                                      file=q["file"], stem=q["stem"], site=q["site"], won=q["won"],
                                      url=(lk or {}).get("url", ""), link_name=(lk or {}).get("name", ""),
                                      num=next((i for i, f in enumerate(lib.demo_files(), 1) if demo_stem(f) == q["stem"]), "?")))
            plays.append(d)
        return plays, len(prints), len(pro_prints)

    def do_signature(self, arg):
        """signature | signature report [pdf|html|both] - the scouted team's recurring T-side plays, each matched to
        the most similar rounds in your pro library (with your MetaStack links)."""
        if not self.need_data():
            return
        parts = arg.strip().lower().split()
        if parts and parts[0] == "report":
            return self.signature_report(parts[1] if len(parts) > 1 else "")
        plays, n_rounds, n_pro = self.signature_plays()
        if not plays:
            warn(f"No recurring T-side play found in {n_rounds} gun rounds ({self.an.scope_label()}). "
                 "More demos or `set MIN_SAMPLES 3` helps.")
            return
        if not n_pro:
            warn("No pro library parsed yet - showing the plays without pro matches. `pro` to get started.")
        for i, p in enumerate(plays, 1):
            print(C.s(f"\n  [{i}] {p['map']}  {p['title']}", "bold", "c")
                  + f"  - {p['rounds']} rounds, won {p['win_pct']}%, hits ~{p['hit']}")
            print(f"      utility: {p['utility']}")
            print(C.s("      e.g. " + "; ".join(f"vs {e['opponent']} r{e['round']}" for e in p["examples"]), "d"))
            for q in p["pros"]:
                link = q["url"] or C.s(f"(no MetaStack link: pro link {q['num']} {q['round']} <url>)", "d")
                print(f"      {C.s(str(q['sim']) + '%', 'g')} {q['team']} vs {q['opponent']} r{q['round']} "
                      f"[pro #{q['num']}]  {link}")
            if n_pro and not p["pros"]:
                print(C.s("      no close pro round in your library", "d"))
        unlinked = sum(1 for p in plays for q in p["pros"] if not q["url"])
        if unlinked:
            print(C.s(f"\n  {unlinked} matched pro round(s) have no MetaStack link yet. Find the round on MetaStack and save it "
                      "with `pro link <#> <round> <url> [name]` - the report links to it from then on.", "d"))
        print(C.s("  `signature report` builds the PDF/HTML version.", "d"))

    def signature_report(self, fmt=""):
        return self._hiding_dropped(self._signature_report_impl, fmt)

    def _signature_report_impl(self, fmt=""):
        self.use_theme()
        fmt = report_fmt(fmt or self.cfg["REPORT_FORMAT"])
        if fmt not in ("pdf", "html", "both"):
            bad("signature report pdf | html | both")
            return
        plays, n_rounds, n_pro = self.signature_plays()
        team = self.team_name or "Scouted team"
        sub = (f"{n_rounds} gun T rounds  |  pro library: {n_pro} T rounds  |  {self.an.scope_label()}  |  "
               f"generated {datetime.now():%Y-%m-%d %H:%M}")
        first = [("title", f"{team} - signature plays", sub),
                 ("bullets", [([], "Each play is a group of this team's T rounds with the same utility, route and site. "
                                   "Match % = how close the closest pro rounds in your library are (utility 40%, "
                                   "where the five stand 30%, site 20%, timing 10%).", ""),
                              ([], "MetaStack links are the ones you saved with `pro link`; rounds without one show the "
                                   "command to add it.", "")])]
        if not plays:
            first += [("bullets", [([], "No recurring T-side play with the current filters.", "")])]
        pages = [("signature", first)]
        by_map = defaultdict(list)
        for i, p in enumerate(plays, 1):
            by_map[p["map"]].append((i, p))
        for m, items in by_map.items():
            blocks = [("title", f"{m} - signature plays", f"{team}  |  {self.an.scope_label()}")]
            for i, p in items:
                blocks += [("h2", f"Play {i}: {p['title']}"),
                           ("bullets", [([f"{p['rounds']} rounds", f"won {p['win_pct']}%", f"hits ~{p['hit']}"],
                                         f"Utility: {p['utility']}", ""),
                                        ([], f"Where they go: {p['lanes']}", ""),
                                        ([], "Seen: " + "; ".join(f"{e['date']} vs {e['opponent']} round {e['round']}"
                                                                  for e in p["examples"]), "")])]
                if p["pros"]:
                    blocks += [("h3", "Closest pro rounds"),
                               ("links", [(f"{q['sim']}%  {q['team']} vs {q['opponent']}, round {q['round']} ({q['file']})"
                                           + (f" - {q['link_name']}" if q["link_name"] else ""),
                                           q["url"], "" if q["url"] else f"no MetaStack link yet: pro link {q['num']} {q['round']} <url>")
                                          for q in p["pros"]])]
                elif n_pro:
                    blocks += [("bullets", [([], "No close pro round in your library for this one.", "")])]
            pages.append((m, blocks))
        groups = add_index(pages)
        out = self.ws.out / "report"
        out.mkdir(parents=True, exist_ok=True)
        if fmt in ("html", "both"):
            path = out / f"{safe_name(team)}_signature.html"
            path.write_text(render_report_html(team, pages, []), encoding="utf-8")
            good(f"HTML signature report: {path.name}")
            open_path(path, self.cfg)
        if fmt in ("pdf", "both"):
            path = out / f"{safe_name(team)}_signature.pdf"
            n = save_report_pdf(pages, path, groups)
            good(f"PDF signature report ({n} pages): {path.name}")
            if fmt == "pdf":
                open_path(path, self.cfg)
        good(f"Signature report can be found at {out}")

    do_sig = do_signature

    # ---- franchise logos ---------------------------------------------------------------------
    def logo_dir(self):
        d = self.ws.root / "logos"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def logo_index(self):
        try:
            return json.loads((self.logo_dir() / "index.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def logo_for(self, ref):
        """Path of a downloaded org logo by franchise name, prefix, team name or list number ('' if none)."""
        idx = self.logo_index()
        ref = str(ref or "").strip().lower()
        if not ref or not idx:
            return ""
        items = sorted(idx.items(), key=lambda kv: kv[0].lower())
        if ref.isdigit() and 0 < int(ref) <= len(items):
            return items[int(ref) - 1][1]["file"]
        for name, e in items:
            if ref in (name.lower(), e.get("prefix", "").lower()) or ref in [t.lower() for t in e.get("teams", [])]:
                return e["file"]
        hits = [e["file"] for name, e in items if ref in name.lower()]
        return hits[0] if len(hits) == 1 else ""

    def team_logo(self):
        """The scouted team's org logo (downloads the logos the first time it's needed)."""
        try:
            fr = json.loads((self.ws.team_root / "team.json").read_text()).get("franchise", "")
        except Exception:
            fr = ""
        if not fr:
            return ""
        if not self.logo_index():
            self.do_logos("quiet")
        f = self.logo_for(fr)
        return f if f and Path(f).exists() else ""

    def do_logos(self, arg):
        """logos [refresh] - download every CSC org (franchise) logo, for `style logo <org>` / `style logo auto`."""
        quiet = arg.strip() == "quiet"
        api = self.csc()
        try:
            frs = api.franchises(active=True)
        except CscError as e:
            bad(str(e))
            return
        out = self.logo_dir()
        idx = {} if "refresh" in arg else self.logo_index()
        hosts = ([self.cfg["CSC_MEDIA"]] if self.cfg["CSC_MEDIA"] else []) + [h for h in Csc.MEDIA_HOSTS if h != self.cfg["CSC_MEDIA"]]
        got = failed = 0
        for f_ in frs:
            name, path = f_.get("name", "?"), ((f_.get("logo") or {}).get("url") or "")
            dst = out / (safe_name(f_.get("prefix") or name) + ".png")
            if name in idx and dst.exists() and "refresh" not in arg:
                continue
            if not path:
                failed += 1
                continue
            b, host = api.fetch_image(path, hosts)
            if not b:
                failed += 1
                continue
            if host is not None and host != self.cfg["CSC_MEDIA"] and host:
                self.cfg.set("CSC_MEDIA", host)  # remember where logos live
                self.cfg.save()
                hosts = [host] + [h for h in hosts if h != host]
            try:
                from PIL import Image
                Image.open(io.BytesIO(b)).convert("RGBA").save(dst)  # webp -> png (works everywhere)
            except Exception:
                failed += 1
                continue
            idx[name] = {"prefix": f_.get("prefix", ""), "file": str(dst), "teams": [t["name"] for t in f_.get("teams") or []]}
            got += 1
        (out / "index.json").write_text(json.dumps(idx, indent=2), encoding="utf-8")
        if failed and not got and not idx and not quiet:
            bad(f"Couldn't download the logos from any known CSC address. Right-click an org logo on playcsc.com, "
                f"`Copy Image Link`, and run:  set CSC_MEDIA <the part before /images/...>   then `logos` again.")
            return
        if quiet:
            return
        good(f"{len(idx)} org logo(s) in {out}" + (f" ({got} new)" if got else "") + (f"; {failed} couldn't be downloaded" if failed else ""))
        rows = [{"#": i, "org": n, "prefix": e.get("prefix", ""), "teams": ", ".join(e.get("teams", []))}
                for i, (n, e) in enumerate(sorted(idx.items(), key=lambda kv: kv[0].lower()), 1)]
        if rows:
            self.print_table(pd.DataFrame(rows), "CSC orgs", rows=100)
            print(C.s("  Use one: style logo <org name, prefix or #>  |  style logo auto = the scouted team's org", "d"))

    # ---- report styling -------------------------------------------------------------------
    def style_path(self):
        return self.ws.root / STYLE_FILE_NAME

    def use_theme(self):
        t = load_theme(self.cfg["REPORT_THEME"], self.style_path())
        t["team_logo"] = self.team_logo() if self.cfg.bool("REPORT_TEAM_LOGO") else ""
        if t.get("logo") == "auto":
            t["logo"] = "" if t["team_logo"] else self.team_logo()  # already in every corner
        apply_theme(t)
        return t

    def _style_file(self):
        path = self.style_path()
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            st = json.loads(json.dumps(STYLE_TEMPLATE))
        return st

    def do_style(self, arg):
        """style [theme] | style preview [theme|all] | style logo <file|org|auto|off> | style footer <text> | style color <name> <#hex>
        | style font <file.ttf> [bold.ttf] | style edit | style reset - how the reports look."""
        parts = shlex.split(arg) if arg.strip() else []
        sub = parts[0].lower() if parts else ""
        if not sub or sub in ("list", "show"):
            cur = self.cfg["REPORT_THEME"] or "midnight"
            print(C.s("\n  Report themes", "bold"))
            for i, (k, t) in enumerate(THEMES.items(), 1):
                mark = C.s("  (current)", "g") if k == cur else ""
                print(f"  {C.s('[' + str(i) + ']', 'c', 'bold')} {k:<9} {t['label']}{mark}")
            st = self.style_path()
            if st.exists():
                t = load_theme(cur, st)
                bits = [f"{k}={t[k]}" for k in ("font", "logo", "footer") if t.get(k)]
                over = {k: v for k, v in (self._style_file().get("colors") or {}).items() if v}
                print(C.s(f"\n  Your tweaks ({st}):", "bold") + " " + (", ".join(bits + [f"{k} {v}" for k, v in over.items()]) or "none"))
            print(C.s("\n  style <name> to switch  |  style preview all  |  style logo / footer / color / font  |  style edit", "d"))
            if self.interactive and not parts:
                a = self.ask("Number or name to switch (Enter to keep)", "")
                if a:
                    self.do_style(list(THEMES)[int(a) - 1] if a.isdigit() and 0 < int(a) <= len(THEMES) else a)
            return
        if sub in THEMES or (sub.isdigit() and 0 < int(sub) <= len(THEMES)):
            name = list(THEMES)[int(sub) - 1] if sub.isdigit() else sub
            self.cfg.set("REPORT_THEME", name)
            self.cfg.save()
            good(f"Reports now use the {name} theme: {THEMES[name]['label']}")
            return
        if sub == "preview":
            which = parts[1].lower() if len(parts) > 1 else (self.cfg["REPORT_THEME"] or "midnight")
            names = list(THEMES) if which == "all" else [which]
            if any(n not in THEMES for n in names):
                bad(f"Unknown theme. Themes: {', '.join(THEMES)}")
                return
            return self.style_preview(names)
        if sub in ("edit", "file"):
            path = self.style_path()
            if not path.exists():
                path.write_text(json.dumps(STYLE_TEMPLATE, indent=2), encoding="utf-8")
            good(f"Style file: {path}")
            print(C.s("    Edit colors / font / logo / footer there; blank = the theme's own value. Then `style preview`.", "d"))
            open_path(path, self.cfg) if self.interactive else None
            return
        if sub == "reset":
            if self.style_path().exists():
                self.style_path().unlink()
            good("Removed your style tweaks - reports use the plain theme again.")
            return
        st = self._style_file()
        if sub == "logo" and len(parts) > 1 and parts[1].lower() in ("auto", "team"):
            st["logo"] = "auto"
            if not self.team_logo():
                warn("Couldn't find the scouted team's org logo yet (`logos` downloads them) - saved anyway.")
        elif sub == "logo" and len(parts) > 1 and parts[1].lower() in ("off", "none"):
            st["logo"] = ""
        elif sub == "logo" and len(parts) > 1 and not Path(" ".join(parts[1:])).expanduser().exists():
            ref = " ".join(parts[1:])
            if not self.logo_index():
                self.do_logos("quiet")
            org = self.logo_for(ref)
            if not org:
                bad(f"'{ref}' isn't a file or a CSC org. `logos` lists the orgs.")
                return
            st["logo"] = org
        elif sub == "logo" and len(parts) > 1:
            lp = Path(" ".join(parts[1:])).expanduser()
            dst = self.ws.root / ("report_logo" + lp.suffix.lower())  # keep a copy so moving the original doesn't break it
            shutil.copyfile(lp, dst)
            st["logo"] = str(dst)
        elif sub == "logo":
            st["logo"] = ""
        elif sub == "footer":
            st["footer"] = " ".join(parts[1:])  # blank = back to the default, `off` = no footer
        elif sub in ("color", "colour") and len(parts) == 3:
            key, val = parts[1].lower(), parts[2]
            alias = {"accent": "acc", "background": "bg", "text": "ink", "muted": "mut", "lines": "line", "box": "card",
                     "headers": "head", "header": "head", "headings": "head"}
            key = alias.get(key, key)
            if key not in THEME_KEYS or not re.fullmatch(r"#?[0-9a-fA-F]{6}", val):
                bad("style color <acc|head|bg|card|ink|mut|line> <#rrggbb>   e.g. style color head #ff8800")
                return
            st.setdefault("colors", {})[key] = "#" + val.lstrip("#")
        elif sub == "font" and len(parts) > 1:
            for k, v in zip(("font", "font_bold"), parts[1:3]):
                if not Path(v).exists():
                    bad(f"Font not found: {v}")
                    return
                st[k] = v
        else:
            bad("style [theme] | style preview [theme|all] | style logo <file|org|auto|off> | style footer <text> | "
                "style color <acc|bg|card|ink|mut|line> <#hex> | style font <file.ttf> [bold.ttf] | style edit | style reset")
            return
        self.style_path().write_text(json.dumps(st, indent=2), encoding="utf-8")
        good("Saved. `style preview` to see it.")

    do_theme = do_style

    def style_preview(self, names):
        """One sample page per theme (fake numbers) so you can compare looks without real data."""
        out = self.ws.out / "style_preview"
        out.mkdir(parents=True, exist_ok=True)
        demo = pd.DataFrame({"map": ["de_mirage", "de_inferno", "de_anubis", "de_nuke"], "played": [6, 4, 3, 1],
                             "W-L": ["4-2", "3-1", "1-2", "0-1"], "win%": [67.0, 75.0, 33.0, 0.0], "their bans": [0, 0, 1, 5]})
        blocks = [("title", f"{self.team_name or 'Sirens'} - scouting report", "sample page  |  S21  |  theme preview"),
                  ("h2", "Top counter-play"),
                  ("bullets", [(["de_mirage T", "site preference"], "They go A in 64% of T rounds. Lean your CT setup "
                                "toward A and rotate off early info.", "29/45 T rounds"),
                               (["de_inferno CT", "aggression"], "Their AWPer takes early fights at Banana on CT and wins "
                                "70%. Don't dry-peek it.", "13 opening duels")]),
                  ("h2", "Map veto"), ("table", demo, 10),
                  ("links", [("82%  Vitality vs FaZe, round 8 - A split", "https://example.com", ""),
                             ("80%  G2 vs MOUZ, round 24", "", "no MetaStack link yet")])]
        saved = self.cfg["REPORT_THEME"]
        paths = []
        from PIL import Image, ImageDraw
        try:
            for n in names:
                t_ = load_theme(n, self.style_path() if n == (saved or "midnight") else None)
                t_["team_logo"] = self.team_logo() if self.cfg.bool("REPORT_TEAM_LOGO") else ""
                if t_.get("logo") == "auto":
                    t_["logo"] = "" if t_["team_logo"] else self.team_logo()
                apply_theme(t_)
                pages = [("preview", [b for b in blocks])]
                groups = add_index(pages, cover=False)
                img = render_page_jpg(pages[0][1])[0]["img"]
                d = ImageDraw.Draw(img)
                d.text((img.width - 260, img.height - 34), f"theme: {n}", fill=THEME["mut"])
                p_ = out / f"theme_{n}.png"
                img.save(p_)
                paths.append(p_)
        finally:
            self.use_theme()
        if len(paths) > 1:  # contact sheet: all themes side by side, 4 per row
            ims = [Image.open(x) for x in paths]
            w = 700
            ims = [im.resize((w, int(im.height * w / im.width))) for im in ims]
            cols = 4
            rows_ = math.ceil(len(ims) / cols)
            hh = max(im.height for im in ims)
            sheet = Image.new("RGB", (cols * w + (cols + 1) * 16, rows_ * hh + (rows_ + 1) * 16), "#808080")
            for i, im in enumerate(ims):
                sheet.paste(im, (16 + (i % cols) * (w + 16), 16 + (i // cols) * (hh + 16)))
            sp = out / "all_themes.png"
            sheet.save(sp)
            paths.insert(0, sp)
        good(f"Preview(s): {', '.join(p_.name for p_ in paths)}")
        good(f"Theme previews can be found at {out}")
        if self.cfg.bool("OPEN_FILES"):
            open_path(paths[0], self.cfg)

    def _hiding_dropped(self, fn, *a, keep=None):
        """Run a report builder with dropped players left out (REPORT_HIDE_DROPPED), unless he's the subject."""
        hide = self.cfg.bool("REPORT_HIDE_DROPPED") and self.ds.loaded
        if hide and keep and self.ds.status_of(keep) == "dropped":
            hide = False
        if hide:
            n = len(self.ds.dropped_sids())
            if n:
                info(f"Leaving {n} dropped player(s) out of the report (`set REPORT_HIDE_DROPPED false` to keep them).")
        old = self.ds.hide_dropped
        self.ds.hide_dropped = hide
        try:
            return fn(*a)
        finally:
            self.ds.hide_dropped = old

    def do_report(self, arg):
        """report [pdf|html|both] | report player <name> [fmt] - team report, or a full report on one player."""
        parts = arg.strip().split()
        if parts and parts[0].lower() in ("player", "p"):
            return self._report_impl(arg)
        return self._hiding_dropped(self._report_impl, arg)

    def _report_impl(self, arg):
        self.use_theme()
        if not self.need_data():
            return
        parts = arg.strip().split()
        if parts and parts[0].lower() in ("player", "p"):
            fmt = parts[-1].lower() if len(parts) > 2 and parts[-1].lower() in ("pdf", "jpg", "html", "both") else ""
            name = " ".join(parts[1:-1] if fmt else parts[1:])
            return self.player_report(name, fmt)
        fmt = arg.strip().lower()
        if not fmt and self.interactive:
            default = report_fmt(self.cfg["REPORT_FORMAT"])
            print(C.s("  (for one player instead: report player <name>)", "d"))
            print(C.s("\n  Report format", "bold"))
            for i, (k, label) in enumerate((("pdf", "PDF (one file: summary, a page per map, players)"),
                                            ("html", "HTML page (opens in your browser right away)"),
                                            ("both", "Both - HTML opens right away, PDF saved next to it")), 1):
                print(f"  {C.s('[' + str(i) + ']', 'c', 'bold')} {label}" + (C.s("  (default)", "d") if k == default else ""))
            ans = self.ask("Choose", {"pdf": "1", "html": "2", "both": "3"}.get(default, "1")).lower()
            fmt = {"1": "pdf", "2": "html", "3": "both"}.get(ans, ans)
        fmt = report_fmt(fmt or self.cfg["REPORT_FORMAT"])
        if fmt not in ("pdf", "html", "both"):
            bad("report pdf | html | both")
            return
        a = self.an
        maps = a.maps_in_scope()
        team = self.team_name or "Scouted team"
        info(f"Building report for {team}: {len(maps)} map(s)...")
        self._in_report = True
        out = self.ws.out / "report"
        imgs = out / "img"
        n_demos = len(self.ds.matches[self.ds.matches["demo"].isin(self.ds.demos_in_scope())])
        sub = f"{n_demos} demos  |  {a.scope_label()}  |  generated {datetime.now():%Y-%m-%d %H:%M}"
        cp = a.counterplay()
        bullet = lambda x: ([f"{x['map']} {x['their_side']}", x["area"]], show_text(x["suggestion"]), x["evidence"])

        # ---- page 1: summary
        side_order = {"T": 0, "CT": 1, "both": 2}
        side_name = {"T": "their T side", "CT": "their CT side", "both": "both sides"}

        def grouped(items, per_group):
            """Counter-play grouped by map, then their side (T, CT, both); strongest first inside each group."""
            blocks = []
            items = sorted(items, key=lambda x: (x["map"] == "all", x["map"], side_order.get(x["their_side"], 3), -x["priority"]))
            for (m_, sd), grp in itertools.groupby(items, key=lambda x: (x["map"], x["their_side"])):
                grp = list(grp)[:per_group]
                head = ("All maps" if m_ == "all" else m_) + "  ·  " + side_name.get(sd, sd)
                blocks += [("h3", head), ("bullets", [([x["area"]], show_text(x["suggestion"]), x["evidence"]) for x in grp])]
            return blocks

        summary = [("title", f"{team} - scouting report", sub), ("h2", "Top counter-play")] + grouped(cp, 4)
        try:
            vv = self.veto_view() if self.ws.manifest() else None
        except Exception as e:
            vv = None
            warn(f"Vetos skipped in report: {e}")
        if vv and vv.tables:
            summary += [("h2", "Map veto"), ("bullets", [([], show_text(n_), "") for n_ in vv.notes])]
            for t, df in vv.tables:
                if t.startswith("Veto history"):
                    summary += [("h3", "Recent vetos"), ("bullets", [([r["date"], f"vs {r['opponent']}"], r["veto"], r["result"])
                                                                   for _, r in df.head(10).iterrows()])]
                else:
                    summary += [("h3", t), ("table", df, 10)]
        summary += [("h2", "Anomalies"), ("bullets", [([x["kind"]], show_text(x["text"]), "") for x in a.anomalies()[:15]]),
                    ("h2", "Overview")]
        for t, df in a.v_overview().tables:
            summary += [("h3", t), ("table", df, 20)]
        pages = [("summary", summary)]

        # ---- one page per map
        saved_map = self.cfg["MAP"]
        try:
            for m in maps:
                self.cfg.v["MAP"] = m
                blocks = [("title", m, f"{team}  |  {a.scope_label()}")]
                mr = a.player_roles()
                if len(mr):
                    blocks += [("h2", "Roles on this map"),
                               ("table", mr[["player", "roster", "role", "CT role", "IGL", "why"]].rename(columns={"why": "assessment"}), 10)]
                pw = a.power_positions()
                if len(pw):
                    blocks += [("h2", "Power positions"),
                               ("table", pw[["half", "player", "roster", "power position", "% of rounds", "K-D there", "next most"]], 12)]
                mcp = [x for x in cp if x["map"] == m]
                if mcp:
                    blocks += [("h2", "Counter-play on this map"),
                               ("bullets", [([x["their_side"], x["area"]], show_text(x["suggestion"]), x["evidence"]) for x in mcp[:8]])]
                pics = []
                for key in ("power", "openings", "smoke", "flash", "molotov", "pos_early", "pos_mid", "pos_late", "postplant", "rotations", "lowbuy"):
                    v = self.build_view(key)
                    for _, fn in v.plots:
                        pics.extend(fn(imgs))
                blocks.append(("images", pics))
                for key, ntab in (("executes", 5), ("setups", 2), ("rotations", 1), ("postplant", 4), ("openings", 3), ("smoke", 2), ("molotov", 1),
                                  ("flash", 1), ("pos_early", 1), ("pos_mid", 1), ("pos_late", 1), ("economy", 2), ("lowbuy", 8)):
                    v = self.build_view(key)
                    blocks.append(("h2", v.title))
                    for t, df in v.tables[:ntab]:
                        blocks += [("h3", t), ("table", df, 10)]
                    if v.notes:
                        blocks.append(("bullets", [([], show_text(n_), "") for n_ in v.notes]))
                pages.append((m, blocks))
        finally:
            self.cfg.v["MAP"] = saved_map
            self._in_report = False

        # ---- players
        pl = [("title", "Players", f"{team}  |  {a.scope_label()}")]
        rv = a.v_roles()
        if rv.tables:
            pl += [("h2", "Roles"), ("table", rv.tables[0][1], 15)]
        Rr = self.ds.roster()
        if len(Rr):
            pl += [("h2", "Roster"), ("table", Rr.drop(columns=["steamid", "auto"]).rename(
                columns={"share": "% maps", "on_team": "on CSC roster", "recent": "last 5 maps", "last": "last played",
                         "now": "where he is now"}), 20)]
        for t, df in a.v_players().tables:
            pl += [("h2", t), ("table", df, 25)]
        for t, df in a.v_trends().tables:
            pl += [("h2", "Trends - match by match"), ("table", df, 30)]
        pl += [("h2", "All counter-play suggestions")] + grouped(cp, 99)
        pages.append(("players", pl))

        out.mkdir(parents=True, exist_ok=True)
        groups = add_index(pages)
        made = []
        if fmt in ("html", "both"):
            path = out / f"{safe_name(team)}_report.html"
            path.write_text(render_report_html(team, pages, maps), encoding="utf-8")
            made.append(path)
            good(f"HTML report: {path}")
            open_path(path, self.cfg)  # open it right away; the PDF is built after
        if fmt in ("pdf", "both"):
            for old in out.glob(f"{safe_name(team)}_report_*.jpg"):  # pages from older versions
                old.unlink()
            path = out / f"{safe_name(team)}_report.pdf"
            n_pages = save_report_pdf(pages, path, groups)
            made.append(path)
            good(f"PDF report ({n_pages} pages): {path.name}")
            print(C.s("[+] ", "g", "bold") + f"Report can be found at {out}")
        if made and fmt == "pdf":
            open_path(made[-1], self.cfg)

    def _ask_format(self, fmt):
        if fmt:
            return fmt
        default = report_fmt(self.cfg["REPORT_FORMAT"])
        if not self.interactive:
            return default
        ans = self.ask("Format: [1] PDF  [2] HTML  [3] Both", {"pdf": "1", "html": "2", "both": "3"}.get(default, "1"))
        return report_fmt({"1": "pdf", "2": "html", "3": "both"}.get(ans.strip().lower(), ans.strip().lower() or default))

    def _pick_player(self, name):
        names = self.ds.tgt_names(all_players=True)
        if not name and self.interactive:
            for i, n in enumerate(names, 1):
                print(f"  [{i}] {self.ds.label(n)}")
            name = self.ask("Player number or name")
        if not name:
            return None
        if name.isdigit() and 0 < int(name) <= len(names):
            return names[int(name) - 1]
        hit = [n for n in names if n.lower() == name.lower()] or [n for n in names if name.lower() in n.lower()]
        if len(hit) != 1:
            bad(f"No single player matches '{name}'. Players: {', '.join(names)}")
            return None
        return hit[0]

    def player_form(self, n):
        """Match-by-match numbers for one player."""
        P = self.ds.f("pos", side=False, phase=False, player=False)
        K = self.ds.f("kills", side=False, phase=False, player=False)
        N = self.ds.f("nades", side=False, phase=False, player=False)
        D = self.ds.f("dmg", side=False, phase=False, player=False)
        mine = P[P["tgt"] & (P["name"] == n)]
        sid = mine["steamid"].iloc[0] if len(mine) else ""
        rows = []
        for demo, g in mine.groupby("demo"):
            r = g["key"].nunique()
            k = K[(K["demo"] == demo) & K["tgt_att"] & (K["att_name"] == n)]
            dd = K[(K["demo"] == demo) & K["tgt_vic"] & (K["vic_name"] == n)]
            dm = D[(D["demo"] == demo) & (D["att_sid"] == sid)]["dmg"].sum() if len(D) else 0
            meta = self.ds.matches.set_index("demo").loc[demo]
            rows.append({"date": meta["date"], "opponent": meta["opponent"], "map": meta["map"], "rounds": r,
                         "K": len(k), "D": len(dd), "ADR": round(dm / max(1, r), 1), "FK": int(k["opening"].sum()),
                         "FD": int(dd["opening"].sum()), "nades": int(((N["demo"] == demo) & N["tgt"] & (N["name"] == n)).sum())})
        return pd.DataFrame(rows).sort_values("date") if rows else pd.DataFrame()

    def player_report(self, name="", fmt=""):
        n = self._pick_player(name)
        if not n:
            return
        return self._hiding_dropped(self._player_report_impl, n, fmt, keep=n)

    def _player_report_impl(self, name="", fmt=""):
        self.use_theme()
        n = self._pick_player(name)
        if not n:
            return
        fmt = self._ask_format(fmt)
        fmt = report_fmt(fmt)
        if fmt not in ("pdf", "html", "both"):
            bad("Format must be pdf, html or both")
            return
        a = self.an
        saved = {k: self.cfg.v[k] for k in ("PLAYER", "MAP")}
        team = self.team_name or "Scouted team"
        status = self.ds.status_of(n)
        info(f"Building player report for {n}...")
        out = self.ws.out / "report"
        imgs = out / "img"
        mentions = lambda text: re.search(rf"\b{re.escape(n)}\b", str(text), re.I) is not None
        try:
            self.cfg.v["PLAYER"] = n
            maps = a.maps_in_scope()
            played = sorted(self.ds.pos[self.ds.pos["tgt"] & (self.ds.pos["name"] == n)]["map"].unique())
            maps = [m for m in maps if m in played]
            sub = f"{team}  |  {status or 'player'}  |  {a.scope_label()}  |  generated {datetime.now():%Y-%m-%d %H:%M}"
            first = [("title", f"{n} - player report", sub)]
            self.cfg.v["PLAYER"] = ""
            rl = a.roles_by_map() if not self.cfg["MAP"] else a.player_roles().assign(map=self.cfg["MAP"])
            self.cfg.v["PLAYER"] = n
            if len(rl) and (rl["player"] == n).any():
                first += [("h2", "Role by map"), ("bullets", [([rr["map"], rr["role"]] + ([rr["CT role"]] if rr["CT role"] else [])
                                                                + (["IGL"] if rr["IGL"] else []), rr["why"], rr["IGL"])
                                                               for _, rr in rl[rl["player"] == n].iterrows()])]
            pw = a.power_positions()
            if len(pw):
                first += [("h2", "Power positions"),
                          ("table", pw[["map", "half", "power position", "% of rounds", "K-D there", "next most", "rounds"]], 20)]
            Pt = a.v_players().tables
            stats = Pt[0][1]
            if len(stats):
                first += [("h2", "Stats"), ("table", stats[stats["player"] == n], 3)]
                saved_side = self.cfg.v["SIDE"]
                rows = []
                for sd in ("T", "CT"):
                    self.cfg.v["SIDE"] = sd
                    t = a.v_players().tables[0][1]
                    t = t[t["player"] == n].copy()
                    if len(t):
                        t.insert(1, "side", sd)
                        rows.append(t)
                self.cfg.v["SIDE"] = saved_side
                if rows:
                    first += [("h3", "By side"), ("table", pd.concat(rows), 4)]
            if len(Pt) > 1 and len(Pt[1][1]):
                first += [("h2", "Where he plays (mid-round)"), ("table", Pt[1][1][Pt[1][1]["player"] == n], 6)]
            o = a.v_openings()
            for t, df in o.tables[1:]:
                won = t.startswith("Where they WIN")
                first += [("h2" if won else "h3", "Where he wins opening duels" if won else "Where he loses opening duels"),
                          ("table", df.drop(columns=["players"], errors="ignore"), 8)]
            for kind, label in (("smoke", "Smokes"), ("flash", "Flashes"), ("molotov", "Molotovs"), ("he", "HE grenades")):
                v = a.v_utility(kind)
                if v.tables:
                    first += [("h2", f"His {label.lower()}")]
                    for t, df in v.tables[:2]:
                        first += [("h3", t), ("table", df, 8)]
            form = self.player_form(n)
            if len(form):
                first += [("h2", "Match by match"), ("table", form, 30)]
            self.cfg.v["PLAYER"] = ""  # team-wide lists, filtered to lines about him
            cp = [x for x in a.counterplay() if mentions(x["suggestion"])]
            an = [x for x in a.anomalies() if mentions(x["text"])]
            self.cfg.v["PLAYER"] = n
            if cp:
                first += [("h2", "Counter-play involving him"),
                          ("bullets", [([f"{x['map']} {x['their_side']}", x["area"]], show_text(x["suggestion"]), x["evidence"]) for x in cp])]
            if an:
                first += [("h2", "What stands out"), ("bullets", [([x["kind"]], show_text(x["text"]), "") for x in an])]
            pages = [("profile", first)]
            for m in maps:
                self.cfg.v["MAP"] = m
                blocks = [("title", f"{n} on {m}", f"{team}  |  {a.scope_label()}")]
                pics = []
                for key in ("openings", "smoke", "flash", "molotov", "pos_early", "pos_mid", "pos_late", "postplant", "rotations"):
                    v = self.build_view(key)
                    for _, fn in v.plots:
                        pics.extend(fn(imgs))
                blocks.append(("images", pics))
                for key, ntab in (("openings", 3), ("smoke", 2), ("flash", 1), ("molotov", 1),
                                  ("pos_early", 1), ("pos_mid", 1), ("pos_late", 1), ("postplant", 3), ("rotations", 1)):
                    v = self.build_view(key)
                    if v.tables:
                        blocks.append(("h2", v.title))
                        for t, df in v.tables[:ntab]:
                            blocks += [("h3", t), ("table", df, 10)]
                pages.append((m, blocks))
        finally:
            for k, val in saved.items():
                self.cfg.v[k] = val
        out.mkdir(parents=True, exist_ok=True)
        base = f"{safe_name(team)}_player_{safe_name(n)}"
        groups = add_index(pages)
        made = []
        if fmt in ("html", "both"):
            path = out / f"{base}.html"
            path.write_text(render_report_html(f"{n} ({team})", pages, maps), encoding="utf-8")
            made.append(path)
            good(f"HTML report: {path}")
            open_path(path, self.cfg)
        if fmt in ("pdf", "both"):
            for old in out.glob(f"{base}_*.jpg"):  # pages from older versions
                old.unlink()
            path = out / f"{base}.pdf"
            n_pages = save_report_pdf(pages, path, groups)
            made.append(path)
            good(f"PDF player report ({n_pages} pages): {path.name}")
            print(C.s("[+] ", "g", "bold") + f"Player report can be found at {out}")
        if made and fmt == "pdf":
            open_path(made[-1], self.cfg)

    # ---------------------------------------------------------------- export & misc
    def do_export(self, arg):
        """export [last|search|all] [csv|json] - save the last view/search, or every table (all)."""
        parts = arg.split()
        what = parts[0] if parts else "last"
        fmt = parts[1] if len(parts) > 1 else "csv"
        out = self.ws.out / "exports"
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if what == "all":
            if not self.need_data():
                return
            for name in ("rounds", "kills", "nades", "pos", "dmg", "index"):
                t = getattr(self.ds, name)
                if t is None or not len(t):
                    continue
                p = out / f"{name}_{stamp}.{fmt}"
                t.to_json(p, orient="records") if fmt == "json" else t.to_csv(p, index=False)
                good(str(p))
            return
        v = self.last
        if what == "search" and self.last_search is not None:
            v = View("search", "search").table("results", self.last_search.drop(columns=["text"], errors="ignore"))
        if v is None:
            bad("Nothing to export yet.")
            return
        for sub, df in v.tables:
            p = out / f"{v.key}_{safe_name(sub)[:30].replace(' ', '_')}_{stamp}.{fmt}"
            df.to_json(p, orient="records") if fmt == "json" else df.to_csv(p, index=False)
            good(str(p))

    def do_clear(self, arg):
        """clear - clear the screen.  (clear cache [...] = same as `clearcache`)"""
        if arg.strip().lower().startswith("cache"):
            return self.do_clearcache(arg.strip()[5:])
        os.system("cls" if os.name == "nt" else "clear")

    def do_clearcache(self, arg):
        """clearcache [parsed|pro|images|all] [-y] - delete cached data so it's rebuilt. Demos are never deleted.
        parsed = this team's parsed demo data (default; `parse` rebuilds it), pro = the pro library's parsed data
        (`pro parse`), images = heatmaps/report pictures, all = all three."""
        words = arg.lower().split()
        yes = any(w in ("-y", "y", "yes") for w in words)
        what = next((w for w in words if w in ("parsed", "parse", "pro", "images", "img", "all")), "parsed")
        what = {"parse": "parsed", "img": "images"}.get(what, what)
        targets = []
        if what in ("parsed", "all"):
            targets.append((f"parsed data for {self.team_name or 'this team'}", list(self.ws.cache.glob("*.pkl"))
                            + list(self.ws.cache.glob("*.meta.json"))))
        if what in ("pro", "all"):
            lib = self.pro_lib()
            targets.append(("pro library parsed data", list(lib.cache.glob("*.pkl"))))
        if what in ("images", "all"):
            imgs = [f for d in (self.ws.out / "heatmaps", self.ws.out / "report" / "img") if d.exists()
                    for f in d.rglob("*") if f.is_file()]
            targets.append(("heatmap/report images", imgs))
        total = sum(len(f) for _, f in targets)
        if not total:
            info("Nothing cached to clear.")
            return
        size = sum(f.stat().st_size for _, fs in targets for f in fs if f.exists())
        for label, fs in targets:
            print(f"  {label}: {len(fs)} file(s)")
        if self.interactive and not yes and not self.ask(f"Delete these {total} file(s), {human_size(size)}? Demos are kept. (y/N)",
                                                          "n").lower().startswith("y"):
            info("Nothing deleted.")
            return
        for _, fs in targets:
            for f in fs:
                try:
                    f.unlink()
                except Exception as e:
                    warn(f"{f.name}: {e}")
        if what in ("parsed", "all"):
            self.ds.clear()
        if what in ("pro", "all"):
            self._pro_ds = None
        good(f"Cleared {total} file(s) ({human_size(size)}).")
        nxt = [c for w_, c in (("parsed", "`parse`"), ("pro", "`pro parse`")) if what in (w_, "all")]
        if nxt:
            print(C.s("    Next: " + " and ".join(nxt) + " to rebuild from your demos.", "d"))

    def do_exit(self, arg):
        """exit - quit."""
        print("bye")
        return True

    do_quit = do_exit
    do_EOF = do_exit


# =============================================================================
#  Entry point
# =============================================================================
def setup_tls(assume_yes=False):
    """Make every HTTPS request use an up-to-date certificate bundle (certifi). Older Python installs ship stale
    root certificates and then reject valid sites with 'certificate has expired'. Verification stays ON."""
    import ssl
    try:
        import certifi
    except ImportError:
        if not pip_install(["certifi"], assume_yes, "up-to-date HTTPS certificates"):
            return False
        importlib.invalidate_caches()
        try:
            import certifi
        except ImportError:
            return False
    if str(getattr(certifi, "__version__", "2099"))[:4] < "2022":  # old bundles still carry the expired root
        info(f"Your certifi ({certifi.__version__}) is too old for some sites - updating it.")
        subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "certifi"])
        info("Updated. Restart the script if CSC still can't be reached.")
    ctx = ssl.create_default_context(cafile=certifi.where())
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)))
    return True


def main():
    ap = argparse.ArgumentParser(description="CSC Analyst - scouting console for CS Confederation teams")
    ap.add_argument("-w", "--workspace", default=str(Path.cwd() / "csc_workspace"), help="workspace folder")
    ap.add_argument("-d", "--demo-dir", help="where to store demos (saved as DEMO_DIR), e.g. D:\\CS\\demos")
    ap.add_argument("-x", "--execute", help="commands to run, separated by ';' (then exit unless -i)")
    ap.add_argument("-r", "--resource", help="file with one command per line")
    ap.add_argument("-i", "--interactive", action="store_true", help="stay in the console after -x / -r")
    ap.add_argument("-q", "--quiet", action="store_true", help="no banner")
    ap.add_argument("-y", "--yes", action="store_true", help="install missing packages without asking")
    ap.add_argument("--json", action="store_true", help="print views as JSON (sets OUTPUT json for this run)")
    args = ap.parse_args()
    setup_tls(args.yes)

    if os.name == "nt":
        os.system("")  # enables ANSI colours in the Windows console
    load_core(args.yes)
    ws = Workspace(Path(args.workspace).expanduser().resolve())
    scripted = bool(args.execute or args.resource)
    con = Console(ws, interactive=not scripted or args.interactive, assume_yes=args.yes)
    if args.demo_dir:
        con.onecmd(f"set DEMO_DIR {args.demo_dir}")
    if args.json:
        con.cfg.transient["OUTPUT"] = "json"
        C.on = False
    try:
        import readline  # noqa: F401  (history + tab completion where available)
    except Exception:
        pass
    cmds = []
    if args.resource:
        cmds += [l.strip() for l in Path(args.resource).read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
    if args.execute:
        cmds += [c.strip() for c in args.execute.split(";") if c.strip()]
    for c in cmds:
        if not args.json:
            print(con.prompt + c)
        if con.onecmd(c):
            return
    if scripted and not args.interactive:
        return
    if not args.quiet:
        print(C.s(BANNER.format(v=VERSION), "c"))
        print(C.s("  scouting console for CS Confederation teams  |  workspace: ", "d") + str(ws.root))
        if con.team_name:
            print(C.s("  team: ", "d") + con.team_name)
        con.do_menu("")
    try:
        con.cmdloop()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
