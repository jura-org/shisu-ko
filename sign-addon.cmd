@echo off
REM Signs a release through addons.mozilla.org for self-distribution (unlisted channel) by hand, so
REM that it can be installed permanently in regular Firefox. A manual fallback: the release workflow
REM does this for every tag and attaches the signed .xpi to the GitHub release, and a release whose
REM signing failed is best helped by re-running that job, which uploads only while AMO has nothing
REM under the number. AMO refuses a version number that was uploaded before in either channel, so
REM never run this for a version that is still to be released: the workflow uploads only a number
REM AMO does not have, and fails the release when the signed file is not the tag's build.
REM Needs Node.js, git and an AMO API key:
REM   1. Create/log in to a Firefox account at https://addons.mozilla.org/developers/addon/api/key/
REM   2. Set the two values for this window:   set WEB_EXT_API_KEY=user:...   set WEB_EXT_API_SECRET=...
REM   3. Check out the release's tag (git checkout v<version>): the script refuses a tree that differs
REM      from it, uncommitted edits included, since its file goes onto that tag's release.
REM   4. Run this script. It builds dist\firefox as the release workflow does, uploads it and waits for
REM      AMO to sign it; the signed .xpi appears in dist\. Attach it to the release with
REM      gh release upload v<version> dist\shisu_ko-<version>.xpi
setlocal
cd /d "%~dp0"
if "%WEB_EXT_API_KEY%"=="" (
  echo WEB_EXT_API_KEY / WEB_EXT_API_SECRET are not set. See the comments at the top of this script.
  pause
  exit /b 1
)
set "VERSION="
REM Only a major.minor.patch number is taken, so nothing but digits and dots is ever expanded below.
for /f "delims=" %%v in ('node -p "const v = require('./addon/manifest.json').version; /^[0-9]+[.][0-9]+[.][0-9]+$/.test(v) ? v : ''"') do set "VERSION=%%v"
if "%VERSION%"=="" (
  echo addon\manifest.json holds no release version, major.minor.patch.
  pause
  exit /b 1
)
git diff --quiet "v%VERSION%" -- addon scripts\build.mjs || (
  echo addon\ or scripts\build.mjs differs from the release tag v%VERSION%, or there is no such tag.
  echo Check out the tag first: git checkout v%VERSION%
  pause
  exit /b 1
)
for /f "delims=" %%f in ('git ls-files --others --exclude-standard -- addon') do (
  echo addon\ holds a file the tag does not: %%f
  pause
  exit /b 1
)
node scripts\build.mjs --browser firefox || (pause & exit /b 1)
npx --yes web-ext@10.7.0 sign --source-dir dist\firefox --artifacts-dir dist --channel unlisted --no-input
pause
