# PyInstaller spec. Build with: python -m PyInstaller clipper.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files

datas = [("clipper.ico", ".")]
datas += collect_data_files("customtkinter")
datas += collect_data_files("soundcard")

a = Analysis(
    ["clipper.pyw"],
    datas=datas,
    hiddenimports=["pystray._win32", "win32com.shell", "win32com.propsys"],
    excludes=["matplotlib", "scipy", "pandas", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Clipper",
    icon="clipper.ico",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Clipper", upx=False)
