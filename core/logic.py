import json

from core.state import check_current_year, stat_state, check_energy

with open("config.json", "r", encoding="utf-8") as file:
  config = json.load(file)

PRIORITY_STAT = config["priority_stat"]
MAX_FAILURE = config["maximum_failure"]
STAT_CAPS = config["stat_caps"]
ENERGY_REST_THRESHOLD = config.get("energy_rest_threshold", 30)

# Get priority stat from config
def get_stat_priority(stat_key: str) -> int:
  return PRIORITY_STAT.index(stat_key) if stat_key in PRIORITY_STAT else 999

# Will do train with the most support card
# Used in the first year (aim for rainbow)
def most_support_card(results):
  # Seperate wit
  wit_data = results.get("wit")

  # Get all training but wit
  non_wit_results = {
    k: v for k, v in results.items()
    if k != "wit" and int(v["failure"]) <= MAX_FAILURE
  }

  # Check if train is bad
  all_others_bad = len(non_wit_results) == 0

  if all_others_bad and wit_data and int(wit_data["failure"]) <= MAX_FAILURE and wit_data["total_support"] >= 2:
    print("\n[INFO] All trainings are unsafe, but WIT is safe and has enough support cards.")
    return "wit"

  filtered_results = {
    k: v for k, v in results.items() if int(v["failure"]) <= MAX_FAILURE
  }

  if not filtered_results:
    print("\n[INFO] No safe training found. All failure chances are too high.")
    return None

  # Best training
  best_training = max(
    filtered_results.items(),
    key=lambda x: (
      x[1]["total_support"],
      -get_stat_priority(x[0])  # priority decides when supports are equal
    )
  )

  best_key, best_data = best_training

  if best_data["total_support"] <= 1:
    if int(best_data["failure"]) == 0:
      # WIT must be at least 2 support cards
      if best_key == "wit":
        print(f"\n[INFO] Only 1 support and it's WIT. Skipping.")
        return None
      print(f"\n[INFO] Only 1 support but 0% failure. Prioritizing based on priority list: {best_key.upper()}")
      return best_key
    else:
      print("\n[INFO] Low value training (only 1 support). Choosing to rest.")
      return None

  print(f"\nBest training: {best_key.upper()} with {best_data['total_support']} support cards and {best_data['failure']}% fail chance")
  return best_key

# Do rainbow training
def rainbow_training(results):
  # Get rainbow training
  rainbow_candidates = {
    stat: data for stat, data in results.items()
    if int(data["failure"]) <= MAX_FAILURE and data["support"].get(stat, 0) > 0
  }

  if not rainbow_candidates:
    print("\n[INFO] No rainbow training found under failure threshold.")
    return None

  # Find support card rainbow in training
  best_rainbow = max(
    rainbow_candidates.items(),
    key=lambda x: (
      x[1]["support"].get(x[0], 0),
      -get_stat_priority(x[0])
    )
  )

  best_key, best_data = best_rainbow
  print(f"\n[INFO] Rainbow training selected: {best_key.upper()} with {best_data['support'][best_key]} rainbow supports and {best_data['failure']}% fail chance")
  return best_key

def filter_by_stat_caps(results, current_stats):
  return {
    stat: data for stat, data in results.items()
    if current_stats.get(stat, 0) < STAT_CAPS.get(stat, 1200)
  }
  
