# Build an IPA with GitHub Actions

The **Build iPhone IPA** workflow builds on a GitHub-hosted ARM64 Mac. You can prepare the inputs and download the result from Windows; a local Mac is not needed. The output is an unsigned IPA that you sign and sideload yourself. Device requirements and game compatibility are the same as in the [README](../README.md#games).

The workflow runs only when manually dispatched. It does not run on pushes or pull requests, sign an app, publish a Release, or cache game files. A new run downloads and builds the game again.

## 1. Provide the game files

Put your own base game NSP or XCI, and optionally its update and DLC NSPs, in private storage that supports direct HTTPS downloads. Expiring signed object-storage URLs work. Each URL must return the actual file without a browser login, cookies, or an HTML sharing page. ZIP, NSZ and XCZ archives are not supported by this workflow.

Keep the title ID and version tags in the filenames, especially `[v<N>]` for updates. Include only one game, its updates and its DLC. The build packs the newest supplied update unless you select another version when dispatching it.

In your repository, open **Settings > Secrets and variables > Actions > Secrets > New repository secret**. Add a secret named **`GAME_FILES_JSON`** with a JSON array like this (replace the example URLs with your own):

```json
[
  {
    "name": "Super Mario Bros. Wonder [010015100B514000][v0].nsp",
    "url": "https://storage.example.com/private/base.nsp?signature=REPLACE_ME"
  },
  {
    "name": "Super Mario Bros. Wonder [010015100B514800][v327680].nsp",
    "url": "https://storage.example.com/private/update.nsp?signature=REPLACE_ME"
  }
]
```

For the base game alone, use an array with just its object. Add DLC as more objects. `name` is the filename to save on the runner; it need not match the final component of the URL. Use lowercase `.nsp` or `.xci` extensions. Each object can also have an optional `sha256` field containing the file's 64-character SHA-256 checksum. You can obtain it on Windows with:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath 'D:\Games\Base [010015100B514000][v0].nsp'
```

Give signed URLs enough lifetime for the queue, dependency installation and downloads. For example, a full day avoids short expiry windows. A new run needs URLs that are still valid. The download step retries interrupted downloads from the beginning and checks the file header, length when supplied by the server, and optional checksum.

## 2. Provide keys and the download password

Create two more **repository secrets**:

| Secret | Value |
|---|---|
| `PROD_KEYS` | The complete text inside your own `prod.keys`, pasted as multiple lines. Use the file contents, not its Windows path or Base64. |
| `IPA_PASSWORD` | A long, random, single-line password. Save a copy locally; you need the same password to decrypt the result. |

The runner writes `prod.keys` into its temporary game directory with restricted file permissions. Keys, game downloads and intermediate files stay outside the checkout. The cleanup step removes the keys and plaintext IPA even after a failed build; the GitHub-hosted VM is discarded after the job.

The uploaded file is always encrypted with GnuPG AES-256 and integrity protection, including in private repositories. Artifacts in public repositories can be downloaded by other signed-in users, and the IPA contains the game data. Keep the password and private URLs in Secrets rather than workflow inputs, repository files, issues or logs. The workflow does not need Apple credentials or a signing certificate.

## 3. Run the workflow

1. Open **Actions** in your repository. If Actions is disabled on your fork, enable it.
2. Select **Build iPhone IPA > Run workflow**, using the branch that contains this workflow (normally `main`).
3. Choose the options below and click **Run workflow**.

| Input | Meaning |
|---|---|
| `runner` | `macos-26` by default. `macos-26-xlarge` is a paid runner with more CPU/RAM, requires an eligible GitHub plan, and does not guarantee more disk space. |
| `language` | Optional language supported by the game, such as `zh-Hans`, `zh-Hant`, `ja` or `en-US`. Empty uses the build script's normal default, usually English. |
| `app_name` | Optional home-screen name. Empty keeps the game's name. |
| `package` | Optional NSP version tag, such as `v327680` or `v0`. Empty selects the newest supplied version. |
| `free_disk_space` | Enabled by default. Removes unused Xcode installations on this temporary GitHub-hosted VM, preserving the selected Xcode. |
| `check_only` | Checks dependency installation, ARM64 architecture and the iOS/macOS Metal compilers, without secrets, game downloads or an IPA. Useful for checking the runner first. |

The normal build validates the secrets, installs dependencies, downloads the game, checks disk space, runs `scripts/build_ipa_from_nsp.py`, encrypts the IPA, and uploads it. Build progress and failure details appear in the job's steps. The job has a 350-minute timeout, leaving some room within GitHub's hosted-job limit.

## 4. Download and decrypt on Windows

1. Open the completed run under **Actions > Build iPhone IPA**.
2. In the run summary, click the download link, or scroll to **Artifacts** and download **`nxnative-ipa-<run ID>-<attempt>`** while signed in.
3. Extract the downloaded ZIP. It contains `game.ipa.gpg` and `SHA256SUMS.txt`. The artifact expires after **3 days**.
4. Install [Gpg4win](https://www.gpg4win.org/), which includes GnuPG. Open a new PowerShell window in the extracted folder and run:

```powershell
gpg --output game.ipa --decrypt game.ipa.gpg
```

Enter the exact `IPA_PASSWORD` used for this run when prompted. No public/private GPG key pair is needed. If `gpg` is not on your PATH, use the installed GnuPG executable or decrypt through Gpg4win's Kleopatra interface. You can check the downloaded encrypted file against `SHA256SUMS.txt` with:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath .\game.ipa.gpg
```

Sign and install `game.ipa` using your Windows sideloading tool, such as [Sideloadly](https://sideloadly.io/). The IPA is unsigned; downloading it alone does not install it. Where supported by your signing setup, preserve the **Increased Memory Limit** entitlement for larger games (see the [README](../README.md#pack-an-ipa)). The workflow does not test the game on an iPhone.

## Space, costs and troubleshooting

- **Disk space:** GitHub currently lists 14 GB of storage for standard Mac runners. Removing unused Xcode versions can free more, but available space varies with the image. The project estimates total build space as twice the input size plus 10 GB. After downloading, this workflow requires free space of at least the downloaded size plus 10 GiB. It checks again before encryption, which temporarily needs another IPA-sized copy plus a 1 GiB margin. `--no-keep-extracted` removes extracted files after a successful build; it does not reduce peak space needed during the build. Very large games may still need a Mac host with more storage.
- **Costs:** Standard hosted runners are free for public repositories. Private repositories consume included Actions usage and may incur charges; larger runners are billed separately. Large artifacts also count toward your account's storage allowance. Delete downloaded artifacts you no longer need. Check [GitHub's runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), [larger runner specifications](https://docs.github.com/en/actions/reference/runners/larger-runners), and [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) before choosing a paid runner.
- **HTTP 401/403 or download failures:** Refresh the signed URL and confirm it downloads the file without a browser session. A sharing-page URL or ZIP fails the NSP/XCI header check.
- **Missing or invalid secrets:** Add all three names under repository **Secrets**, not Variables. Check that `GAME_FILES_JSON` is valid JSON and `PROD_KEYS` contains the actual key-file text.
- **No artifact:** `check_only` runs do not build one. Otherwise inspect the first failed step; failed builds do not upload a partial or plaintext IPA.
- **Build succeeds but the game fails:** Use the game's tested update where possible, check device requirements and signing entitlements, and inspect the app's `Documents/log.txt`. Some games run the Mac host during shader capture or decompression, so CI runtime behavior can matter in addition to cross-compilation. An unprofiled game still needs bring-up as described in [development.md](development.md#port-another-game).
