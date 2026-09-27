# Publishing on the Chrome Web Store

The Chrome listing is
https://chromewebstore.google.com/detail/shisu-ko/ecenifonpkaiccmmknpbllbebbfigjnm (item id
`ecenifonpkaiccmmknpbllbebbfigjnm`). Its versions so far were uploaded by hand in the Developer
Dashboard, and on 2026-09-26 the store still served 0.5.0 while the repository was at 0.14.6. From
the first run of `cws-listing.yml` on, every release goes to the store by itself: a Chrome Web
Store install gets its updates only from the store, and the store takes a release as it is, with no
listing texts to build and no version number of its own, so unlike addons.mozilla.org
(`docs/amo/README.md`) there is nothing to decide per release. What stays by hand is the key, set
up once as below, the listing's texts and settings in the Dashboard, and the few results a run
cannot settle by itself.

## How a release gets there

Pushing a tag runs `release.yml` (**Release extensions**), which makes the GitHub release with its
`shisu-ko-<version>-chrome.zip`. Once that run has succeeded, `.github/workflows/cws-listing.yml`
(**Publish a release on the Chrome Web Store**) starts by itself, and only for a release run that
the push of a tag in this repository started. It downloads the Chrome zip of the release with the
highest version, which is not always the one GitHub marks Latest, and never builds one of its own.
`node scripts/cws.mjs status <version>` asks the store what it holds against that version, and only
when nothing is in the way does `cws.mjs submit` upload the zip and submit it for review. The store
publishes it by itself once the review has passed, usually within a few days, sometimes weeks later.

The store reviews one submission at a time. A release made while an older version still waits for
its review, or is approved and staged for someone to publish, is held back:
`.github/workflows/cws-schedule.yml` (**Ask the Chrome Web Store for held-back releases**) runs
`cws-listing.yml` every three hours and submits the highest release once the store has published
the older version or its review is cancelled. The schedule never cancels a review and never submits
past a rejected one; there it only warns.

By hand: **Actions** > **Publish a release on the Chrome Web Store** > **Run workflow**, or

    gh workflow run cws-listing.yml -R jura-org/shisu-ko

A run by hand does what a release's run does, for the highest release. With the box **Cancel a
review that waits for an older release and submit the newest release instead** ticked
(`-f cancel_review=true`) it also withdraws an older review that waits and submits the highest
release in its place. No run does that by itself: the older review may be nearly through, and the
Developer Dashboard allows six cancellations a day. One run goes at a time; the runs of a release,
of the schedule and by hand wait their turn in one queue.

`cws.mjs` speaks the store's API v2 alone; v1.1 stops on 2026-10-15.

## Setting up the key, once

The workflow signs in to the store as a Google Cloud service account and needs two things in the
repository: the repository secret `CWS_SERVICE_ACCOUNT_JSON`, that account's JSON key, and the
repository variable `CWS_PUBLISHER_ID`, the publisher id. Until both are set, the schedule passes
quietly, a release's run warns that the release did not reach the store, and a run by hand fails.
The service account holds no role in Google Cloud: its one power is its link to the publisher in the
Developer Dashboard (step 6), and through it every item the publisher owns.

Do all of it with the Google account that owns the Chrome Web Store publisher of Shisu-ko. The store
requires 2-Step Verification on that account for publishing and updating items.

