#!/bin/bash
# 打包成 .app 与 .dmg（离线可用，不依赖网络）
# 产物：mac_bundle_packager/build/ESP32-S3 检修台.app
#       ~/Desktop/ESP32-S3 检修台.dmg
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENVPY="/Users/xiaoyi/.espressif/tools/python/v5.5.6/venv/bin/python"
APP_NAME="ESP32-S3 检修台"
BUILD="$ROOT/mac_bundle_packager/build"
BUNDLE="$BUILD/${APP_NAME}.app"

echo "== 1/6 清理 =="
rm -rf "$BUILD"
mkdir -p "$BUILD"

echo "== 2/6 PyInstaller 打包后台 =="
cd "$ROOT/backend"
"$VENVPY" -m PyInstaller \
  --noconfirm --clean --console \
  --name console_app --onedir \
  --paths "$ROOT/backend" \
  --hidden-import serial.tools.list_ports \
  --collect-all esptool \
  --hidden-import serial \
  --distpath "$BUILD/pyi" \
  --workpath "$BUILD/pyi_work" \
  --specpath "$BUILD/pyi_spec" \
  app.py > "$BUILD/pyinstaller.log" 2>&1 || {
    echo "PyInstaller 失败，日志末尾："; tail -30 "$BUILD/pyinstaller.log"; exit 1; }
echo "  后台二进制: $(du -sh "$BUILD/pyi/console_app" | cut -f1)"

echo "== 3/6 组装 .app 结构 =="
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cp -R "$BUILD/pyi/console_app/." "$BUNDLE/Contents/Resources/backend/"
cp -R "$ROOT/frontend-ui" "$BUNDLE/Contents/Resources/frontend-ui"
cp -R "$ROOT/esp32_components" "$BUNDLE/Contents/Resources/esp32_components"
cp "$ROOT/mac_bundle_packager/assets/AppIcon.icns" "$BUNDLE/Contents/Resources/AppIcon.icns"
# 首次启动用的备份种子：只带索引 + 索引里登记的整片镜像（跳过 .chunk*.tmp 残片）
SEED="$BUNDLE/Contents/Resources/backups"
if [ -f "$ROOT/backups/index.json" ]; then
  mkdir -p "$SEED"
  cp "$ROOT/backups/index.json" "$SEED/"
  /usr/bin/python3 - "$ROOT/backups/index.json" "$ROOT/backups" "$SEED" <<'PYSEED'
import json, shutil, sys, pathlib
index_path, src, dst = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
boards = json.loads(pathlib.Path(index_path).read_text(encoding="utf-8")).get("boards", {})
for e in boards.values():
    p = src / pathlib.Path(e.get("file", "")).name
    if p.exists() and p.stat().st_size > 0:
        shutil.copy2(p, dst / p.name)
PYSEED
  echo "  备份种子: $(du -sh "$SEED" | cut -f1)"
else
  echo "  无历史备份，跳过种子"
fi

echo "== 4/6 编译原生窗口壳 =="
xcrun swiftc -O "$ROOT/mac_bundle_packager/ConsoleShell.swift" \
  -o "$BUNDLE/Contents/MacOS/console_shell" \
  -framework Cocoa -framework WebKit

echo "== 5/6 Info.plist 与签名 =="
cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>${APP_NAME}</string>
  <key>CFBundleDisplayName</key><string>${APP_NAME}</string>
  <key>CFBundleIdentifier</key><string>com.xiaoyi.esp32s3console</string>
  <key>CFBundleVersion</key><string>1.0.0</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>console_shell</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

xattr -cr "$BUNDLE" 2>/dev/null || true
codesign --force --deep --sign - "$BUNDLE" >/dev/null 2>&1 || echo "  跳过签名（本机自用可运行）"
echo "  .app 大小: $(du -sh "$BUNDLE" | cut -f1)"

echo "== 6/6 生成 dmg =="
rm -f "$HOME/Desktop/${APP_NAME}.dmg"
hdiutil create -volname "$APP_NAME" -srcfolder "$BUNDLE" \
  -ov -format UDZO "$HOME/Desktop/${APP_NAME}.dmg" > "$BUILD/hdiutil.log" 2>&1
echo "  dmg: $HOME/Desktop/${APP_NAME}.dmg ($(du -h "$HOME/Desktop/${APP_NAME}.dmg" | cut -f1))"
echo "完成"
