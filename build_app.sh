#!/bin/sh
# Собирает «Погодный планировщик.app» в /Applications: ./build_app.sh
# Пересборка безопасна: данные лежат в ~/Library/Application Support/Погодный планировщик.
set -e
cd "$(dirname "$0")"
SRC="$(pwd)"
APP="/Applications/Погодный планировщик.app"
PY="$(python3 -c 'import sys; print(sys.base_prefix)')"
# Берём сам бинарник Python.app: тогда в Dock будет наш значок и имя, а не «Python».
PYBIN="$PY/Resources/Python.app/Contents/MacOS/Python"
[ -x "$PYBIN" ] || { echo "Нужен Python с python.org (framework build): $PYBIN не найден"; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"
cp "$PYBIN" "$APP/Contents/MacOS/python"
cp server.py scheduler.py ical.py app.py "$APP/Contents/Resources/app/"
cp -R static "$APP/Contents/Resources/app/"
python3 -m pip install -q --target "$APP/Contents/Resources/site-packages" -r requirements.txt pywebview

cat > "$APP/Contents/MacOS/planner" <<EOF
#!/bin/sh
R="\$(dirname "\$0")/../Resources"
export PYTHONPATH="\$R/site-packages" PYTHONDONTWRITEBYTECODE=1 PLANNER_IMPORT_DB="$SRC/data.sqlite3"
exec "\$(dirname "\$0")/python" "\$R/app/app.py"
EOF
chmod +x "$APP/Contents/MacOS/planner"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Погодный планировщик</string>
  <key>CFBundleDisplayName</key><string>Погодный планировщик</string>
  <key>CFBundleIdentifier</key><string>local.weather-planner</string>
  <key>CFBundleExecutable</key><string>planner</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF

# Значок: рендерим icon.svg через Quick Look и собираем .icns.
T="$(mktemp -d)"
qlmanage -t -s 1024 -o "$T" icon.svg >/dev/null 2>&1
mkdir "$T/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$T/icon.svg.png" --out "$T/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$T/icon.svg.png" --out "$T/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$T/icon.iconset" -o "$APP/Contents/Resources/icon.icns"
rm -rf "$T"

# Подпись «для себя»: без неё macOS убивает скопированный в бандл Python.
codesign --force --deep -s - "$APP" >/dev/null 2>&1
touch "$APP"
echo "Готово: $APP"
