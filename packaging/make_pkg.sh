#!/bin/bash
# 쇼파일 생성기 — 소스에서 PyInstaller 빌드 → 서명·공증 .pkg (메모리 macos-packaging-pipeline 절차)
#   ./packaging/make_pkg.sh            → dist/ShowfileGenerator-v<VERSION>.pkg
# 번들·실행파일명은 ASCII만 (한글이면 codesign 시일·pkgbuild가 깨짐). 한글 이름은 CFBundleDisplayName으로만.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

VERSION=$(tr -d '[:space:]' < app/VERSION)
APPNAME="Showfile Generator"
EXE="showfilegen"
BUNDLE_ID="com.audioaz.showfile"
SIGN_APP="Developer ID Application: hyungjun Kim (92PGKJFTU9)"
SIGN_PKG="Developer ID Installer: hyungjun Kim (92PGKJFTU9)"
NOTARY_PROFILE="AZ_NOTARY"
OUT="$REPO/dist/ShowfileGenerator-v${VERSION}.pkg"
B=/tmp/sfbuild

# 미서명 바이너리가 리소스에 섞이면 공증 실패 — 옛 오프라인 앱 zip 등이 base/에 있으면 중단
if find app/base \( -name "*.zip" -o -name "*.app" -o -name "*.pkg" \) | grep -q .; then
  echo "app/base 안에 zip/app/pkg가 있습니다 — 제거 후 다시 실행"; exit 1
fi

rm -rf "$B"; mkdir -p "$B/root" "$B/scripts" "$B/res" "$REPO/dist"

echo "── PyInstaller 빌드 (v$VERSION)"
( cd app && python3 -m PyInstaller --noconfirm --clean --windowed --name "$EXE" \
    --osx-bundle-identifier "$BUNDLE_ID" --icon "$REPO/appicon.icns" \
    --distpath "$B/dist" --workpath "$B/work" --specpath "$B" --paths "$REPO/app" \
    --add-data "$REPO/app/base:base" --add-data "$REPO/app/naming_vocab.json:." --add-data "$REPO/app/VERSION:." \
    --collect-all numbers_parser --collect-all openpyxl \
    --hidden-import sheet2spec --hidden-import dm7_gen --hidden-import klang_gen \
    --hidden-import sprk_gen --hidden-import x32_gen --hidden-import run_pipeline \
    app_server.py > "$B/pyinstaller.log" 2>&1 ) || { tail -30 "$B/pyinstaller.log"; exit 1; }

APP="$B/root/$APPNAME.app"
ditto "$B/dist/$EXE.app" "$APP"
PL="$APP/Contents/Info.plist"
pb() { /usr/libexec/PlistBuddy -c "$1" "$PL" 2>/dev/null || true; }
pb "Delete :CFBundleDisplayName";         pb "Add :CFBundleDisplayName string 쇼파일 생성기"
pb "Delete :CFBundleShortVersionString";  pb "Add :CFBundleShortVersionString string $VERSION"
pb "Delete :CFBundleVersion";             pb "Add :CFBundleVersion string $VERSION"
pb "Delete :LSMinimumSystemVersion";      pb "Add :LSMinimumSystemVersion string 11.0"
pb "Delete :LSUIElement";                 pb "Add :LSUIElement bool true"   # 창 없는 서버 앱 — 독 아이콘 무한 바운스 방지
cp LICENSE.txt TERMS.md "$APP/Contents/Resources/" 2>/dev/null || true

xattr -cr "$APP"
find "$APP" -name "*.cstemp*" -delete 2>/dev/null || true
for fw in "$APP"/Contents/Frameworks/Python*.framework; do
  [ -d "$fw/Versions" ] || continue
  [ -e "$fw/Versions/Current" ] || { v=$(ls "$fw/Versions" | head -1); ln -sfh "$v" "$fw/Versions/Current"; }
done

sign() {  # 타임스탬프 서버 간헐 실패 → 재시도
  local n
  for n in 1 2 3 4 5; do
    codesign --force --options runtime --timestamp --sign "$SIGN_APP" "$@" 2>/dev/null && return 0
    find "$APP" -name "*.cstemp*" -delete 2>/dev/null || true
    sleep 3
  done
  echo "서명 실패: ${*: -1}"; return 1
}

