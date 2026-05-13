# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ARAM-collector.exe

Build with:
    pyinstaller ARAM-collector.spec
"""
from pathlib import Path

src_path = str(Path("src").resolve())

a = Analysis(
    ["lcu_collector.py"],
    pathex=[str(Path(".").resolve()), src_path],
    binaries=[],
    datas=[],
    hiddenimports=[
        "aram_nn",
        "aram_nn.lcu",
        "aram_nn.lcu.client",
        "aram_nn.lcu.process",
        "aram_nn.lcu.poller",
        "aram_nn.lcu.snowball",
        "polars",
        "polars._utils",
        "click",
        "httpx",
        "psutil",
        "psutil._pswindows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "test", "xmlrpc",
        "torch", "torchvision", "torchaudio",
        "numpy", "scipy", "matplotlib", "pandas",
        "PIL", "cv2", "sklearn", "tensorflow",
        "IPython", "jupyter", "notebook",
        "email", "html", "http.server", "urllib.robotparser",
        "xml", "pdb", "doctest", "difflib",
        "multiprocessing.popen_spawn_win32",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ARAM-collector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
