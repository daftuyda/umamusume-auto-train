from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple, Optional, Any, List, Union, Iterable
from urllib.parse import urlencode
import re
import requests
import logging
import math

API_BASE = "https://umasearch.notvoid.moe/api/event_by_name"
HTTP_TIMEOUT = 6.0

STAT_CANON = {
    "speed": "Speed",
    "stamina": "Stamina",
    "power": "Power",
    "guts": "Guts",
    "wisdom": "Wisdom",
}

SCORING = {
    "stat_weights": {
        "Speed": 0.9,
        "Stamina": 0.9,
        "Power": 0.9,
        "Guts": 0.6,
        "Wisdom": 0.6,
        "_default": 0.6,
    },
    "negative_stat_penalty_mult": 1.2,

    "energy_point": 1.0,
    "skill_points_point": 0.5,
    "bond_point": 0.2,

    "hint_point": 1.5,
    "hint_name_boosts": {},

    "good_result_bonus": 2.0,
    "bad_result_penalty": -4.0,

    "debuff_penalties": {
        "Slow Metabolism": -12.0,
        "Injured": -18.0,
        "Fatigue": -6.0,
        "_generic_status": -5.0,
    },

    "assume_missing_chance_as": 1.0,
    "cap_decay_strength": 0.10,
}

@dataclass
class Context:
    current_energy: Optional[int] = None
    max_energy: Optional[int] = None
    prefer_energy_below: int = 30
    low_energy_multiplier: float = 1.5
    stat_caps: Optional[Dict[str, int]] = None
    current_stats: Optional[Dict[str, int]] = None
    avoid_bad_result: bool = True
    hard_avoid_statuses: Optional[List[str]] = None

DEFAULT_CONTEXT = Context()

def fetch_event_by_name(
    event_name: str,
    *,
    global_only: bool = False,
    kinds: Optional[List[str]] = None,
    min_score: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    if not event_name:
        return None
    params: Dict[str, str] = {"event_name": event_name}
    if global_only:
        params["global_only"] = "true"
    if kinds:
        params["kinds"] = ",".join(kinds)
    if min_score is not None:
        params["min_score"] = str(min_score)
    try:
        url = f"{API_BASE}?{urlencode(params)}"
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logging.warning("fetch_event_by_name failed for %r: %s", event_name, e)
        return None
    match = data.get("match")
    if not match or not isinstance(match, dict) or "data" not in match:
        return None
    return data

def _flatten_rewards(items: Iterable[Any]) -> List[Any]:
    """Flatten arbitrarily nested lists/tuples of rewards."""
    flat: List[Any] = []
    queue = list(items) if isinstance(items, (list, tuple)) else [items]
    while queue:
        x = queue.pop(0)
        if isinstance(x, (list, tuple)):
            queue = list(x) + queue
        else:
            flat.append(x)
    return flat

def _canon_stat(name: str) -> str:
    if not name:
        return "Unknown"
    return STAT_CANON.get(name.lower(), name[:1].upper() + name[1:])

def _first_number(val: Any) -> Optional[float]:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, list) and val:
        for x in val:
            if isinstance(x, (int, float)):
                return float(x)
    if isinstance(val, str):
        m = re.search(r"-?\d+(\.\d+)?", val)
        if m:
            return float(m.group(0))
    return None

HINT_RX = re.compile(r"(?P<name>.+?)\s*(?:hint\s*)?\+?(?P<lvl>-?\d+)", re.IGNORECASE)
STATUS_RX = re.compile(r"(?:Get|Gain|Apply)\s+(?P<name>.+?)\s+status", re.IGNORECASE)
CHANCE_RX = re.compile(r"~?\s*(?P<pct>\d{1,3})\s*%")

def _extract_textual_hints_and_statuses(text: str, bucket: List[Dict[str, Any]]):
    m = HINT_RX.search(text or "")
    if m:
        hint_name = m.group("name").strip().rstrip(":")
        lvl = int(m.group("lvl"))
        bucket.append({"kind": "hint", "name": hint_name, "value": lvl, "raw": text})
    s = STATUS_RX.search(text or "")
    if s:
        status = s.group("name").strip().rstrip(":")
        bucket.append({"kind": "status", "name": status, "value": None, "raw": text})

