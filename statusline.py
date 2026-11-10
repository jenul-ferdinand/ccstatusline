#!/usr/bin/env python3
"""Claude Code status line: compact, coloured, single-line display.
Reads the status JSON on stdin, prints one line.

The 5h segment projects the burn rate from an EWMA of the recent consumption
rate (used-%/hour), measured across the gap between renders. The code stores each
sample under `~/.cache` (machine-local, so a synced ~/.claude never carries it)
and creates it on first run. If that file can't be read or written, the code uses
the since-window-open average instead, so you can copy this script to another
machine as-is."""

import contextlib
import json
import os
import sys
from datetime import datetime

WINDOW_S = 5 * 3600  # length of the 5-hour rate-limit window
HALF_LIFE_S = 1800.0  # burn-rate EWMA half-life, ~30 min (raise = steadier)
STATE_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "claude-statusline",
    "state.json",
)

# --- formatting helpers -----------------------------------------------------


def paint(s, code):
    return f"\033[{code}m{s}\033[0m"


def health(remaining):
    """Colour by headroom: green >=50, yellow >=20, red below."""
    return "32" if remaining >= 50 else "33" if remaining >= 20 else "31"


def reset_label(resets_at):
    """Reset time as a compact clock: '6:00pm' -> '6pm', '5:58pm' stays."""
    try:
        s = datetime.fromtimestamp(resets_at).strftime("%I:%M%p").lower().lstrip("0")
        return s.replace(":00", "")
    except Exception:
        return None


def fmt_dur(secs):
    """Compact countdown: 6300 -> '1h45m', 2700 -> '45m', 40 -> '<1m'."""
    secs = max(0, int(secs))
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h{m}m" if h and m else f"{h}h" if h else f"{m}m" if m else "<1m"


def short_path(p, limit=30):
    """Long paths only: drop '~/repos/', then collapse the middle, keeping the
    project (or the first two parts elsewhere) and the last two folders."""
    if len(p) <= limit:
        return p
    keep = 2
    if p.startswith("~/repos/"):
        p, keep = p[len("~/repos/") :], 1
    parts = p.split("/")
    if len(parts) > keep + 2:
        parts = parts[:keep] + ["…"] + parts[-2:]
    return "/".join(parts)


def check_short_path():
    """From ~/.claude: python3 -B -c 'import statusline as s; s.check_short_path()'"""
    assert short_path("~/repos/webapp") == "~/repos/webapp"
    assert short_path("~/repos/webapp/packages/forms/src") == "webapp/…/forms/src"
    assert short_path("/mnt/c/Users/me/AppData/Local/Temp") == "/mnt/…/Local/Temp"
    assert (
        short_path("~/repos/a-very-long-project-name/a-long-folder")
        == "a-very-long-project-name/a-long-folder"
    )


# --- burn-rate state (persisted between renders) ----------------------------


def load_state():
    """Read the last sample. Returns None on any error instead of raising."""
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state):
    """Atomic write via temp file plus rename. Creates the cache dir and file on
    first call. Does nothing if the directory isn't writable, so a bad state file
    can't break the status line."""
    tmp = f"{STATE_PATH}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def burn_rate(used, resets_at, now, state):
    """EWMA of the recent consumption rate in used-%/hour, from the change since
    the last render. Returns (rate, new_state). rate stays None until a
    trustworthy sample exists, so the caller uses the since-open average.

    The 5h window is account-wide, so concurrent sessions share this file. That's
    safe: used_percentage is global and the atomic write prevents corruption."""
    fresh = {"resets_at": resets_at, "t": now, "used": used, "rate": None}
    # Treat as a new window only on an unmistakable reset: resets_at jumps far
    # (real resets move ~5h) or usage drops to near zero. Small jitter or dips
    # must not wipe the accumulated rate; usage dropping to ~0 stays the anchor.
    if (
        not state
        or abs(state.get("resets_at", 0) - resets_at) > 300
        or used < state.get("used", 0) - 5
    ):
        return None, fresh
    dt = now - state.get("t", now)
    if dt < 1:  # same-second (or clock-skewed) re-render: hold
        return state.get("rate"), state
    prev = state.get("rate")
    if prev is None:
        # First trustworthy sample: seed from the smooth since-open average so a
        # 1-second render gap doesn't blow the rate up. Under 3 min in there's
        # no reliable interval yet, so keep sampling.
        rate = avg_rate(used, resets_at, now)
    else:
        # clamp so a usage dip counts as idle
        inst = max(0.0, (used - state["used"]) / (dt / 3600.0))
        alpha = 1 - 0.5 ** (dt / HALF_LIFE_S)  # dt-aware: longer gaps weigh more
        rate = alpha * inst + (1 - alpha) * prev
    fresh["rate"] = rate
    return rate, fresh