def _normalize_failures_for_low_energy(
    results_training: dict,
    current_energy_percent: int,
    *,
    max_safe_fail: int,
    low_energy_trigger: int,
    exclude_keys: tuple = ("wit",),
) -> dict:
    if results_training is None or not isinstance(results_training, dict):
        return results_training

    if current_energy_percent is None or current_energy_percent > low_energy_trigger:
        return results_training

    # Collect non-excluded failure percents
    pairs = []
    for k, v in results_training.items():
        if k in exclude_keys:
            continue
        fail = v.get("failure")
        if isinstance(fail, (int, float)) and fail >= 0:
            pairs.append((k, int(fail)))

    if not pairs:
        return results_training

    # How many are above threshold?
    above = [(k, f) for (k, f) in pairs if f > max_safe_fail]
    below = [(k, f) for (k, f) in pairs if f <= max_safe_fail]

    # Require a "majority high" signal before normalizing
    if len(above) >= max(2, len(pairs) // 2 + 1) and below:
        hi_max = max(f for _, f in above)
        # Bump low outliers to hi_max
        for k, _ in below:
            old = results_training[k]["failure"]
            results_training[k]["failure"] = hi_max
            results_training[k]["_adjusted_false_scan"] = True
        # Optional: log what we did
        print(
            f"[INFO] Low energy ({current_energy_percent}%). "
            f"Detected false-scan outliers → bumped {', '.join(k for k,_ in below)} to {hi_max}%."
        )

    return results_training

def _is_early_or_late_june() -> bool:
    try:
        # If you already have something like these in core.state, great:
        from core.state import current_month, current_week_in_month  # type: ignore[attr-defined]
        month = current_month()        # e.g. "June" or 6
        week = current_week_in_month() # 1..4/5
        # Normalize month
        m = str(month).lower()
        is_june = (m == "june" or m == "jun" or m == "6")
        if not is_june:
            # also accept numeric
            try:
                is_june = int(month) == 6
            except Exception:
                pass
        if not is_june:
            return False
        # "early" (weeks 1–2) or "late" (week 4+)
        return (1 <= int(week) <= 2) or (int(week) >= 4)
    except Exception:
        # If we can't read month/week, stay conservative.
        return False

def _find_safe_double_rainbow(results: dict) -> str | None:
    best = None
    best_tuple = (-1, 999)  # (rainbow_count, -priority) to pick sensibly
    for stat, data in results.items():
        fail = int(data["failure"])
        if fail > MAX_FAILURE:
            continue
        rainbow_count = data["support"].get(stat, 0)
        if rainbow_count >= 2:
            tup = (rainbow_count, -get_stat_priority(stat))
            if tup > best_tuple:
                best_tuple = tup
                best = stat
    return best

# Decide training
def do_something(results):
  year = check_current_year()
  current_stats = stat_state()
  energy = check_energy()
  print(f"Current stats: {current_stats}")
  print(f"Current energy: {energy}%")

  # Existing low-energy false-scan guard (keeps your behavior)
  results = _normalize_failures_for_low_energy(
      results_training=results,
      current_energy_percent=energy,
      max_safe_fail=MAX_FAILURE,
      low_energy_trigger=ENERGY_REST_THRESHOLD,
      exclude_keys=("wit",)
  )
  for k, v in results.items():
      if v.get("_adjusted_false_scan"):
          print(f"[DEBUG] Adjusted {k.upper()} fail% to {v['failure']}% (false-scan guard)")

  filtered = filter_by_stat_caps(results, current_stats)

  if not filtered:
    print("[INFO] All stats capped or no valid training.")
    return None

  # June energy bias
  if _is_early_or_late_june() and energy < max(ENERGY_REST_THRESHOLD, 60):
    # Try to allow ONLY >=2 rainbow (safe) during this window
    pick = _find_safe_double_rainbow(filtered)
    if pick:
      print(f"[INFO] June energy bias active → allowing DOUBLE RAINBOW ({pick.upper()}) "
            f"with {filtered[pick]['support'].get(pick,0)} rainbow supports and "
            f"{filtered[pick]['failure']}% fail.")
      return pick
    else:
      print("[INFO] June energy bias active → no safe ≥2-rainbow found. Resting to build energy.")
      return None

  if "Junior Year" in year:
    return most_support_card(filtered)
  else:
    result = rainbow_training(filtered)
    if result is None:
      print("[INFO] Falling back to most_support_card because rainbow not available.")
      return most_support_card(filtered)
  return result