def _norm_rewards(reward_list: List[Any]) -> List[Dict[str, Any]]:
    """
    Accepts:
      - old shape: {"type":"stat","name":"Speed","value":10}
      - API shapes:
          * single-key dict: {"speed":[5]}
          * multi-key dict: {"energy":[10],"speed":[5],"bond":[5]}
      - nested groups: [ [ {...}, {...} ], [ {...} ] ]
      - text markers: {"type":"text","text":"※ Bad result"} or plain strings
    Returns list of {kind, name, value, raw}
      kind ∈ {"stat","energy","skill_points","bond","hint","status","text","unknown"}
    """
    out: List[Dict[str, Any]] = []

    def push(kind, name=None, value=None, raw=None):
        out.append({"kind": kind, "name": name, "value": value, "raw": raw})

    for item in _flatten_rewards(reward_list):
        if isinstance(item, dict) and "type" in item:
            t = item.get("type")
            if t == "stat":
                name = item.get("name")
                val = item.get("value", 0)
                val = int(val) if isinstance(val, (int, float)) else 0
                lname = (name or "").lower()
                if lname in ("energy", "stamina"):
                    push("energy", "Energy", val, raw=item)
                elif lname in ("skill points", "skill point"):
                    push("skill_points", "Skill points", val, raw=item)
                elif lname in ("bond",):
                    push("bond", "Bond", val, raw=item)
                else:
                    push("stat", _canon_stat(name or "Unknown"), val, raw=item)
            elif t == "text":
                txt = str(item.get("text", ""))
                push("text", None, None, raw=txt)
                _extract_textual_hints_and_statuses(txt, out)
            else:
                push("unknown", None, None, raw=item)
            continue

        if isinstance(item, dict):
            for k, v in item.items():
                k_str = str(k)
                k_low = k_str.lower()
                amt = _first_number(v)
                if amt is None:
                    push("unknown", k_str, None, raw={k_str: v})
                    continue
                ival = int(amt)
                if k_low in ("energy", "stamina"):
                    push("energy", "Energy", ival, raw={k_str: v})
                elif k_low in ("skill points", "skill point"):
                    push("skill_points", "Skill points", ival, raw={k_str: v})
                elif k_low == "bond":
                    push("bond", "Bond", ival, raw={k_str: v})
                else:
                    push("stat", _canon_stat(k_str), ival, raw={k_str: v})
            continue

        if isinstance(item, str):
            push("text", None, None, raw=item)
            _extract_textual_hints_and_statuses(item, out)
            continue

        push("unknown", None, None, raw=item)

    return out

def _cap_decay(stat_name: str, add: int, ctx: Context) -> float:
    if not ctx.stat_caps or not ctx.current_stats:
        return float(add)
    cap = ctx.stat_caps.get(stat_name)
    cur = ctx.current_stats.get(stat_name)
    if cap is None or cur is None or add <= 0:
        return float(add)
    over = max(0, (cur + add) - cap)
    usable = add - over
    if over <= 0:
        return float(add)
    decay = math.exp(-SCORING["cap_decay_strength"] * over)
    return float(usable) + float(over) * decay