1. Open the Google Cloud console, https://console.cloud.google.com/, signed in with that account.
2. Create a project: on the **Manage resources** page
   (https://console.cloud.google.com/cloud-resource-manager) click **Create project**, enter a
   project name such as `shisu-ko-cws`, leave **Parent resource** (**Location** in some versions
   of the form) at **No organization** and click **Create**. Then pick the new project in the
   project picker at the top of the console; steps 3 to 5 happen in it.
3. Enable the API: type `Chrome Web Store API` into the search bar at the top of the console, open
   it and click **Enable** (it is also at
   https://console.cloud.google.com/apis/library/chromewebstore.googleapis.com).
4. Create the service account: **IAM & Admin** > **Service accounts**
   (https://console.cloud.google.com/iam-admin/serviceaccounts) > **Create service account**. Enter
   a service account name such as `cws-publisher`; the console fills in the **Service account ID**
   and shows the account's email address under it,
   `cws-publisher@<project id>.iam.gserviceaccount.com`. Click **Done**, without a role under
   **Permissions** or anyone under **Principals with access**: the store needs neither.
5. Create its key: on the **Service accounts** page click the new account's email address, open the
   **Keys** tab, then **Add key** > **Create new key**, choose the key type **JSON** and click
   **Create**. The browser downloads the key file, and Google never offers it again. If the console
   refuses to create a key, the project sits in an organization that forbids keys (the policy
   `iam.disableServiceAccountKeyCreation`, the default for organizations created since
   3 May 2024); a project under **No organization** has no such policy.
6. Link the account to the publisher: open the Developer Dashboard,
   https://chrome.google.com/webstore/devconsole, with the same Google account (if it belongs to
   more than one publisher, switch to Shisu-ko's first), go to **Account**, paste the service
   account's email address into the **Service account** field and click **Add a service account**.
   A publisher takes only one service account.
7. Copy the publisher id: the same **Account** page shows it as **Publisher ID** under **Profile**
   (Google's API guide calls the place **Publisher** > **Settings**), and it is the id in the
   Dashboard's address, `https://chrome.google.com/webstore/devconsole/<publisher id>`.
8. Store both in the repository. On GitHub, open jura-org/shisu-ko > **Settings** > **Secrets and
   variables** (in the sidebar's **Security** section) > **Actions**. On the **Secrets** tab click
   **New repository secret**, enter the name `CWS_SERVICE_ACCOUNT_JSON`, paste the whole content of
   the key file as the **Secret** (open it in a text editor, select all, copy) and click **Add
   secret**. On the **Variables** tab click **New repository variable**, enter the name
   `CWS_PUBLISHER_ID`, the publisher id as the **Value**, and click **Add variable**. Both belong to
   the repository, not to an environment: the workflow names none. Or with the GitHub CLI, in the
   folder that holds the key file, with its name in place of `key.json` and the publisher id in
   place of `PUBLISHER_ID`. In bash:

   ```
   gh secret set CWS_SERVICE_ACCOUNT_JSON -R jura-org/shisu-ko < key.json
   gh variable set CWS_PUBLISHER_ID -R jura-org/shisu-ko --body PUBLISHER_ID
   ```

   In PowerShell, which has no `<` redirection:

   ```
   Get-Content -Raw key.json | gh secret set CWS_SERVICE_ACCOUNT_JSON -R jura-org/shisu-ko
   gh variable set CWS_PUBLISHER_ID -R jura-org/shisu-ko --body PUBLISHER_ID
   ```

   `gh secret list -R jura-org/shisu-ko` and `gh variable list -R jura-org/shisu-ko` then show both
   (a secret by its name only).
9. Delete the key file: `rm key.json` in bash, `Remove-Item key.json` in PowerShell; if it went to
   the recycle bin, or the folder is synced to a cloud drive, delete it there too. The secret is the
   one copy the workflow needs, and a lost key is replaced by a new one (steps 5, 8 and 9), never
   downloaded again.
10. Start the first run:

    ```
    gh workflow run cws-listing.yml -R jura-org/shisu-ko
    ```

    It submits the highest release, 0.14.6 or newer; *Reading a run* below says how to follow it.
    0.5.0, the version the store holds, has neither the `notifications` permission nor the optional
    `nativeMessaging`: should the publish fail over a permission the store wants justified, write
    the justification in the Dashboard's **Privacy** tab and submit the draft there (*When a request
    failed* below).

## Reading a run

The Actions tab lists the runs of a release and the runs by hand under **Publish a release on the
Chrome Web Store**, and the schedule's under **Ask the Chrome Web Store for held-back releases**,
whose job reads `catch-up / publish`. From the command line, with the run's ID (the ID column of
`gh run list`, not the #number the Actions tab shows):

    gh run list --workflow cws-listing.yml -R jura-org/shisu-ko --limit 5
    gh run watch RUN_ID -R jura-org/shisu-ko
    gh run view RUN_ID -R jura-org/shisu-ko
    gh run view RUN_ID -R jura-org/shisu-ko --log

(`--workflow cws-schedule.yml` lists the schedule's runs.) The step to read is **Submit it to the
Chrome Web Store**. Its first line is what the store holds, for example
`published 0.5.0 (PUBLISHED); nothing submitted` or
`published 0.5.0 (PUBLISHED); submitted 0.14.6 (PENDING_REVIEW)`. A run that uploads adds
`uploading dist/release/shisu-ko-<version>-chrome.zip (<size> bytes)`, then any
`the store warns: ...` lines, the store's own remarks on the submission, which do not stop it but
are worth reading, and last the line of what the store holds afterwards.

The result is one annotation, shown at the top of the run's page and by `gh run view`: a notice
when all went well or there is nothing to do, a warning when someone should act, and an error, with
a red run, when the release did not get where it should. A run that fails before the store has
answered names the cause in the step's last line, for example `CWS_SERVICE_ACCOUNT_JSON is not
JSON` or `the token request failed: HTTP 400 ...`.

## What each result asks of you

- **submit** (notice, `<version> is submitted to the Chrome Web Store (PENDING_REVIEW)`):
  nothing. The Dashboard shows the version as pending review, the store publishes it when the
  review passes and emails the publisher when it does not. Google asks for a message to developer
  support after three weeks in review.
- **in-review**, **published**, **newer** (notice, `nothing to submit`): nothing. `newer` means the
  store's submission holds a version above every release, one uploaded by hand.
- **cancelled** (notice): the review of this version was cancelled in the Dashboard, and the
  workflow leaves it alone until the next release. To go ahead after all, submit the draft there.
- **waiting** (notice): an older version waits for its review or is staged; the log's first line
  says which. Nothing is needed: the schedule submits the release once the store has published the
  older version or its review is cancelled, and for a staged version that means someone publishes
  it in the Dashboard. To withdraw the older review instead:
  `gh workflow run cws-listing.yml -R jura-org/shisu-ko -f cancel_review=true`. When that run
  fails, its log says whether the review was cancelled first: if it was, the schedule takes over;
  if not, run it again.
- **staged** (warning): this version passed its review and waits to be published, which happens
  only when someone submitted it in the Dashboard with publishing deferred. Publish it there within
  30 days, after which it returns to a draft that needs a new review.
- **rejected** (error; a warning on the schedule, since the store emails the rejection): read the
  review in the Dashboard, fix what it names and release the next patch version. The rejected
  version is never submitted again.
- **rejected-older** (warning): the store rejected an older version, and this release may repeat
  what was rejected, since it may have been made while that one was in review. The first run of the
  newest release's own release workflow submits it all the same, as does a run by hand, and warns
  to read the review; the schedule and a re-run only warn. Read the review: if the release does not
  repeat the problem, `gh workflow run cws-listing.yml -R jura-org/shisu-ko` submits it; if it does,
  fix it and release the next patch version.
- **taken-down** (error; a warning on the schedule): the store has taken Shisu-ko down, and the
  workflow submits nothing until it is back. Read why in the Dashboard and in the store's email,
  then appeal there, or fix it in a release and upload that release's
  `shisu-ko-<version>-chrome.zip` in the Dashboard by hand (**Package** > **Upload New Package**,
  then **Submit for Review**).
- **No key** (warning on a release, `this release did not go to the Chrome Web Store`; error on a
  run by hand): set up the key as above, then run the workflow by hand.

### When a request failed

A failed request ends the step with a line that names it (`the token request`, `fetchStatus`,
`the upload`, `publish`) and gives Google's own message. When it happened on the way to a
submission, the annotation reads `<version> did not reach the Chrome Web Store (the lines above say
why)`; before that, GitHub's own `Process completed with exit code 1` (or 2) stands in its place.

- The token request or `fetchStatus` failed: the sign-in did not work. Check that the secret holds
  the whole key file, that the key still exists and is enabled on the account's **Keys** tab, that
  the Chrome Web Store API is enabled in its project, that the Dashboard's **Account** page lists
  the service account, and that `CWS_PUBLISHER_ID` is the publisher id.
- The upload failed: nothing reached the store. Fix what it refused; the schedule tries again within
  three hours, or run the workflow by hand.
- The store was still reading the upload when the wait ended (`the upload is still IN_PROGRESS
  after 300 s: not submitting it`): the zip did reach the store, and the store may finish reading
  it afterwards. Look at the Dashboard's **Package** tab. If it holds the version as a draft, submit
  it there by hand (**Submit for Review**), as in the next case, since the store refuses the same
  version as a new upload. If it does not, the schedule uploads it again within three hours, or run
  the workflow by hand.
- The upload went in and the publish failed, for example over something the Dashboard wants for
  this version, such as a permission's justification in the **Privacy** tab: the Dashboard now
  holds the version as a draft, and the store refuses the same version as a new upload, so every
  later run fails the same way. Fix what it names in the Dashboard and submit the draft there by
  hand (**Submit for Review**).

When the submission that failed was one past a rejected older version, the schedule does not try
again at all: once the cause is fixed, run the workflow by hand.

## Keeping the key safe

- The key file is the whole credential: whoever holds it can upload and publish versions of every
  item of the publisher, Shisu-ko for all its users included, until the key is deleted. It never
  expires by itself.
- It lives only in the repository secret. GitHub never shows a secret again and hands this one to
  the one step that talks to the store, and `cws.mjs` never prints it or the tokens made with it.
  GitHub hands no secret to a pull request from a fork, but anyone who can push to the repository
  can reach it through a workflow of their own: write access here amounts to publishing rights on
  the store.
- Never commit it, paste it into an issue or a chat, or keep a copy in the checkout. Google disables
  a key it finds published in a public repository by itself, and releases then fail at the token
  request.
- If it leaks, or may have: delete it in the Google Cloud console (**IAM & Admin** > **Service
  accounts** > the account > **Keys**, the key's delete button). That cannot be undone and stops
  the key at once; a token already made with it lasts an hour at most. Then create a new key and
  store it (steps 5, 8 and 9). A key Google has disabled is replaced the same way.
- Give the service account no role in Google Cloud; it needs none.

## Only in the Developer Dashboard

The API uploads packages and submits them, nothing else. The rest is set on Shisu-ko's pages in the
Developer Dashboard and stays as it is there until someone changes it by hand:

- **Store Listing**: the description, the screenshots, the category and the other listing texts.
- **Privacy**: the single purpose, a justification for each permission, the data use disclosures
  and the privacy policy link. A release that adds a permission can need its justification there
  before the store takes it.
- **Distribution**: the visibility (public, unlisted or private) and the regions. The API always
  publishes with the visibility of the last publish: after a change of visibility in the Dashboard,
  the API cannot publish until one version has been published there by hand with the new setting.
- **Account**: the service account (step 6) and the email notifications. Rejection and take-down
  emails are on by default; emails for a published or staged version can be turned on there.

## Store rules

- Every version must be higher than the one before, compared part by part as numbers (0.14.10 is
  above 0.14.9, and 0.14.6 above 0.5.0), and the store takes each version once. So only the highest
  release goes up, and a rejected version is fixed forward with the next patch version.
- The package carries no `key` and no `update_url`: the store keeps the item's own key, refuses a
  key on a new item and any other than the item's own on an update, and a store install updates
  from the store. `addon/tests/settings.test.js` and `scripts/tests/build.test.mjs` hold both, and
  `build.test.mjs` also holds the manifest's name within 75 characters and its description within
  132 (it has 131).
- One submission is reviewed at a time, and a staged version returns to a draft after 30 days. A
  publisher can cancel six reviews a day and link one service account.

## When the schedule is switched off

GitHub switches off a workflow with a schedule in a public repository after 60 days without activity
in the repository. Only `cws-schedule.yml` has a schedule, so only the catch-up of held-back
releases stops then; `cws-listing.yml` still starts for every release. The Actions tab shows the
schedule as disabled; **Actions** > **Ask the Chrome Web Store for held-back releases** > **Enable
workflow** turns it back on, or

    gh workflow enable cws-schedule.yml -R jura-org/shisu-ko
