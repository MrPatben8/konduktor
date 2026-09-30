"""Convert to Stems: everything outside the stem FILE itself.

`core/stem_file.py` writes a stem file from a separation callback; the Traktor
adapter swaps it into the collection (`apply_stem_swaps`). This package is what
sits around them: the downloadable engine and weights (`engine_manager`), the
engine process (`engine_process`), and — next — the batch and the parked-
originals lifecycle.
"""