def _score_option(opt_name: str, rewards: List[Any], ctx: Context) -> Tuple[float, List[str]]:
    details: List[str] = []
    norm = _norm_rewards(rewards)
    score = 0.0

    prob = 1.0
    m = CHANCE_RX.search(opt_name)
    if m:
        pct = float(m.group("pct"))
        prob = max(0.0, min(1.0, pct / 100.0))

    has_good = any(r["kind"] == "text" and "good result" in str(r["raw"]).lower() for r in norm)
    has_bad  = any(r["kind"] == "text" and "bad result"  in str(r["raw"]).lower() for r in norm)
    if has_good:
        score += SCORING["good_result_bonus"]
        details.append(f"+{SCORING['good_result_bonus']:.1f} good-result bonus")
    if has_bad:
        penalty = SCORING["bad_result_penalty"]
        score += penalty
        details.append(f"{penalty:.1f} bad-result penalty")

    if ctx.hard_avoid_statuses:
        for r in norm:
            if r["kind"] == "status" and r["name"] in ctx.hard_avoid_statuses:
                details.append(f"-999 hard-avoid status: {r['name']}")
                return (-999.0, details)

    for r in norm:
        kind, name, val = r["kind"], r.get("name"), r.get("value")
        ev_mult = prob if prob is not None else SCORING["assume_missing_chance_as"]

        if kind == "energy" and isinstance(val, int):
            mult = SCORING["energy_point"]
            if ctx.current_energy is not None and ctx.max_energy is not None:
                if ctx.current_energy <= ctx.prefer_energy_below:
                    mult *= ctx.low_energy_multiplier
                    details.append(f"(energy low bias x{ctx.low_energy_multiplier:.2f})")
            delta = val * mult * ev_mult
            score += delta
            details.append(f"+{delta:.1f} Energy {val:+d}")

        elif kind == "skill_points" and isinstance(val, int):
            delta = val * SCORING["skill_points_point"] * ev_mult
            score += delta
            details.append(f"+{delta:.1f} Skill points {val:+d}")

        elif kind == "bond" and isinstance(val, int):
            delta = val * SCORING["bond_point"] * ev_mult
            score += delta
            details.append(f"+{delta:.1f} Bond {val:+d}")

        elif kind == "stat" and isinstance(val, int) and name:
            base = SCORING["stat_weights"].get(name, SCORING["stat_weights"]["_default"])
            adj = _cap_decay(name, val, ctx)
            if val < 0:
                delta = val * base * SCORING["negative_stat_penalty_mult"] * ev_mult
            else:
                delta = adj * base * ev_mult
            score += delta
            details.append(f"{'+' if delta>=0 else ''}{delta:.1f} {name} {val:+d}")

        elif kind == "status" and name:
            penalty = SCORING["debuff_penalties"].get(name, SCORING["debuff_penalties"]["_generic_status"])
            score += penalty * ev_mult
            details.append(f"{penalty:.1f} Status {name}")

    if ctx.avoid_bad_result and has_bad and score > -50:
        score += -2.0
        details.append("-2.0 extra avoid-bad nudge")

    return (score, details)

def get_optimal_choice(event_name: str, ctx: Context = DEFAULT_CONTEXT) -> Tuple[int, int]:
    api = fetch_event_by_name(event_name)
    if not api:
        print(f"[DEBUG] No API result for '{event_name}' — returning (0, 1)")
        return (0, 1)

    match_data = api["match"]
    name = match_data.get("event_name", event_name)
    data = match_data.get("data", {})
    options = data.get("options", {})

    if not isinstance(options, dict):
        if isinstance(options, list):
            options = {f"Option {i+1}": options[i] for i in range(len(options))}
        else:
            print(f"[DEBUG] Unsupported options format for '{name}': {type(options)}")
            return (0, 1)

    print(f"[DEBUG] Event: {name}")
    scored: List[Tuple[str, float, List[str]]] = []
    for idx, (opt_name, rewards) in enumerate(options.items(), start=1):
        s, detail = _score_option(opt_name, rewards, ctx)
        scored.append((opt_name, s, detail))

    for i, (opt_name, s, detail) in enumerate(scored, start=1):
        print(f"  Option {i}: {opt_name}")
        for line in detail:
            print(f"    {line}")
        print(f"    => total score: {s:.2f}")

    best_idx = max(range(len(scored)), key=lambda i: scored[i][1]) if scored else 0
    total_choices = len(scored)
    print(f"[DEBUG] Picked choice #{best_idx+1} of {total_choices}")
    return (total_choices, best_idx + 1)

def get_event_payload(event_name: str) -> Optional[Dict[str, Any]]:
    api = fetch_event_by_name(event_name)
    return api["match"] if api else None
