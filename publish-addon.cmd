@echo off
REM Submits a release to the public listing on addons.mozilla.org (listed channel) by hand: the
REM fallback for .github\workflows\amo-listing.yml, which does the same from GitHub. The release
REM workflow has every tag signed for self-distribution under its own number, and AMO takes a number
REM once in either channel, so the listed build is the release's code under <version>.1: this builds
REM dist\firefox and rewrites the version in its manifest (scripts\amo-xpi.mjs listing); addon\
REM stays as it is. The listing text comes from docs\amo (make_metadata.py builds the JSON web-ext
REM sends); the privacy policy, icon and screenshots are set once in the Developer Hub, see
REM docs\amo\README.md. Needs Node.js, Python, git and the same AMO API key as sign-addon.cmd:
REM   1. https://addons.mozilla.org/developers/addon/api/key/
REM   2. Set the two values for this window:   set WEB_EXT_API_KEY=user:...   set WEB_EXT_API_SECRET=...
REM   3. Check out the newest release's tag (git checkout v<version>): only the newest release goes to
REM      the listing (AMO disables a listed version still in review when another is submitted, and
REM      takes one only above the last approved), and the reviewer notes point to its tag. The script
REM      refuses anything else, uncommitted edits included.
REM   4. Run this script. It uploads, creates the version and returns at once; the review takes days.
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
REM The newest release tag as origin has it, among the tags made of v, digits and dots alone, so
REM that no other tag name is ever expanded below (findstr /x cannot be used: git ends its lines
REM with LF alone, which findstr /x does not match).
git fetch --quiet --tags origin || (
  echo Could not fetch the release tags from origin.
  pause
  exit /b 1
)
set "NEWEST="
for /f "delims=" %%t in ('git tag --list "v[0-9]*" --sort=-v:refname ^| findstr /r /v /c:"[^v0-9.]"') do if not defined NEWEST set "NEWEST=%%t"
if not "%NEWEST%"=="v%VERSION%" (
  echo v%VERSION% is not the newest release tag, %NEWEST% is: publish the newest release.
  pause
  exit /b 1
)
git diff --quiet "v%VERSION%" -- addon docs\amo scripts\build.mjs || (
  echo addon\, docs\amo or scripts\build.mjs differs from the release tag v%VERSION%.
  echo Check out the tag first: git checkout v%VERSION%
  pause
  exit /b 1
)
for /f "delims=" %%f in ('git ls-files --others --exclude-standard -- addon') do (
  echo addon\ holds a file the tag does not: %%f
  pause
  exit /b 1
)
python docs\amo\make_metadata.py || (pause & exit /b 1)
node scripts\build.mjs --browser firefox || (pause & exit /b 1)
set "LISTED="
for /f "delims=" %%v in ('node scripts\amo-xpi.mjs listing dist\firefox') do set "LISTED=%%v"
if not "%LISTED%"=="%VERSION%.1" (
  echo The listed build was not stamped %VERSION%.1.
  pause
  exit /b 1
)
npx --yes web-ext@10.7.0 sign --source-dir dist\firefox --artifacts-dir dist --channel listed --amo-metadata docs\amo\amo-metadata.json --approval-timeout 0 --no-input
pause
