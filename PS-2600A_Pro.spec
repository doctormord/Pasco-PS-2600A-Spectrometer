# -*- mode: python ; coding: utf-8 -*-
# Desktop app + embedded remote server ("Remote access") + headless mode
# (PS-2600A_Pro.exe --headless). The API group modules are imported
# dynamically (api_v1.GROUPS) and uvicorn picks its loop/protocol
# implementations at runtime, so both are listed as hidden imports.

hiddenimports = [
    # shared core / API (dynamic imports)
    'engine', 'api', 'api_v1', 'api_models', 'api_v1_analysis',
    'api_v1_calibration', 'api_v1_display', 'api_v1_wavelength',
    'server_runner', 'gui_remote', 'gui_help', 'version',
    'wavelength_calibration', 'export_paths', 'autosave', 'calibration_report',
    'calibration_workflow', 'calibration_capture', 'reference_sources',
    # device backends (registered at runtime)
    '_device_pasco', '_device_demo', '_device_lr2t', '_device_ocean',
    # uvicorn runtime selections
    'uvicorn.logging',
    'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
    'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.http.httptools_impl',
    'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
    'uvicorn.protocols.websockets.websockets_impl',
    'uvicorn.protocols.websockets.wsproto_impl',
    'uvicorn.lifespan', 'uvicorn.lifespan.on', 'uvicorn.lifespan.off',
    'websockets', 'h11',
    # FastAPI / pydantic
    'fastapi', 'starlette', 'pydantic', 'anyio._backends._asyncio',
    # reports (matplotlib Agg backend, lazily imported)
    'matplotlib.backends.backend_agg', 'matplotlib.backends.backend_pdf',
]

a = Analysis(
    ['PS-2600A_Pro.py'],
    pathex=[],
    binaries=[],
    datas=[('static', 'static')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PS-2600A_Pro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