echo "── 내부 바이너리 서명"
N=0
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then sign "$f"; N=$((N+1)); fi
done < <(find "$APP/Contents" -type f ! -path "*/MacOS/$EXE" -print0)
echo "  $N 개"
for fw in "$APP"/Contents/Frameworks/Python*.framework; do [ -d "$fw" ] && sign "$fw"; done

echo "── 앱 서명"
sign --entitlements packaging/entitlements.plist "$APP"
codesign --verify --deep --strict "$APP" && echo "  서명 유효"

echo "── 앱 공증"
ditto -c -k --keepParent "$APP" "$B/app.zip"
xcrun notarytool submit "$B/app.zip" --keychain-profile "$NOTARY_PROFILE" --wait | tee "$B/notary_app.log"
grep -q "status: Accepted" "$B/notary_app.log" || { echo "앱 공증 실패 — xcrun notarytool log <id> --keychain-profile $NOTARY_PROFILE"; exit 1; }
for i in 1 2 3; do xcrun stapler staple "$APP" && break; echo "  티켓 전파 대기 ($i/3)"; sleep 20; done

cat > "$B/scripts/postinstall" <<'POST'
#!/bin/bash
# /Applications 만 본다 (데스크탑·다운로드·드라이브를 건드리면 설치 프로그램 폴더 접근 허용 창이 뜸)
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "/Applications/Showfile Generator.app" 2>/dev/null || true
exit 0
POST
chmod +x "$B/scripts/postinstall"

cat > "$B/component.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><array><dict>
  <key>BundleHasStrictIdentifier</key><true/>
  <key>BundleIsRelocatable</key><false/>
  <key>BundleIsVersionChecked</key><false/>
  <key>BundleOverwriteAction</key><string>upgrade</string>
  <key>RootRelativeBundlePath</key><string>$APPNAME.app</string>
</dict></array></plist>
PLIST

echo "── pkg 만들기"
pkgbuild --root "$B/root" --component-plist "$B/component.plist" \
  --identifier "$BUNDLE_ID" --version "$VERSION" \
  --install-location /Applications --scripts "$B/scripts" "$B/raw.pkg"
cp LICENSE.txt "$B/res/LICENSE.txt"
productbuild --synthesize --package "$B/raw.pkg" "$B/dist.xml"
python3 - "$B/dist.xml" <<'PY'
import sys
p = sys.argv[1]; s = open(p, encoding='utf-8').read()
s = s.replace('<pkg-ref', '<title>Showfile Generator</title>\n    <license file="LICENSE.txt"/>\n    <pkg-ref', 1)
open(p, 'w', encoding='utf-8').write(s)
PY
productbuild --distribution "$B/dist.xml" --resources "$B/res" --package-path "$B" "$B/unsigned.pkg"
productsign --sign "$SIGN_PKG" "$B/unsigned.pkg" "$OUT"

echo "── pkg 공증"
xcrun notarytool submit "$OUT" --keychain-profile "$NOTARY_PROFILE" --wait | tee "$B/notary_pkg.log"
grep -q "status: Accepted" "$B/notary_pkg.log" || { echo "pkg 공증 실패"; exit 1; }
for i in 1 2 3; do xcrun stapler staple "$OUT" && break; echo "  티켓 전파 대기 ($i/3)"; sleep 20; done
spctl -a -vvv -t install "$OUT" 2>&1 | head -3

# 옛 버전 pkg는 남기지 않는다 (과거 버전은 GitHub Releases에 있음)
for f in "$REPO"/dist/ShowfileGenerator-v*.pkg; do
  [ -e "$f" ] || continue; [ "$f" = "$OUT" ] && continue; rm -f "$f" && echo "  옛 버전 삭제: $f"
done
echo "완료: $OUT ($(du -h "$OUT" | cut -f1))"
echo "배포: ~/Projects/AudioAZ/publish-installer.sh showfile-v$VERSION \"$OUT\" → app_server.py OFFLINE_PKG_VERSION 갱신"
