"""Konduktor's stem engine: Demucs htdemucs_ft behind a small process protocol.

A separate program from Konduktor's backend — it carries torch, which the
backend deliberately does not — built and released on its own channel
(`engine-v*`) and downloaded on first use. It imports nothing from `konduktor`.
"""

#: Bumped whenever the engine's behaviour or protocol changes; the app names the
#: engine version it needs.
VERSION = "1.0.0"