# --- projection to the reset ------------------------------------------------


def avg_rate(used, resets_at, now):
    """Average %/hour since the window opened; None if <3 min in or window over."""
    elapsed_h = (now - (resets_at - WINDOW_S)) / 3600.0
    return used / elapsed_h if 0.05 <= elapsed_h < WINDOW_S / 3600 else None


def project_left(used, rate, resets_at, now):
    """(% left at reset, seconds-until-0% | None) projected at `rate` %/hour.
    eta is set only when you're on pace to hit 0% before the reset."""
    proj_left = round(100 - (used + rate * (resets_at - now) / 3600.0))
    eta = (100 - used) / rate * 3600 if rate > 0 and proj_left < 0 else None
    return proj_left, eta


def proj_tail(proj_left, eta_secs, label):
    """Paint the '>...' projection/ETA tail of the 5h segment."""
    if proj_left < 0:  # on pace to hit 0% before the reset
        when = fmt_dur(eta_secs) if eta_secs is not None else label
        return paint(f">0%>{when}" if when else ">0%", "31")
    col = "33" if proj_left < 15 else "90"  # yellow if you'll cut it close
    return paint(f">{proj_left}%>{label}" if label else f">{proj_left}%", col)


# --- main -------------------------------------------------------------------


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        print("")
        return

    parts = []

    # 1. Working directory, cyan (home shown as ~, forward slashes)
    cwd = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or "?"
    cwd = cwd.replace("\\", "/").rstrip("/") or cwd
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if home and (cwd.lower() + "/").startswith(home.lower() + "/"):
        cwd = "~" + cwd[len(home) :]
    parts.append(paint(short_path(cwd), "96"))

    # 2. Model family as one letter to save space, magenta.
    # Opus->O, Sonnet->S, Haiku->H, Fable->F; else the display name's initial.
    name = (d.get("model") or {}).get("display_name") or "?"
    families = ("Opus", "Sonnet", "Haiku", "Fable")
    model = next(
        (f[0] for f in families if f.lower() in name.lower()), name[:1].upper()
    )
    level = (d.get("effort") or {}).get("level")
    thinking_on = (d.get("thinking") or {}).get("enabled")
    if level and thinking_on is not False:
        model += f" {level}"
    seg = paint(model, "95")
    if d.get("fast_mode"):
        seg = paint("⚡", "93") + seg
    parts.append(seg)

    # 3. Context remaining, health-coloured
    ctx = (d.get("context_window") or {}).get("remaining_percentage")
    if ctx is not None:
        parts.append(paint(f"ctx {round(ctx)}%", health(ctx)))

    rl = d.get("rate_limits") or {}

    # 4. 5-hour window: % left + burn-rate projection to the reset
    five = rl.get("five_hour") or {}
    if five.get("used_percentage") is not None:
        used = five["used_percentage"]
        left = round(100 - used)
        seg = paint(f"5h {left}%", health(left))
        resets = five.get("resets_at")
        if resets:
            label = reset_label(resets)
            now = datetime.now().timestamp()
            state = load_state()
            rate, new_state = burn_rate(used, resets, now, state)
            if new_state is not state:  # skip redundant writes on same-second renders
                save_state(new_state)
            measured = rate is not None  # a measured rate gets a '·%/h' tag
            if not measured:  # none yet -> coarse since-open average, untagged
                rate = avg_rate(used, resets, now)
            if rate is not None:
                proj_left, eta = project_left(used, rate, resets, now)
                tag = paint(f"·{round(rate)}%/h", "90") if measured else ""
                seg += " " + tag + proj_tail(proj_left, eta, label)
            elif label:
                seg += " " + paint(f"resets {label}", "90")
        parts.append(seg)

    # 5. 7-day window: % left, health-coloured
    week = rl.get("seven_day") or {}
    if week.get("used_percentage") is not None:
        left = round(100 - week["used_percentage"])
        parts.append(paint(f"7d {left}%", health(left)))

    # 6. Full session id (for `claude --resume`), grey
    sid = d.get("session_id")
    if sid:
        parts.append(paint(sid, "90"))

    sys.stdout.write(paint(" │ ", "90").join(parts))


if __name__ == "__main__":
    main()
