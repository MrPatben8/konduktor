"""`core.level_plan.plan_gain` — every outcome, the step rounding, the ceiling.

Pure: no files, no library. What Normalize's preview counts and what its run
writes both come from this function, so its rules are pinned here once.
"""
from __future__ import annotations

from konduktor.core.level_plan import STEP_DB, plan_gain, shifted
from konduktor.core.loudness import Loudness

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


def L(lufs, tp, rms=None):
    return Loudness(lufs=lufs, rms=rms if rms is not None else lufs + 2, true_peak=tp, lra=6.0, plr=tp - lufs)


def plan(loud, suffix=".flac", measure="lufs", target=-9.0, ceiling=-1.0):
    return plan_gain(loud, suffix, measure=measure, target=target, ceiling=ceiling)


print("== the basics ==")
check("one step is 2^(1/4) in amplitude, 1.505 dB", abs(STEP_DB - 1.50515) < 1e-4, str(STEP_DB))
s = plan(L(-6.0, 1.0))
check("a hot lossless track comes down exactly", (s.outcome, s.gain_db) == ("apply", -3.0), str(s))
s = plan(L(-12.0, -6.0))
check("a quiet one goes up exactly when the peak allows", (s.outcome, s.gain_db) == ("apply", 3.0), str(s))
s = plan(L(-9.02, -2.0))
check("within 0.05 dB is at target (lossless)", s.outcome == "at_target" and not s.writes, str(s))
check("no measurement: unmeasured", plan(None).outcome == "unmeasured")
check("ALAC / AAC / Ogg: unsupported", all(plan(L(-6, 0), x).outcome == "unsupported" for x in (".m4a", ".ogg", ".opus")))
check("the suffix is case-blind", plan(L(-6.0, 1.0), ".FLAC").outcome == "apply")

print("== the ceiling (a raise never clips) ==")
s = plan(L(-14.0, -3.0))
check("a raise stops at the true-peak ceiling and says how far short",
      (s.outcome, s.gain_db, s.short_by) == ("capped", 2.0, 3.0), str(s))
s = plan(L(-12.0, 0.5))
check("a peak already over the ceiling at/below target is left alone", (s.outcome, s.gain_db) == ("hot_peak_left", 0.0), str(s))
s = plan(L(-6.0, 3.0))
check("…but a HOT track over the ceiling still comes down", (s.outcome, s.gain_db) == ("apply", -3.0), str(s))

print("== MP3: lossless steps of 1.505 dB ==")
s = plan(L(-6.0, 1.0), ".mp3")
check("a lowering rounds to the nearest step (-3.0 -> -3.01)", abs(s.gain_db + 2 * STEP_DB) < 1e-6 and s.outcome == "apply", str(s))
s = plan(L(-8.4, -3.0), ".mp3")
check("under half a step from target is at target", s.outcome == "at_target", str(s))
s = plan(L(-12.0, -6.0), ".mp3")
check("a raise rounds to the nearest step when the peak allows (+3.0 -> +3.01)",
      abs(s.gain_db - 2 * STEP_DB) < 1e-6 and s.outcome == "apply", str(s))
s = plan(L(-12.0, -3.5), ".mp3")
check("a raise never rounds UP past the ceiling (headroom 2.5 -> one step)",
      abs(s.gain_db - STEP_DB) < 1e-6 and s.outcome == "capped", str(s))
s = plan(L(-12.0, -2.0), ".mp3")
check("headroom under one step: capped at zero, nothing written", s.outcome == "capped" and not s.writes, str(s))
check("every MP3 gain is a whole number of steps",
      all(abs(g / STEP_DB - round(g / STEP_DB)) < 1e-6
          for g in (plan(L(x, x + 5), ".mp3").gain_db for x in (-16, -13.3, -10.1, -7.7, -4.2))))

print("== the measure ==")
s = plan(L(-9.0, -6.0, rms=-12.0), measure="rms")
check("RMS measures the loud sections: -12 dB RMS goes up 3", (s.outcome, s.gain_db) == ("apply", 3.0), str(s))

print("== the level after ==")
after = shifted(L(-12.0, -6.0), 3.0)
check("LUFS, RMS and true peak move by the gain; LRA and PLR do not",
      (after.lufs, after.rms, after.true_peak, after.lra, after.plr) == (-9.0, -7.0, -3.0, 6.0, 6.0), str(after))

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
raise SystemExit(1 if failed else 0)
