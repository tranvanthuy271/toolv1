# Weapon Sprite Adapter

Desktop tool with several image utilities:
- Resize by Template: remove background from a source image, then resize it to match the visible bounds of up to 6 template images without rotating it.
- Client Scale Export: treat the input as a client x4 asset, decrypt `.dnd` when needed, then export PNG variants for `x4`, `x3`, `x2`, and `x1` while keeping the original filename stem.
- Remove Background: strip a flat background from a single image and export a transparent PNG.
- Encrypt / Decrypt PNG: reverse the first 51 bytes used by the client image format and keep encrypted outputs on the `.png` extension.

## Run from source

```powershell
cd C:\Nro\LangLa\Tools\RenderImage
python -m pip install -r requirements.txt
python app.py
```

## Build EXE

```powershell
cd C:\Nro\LangLa\Tools\RenderImage
python -m pip install -r requirements-build.txt
pyinstaller --noconsole --clean --onefile --collect-all tkinterdnd2 --name WeaponSpriteAdapter app.py
```

Or double-click `build_exe.bat`.

Output executable:

```text
dist\WeaponSpriteAdapter.exe
```

## Notes

- Source run saves outputs into `outputs\`
- EXE run saves outputs into `Documents\WeaponSpriteAdapter\outputs\`
- `Auto remove background` helps when the uploaded image has a flat background
- `Resize by Template` now keeps the template image filename stem for exported PNGs instead of renaming them to `slot_*`
- `Resize by Template` now lets you choose `Folder rieng` (timestamp batch folder) or `Folder chung` (`outputs/resize_template_common`)
- Desktop app now includes a separate `Remove Background` tab for background-only export
- `Client X4 -> X1` exports `x4/x3/x2/x1` PNG folders and accepts both `.png` and client-encrypted `.dnd` inputs
- `Client X4 -> X1` now lets you choose `Folder rieng` (`x4/x3/x2/x1`) or `Folder chung` (`all_sizes` with `_x4/_x3/_x2/_x1` suffixes)
- Desktop app supports click-to-upload and drag-and-drop when `tkinterdnd2` is installed
- `Encrypt / Decrypt PNG` now writes encrypted copies with the original `.png` extension into `outputs/encrypt_decrypt/encrypted`
