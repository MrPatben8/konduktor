# PyInstaller spec for the frozen stem engine. ONEDIR, not onefile: a onefile
# build re-extracts ~500 MB of torch into a temp folder on EVERY launch; the
# folder is instead unpacked once, into app-data, by Konduktor's engine manager.
#
#   .venv/bin/python -m PyInstaller --noconfirm --clean konduktor-engine.spec
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("demucs")

a = Analysis(
    ["engine_main.py"],
    pathex=["."],
    hiddenimports=hidden,
    excludes=[
        # Never used for inference, and large.
        "tkinter", "matplotlib", "IPython", "notebook", "tensorboard",
        "torchvision", "torchaudio", "PIL",
        # The engine never goes online: Konduktor downloads the weights.
        "huggingface_hub", "hf_xet",
    ],
)
# torch ships C++ headers (tens of MB, thousands of files) that only matter for
# building extensions; they are also the deepest paths in the tree, which is
# what trips Windows' 260-character limit.
a.datas = [d for d in a.datas if "/include/" not in d[0].replace("\\", "/")
           and not d[0].replace("\\", "/").startswith("torch/include")]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="konduktor-engine",
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="konduktor-engine", upx=False)
