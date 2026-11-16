# Runner Requirements and Supply-Chain Security

## Runner requirements

GitHub-hosted runners (`ubuntu-latest`, `macos-latest`, `windows-latest`) work out of the box. Prebuilt binaries are installed with `cargo-binstall` (no Rust toolchain needed).

Self-hosted runners require:

- **Actions Runner 2.327.1+ and glibc 2.28+** (Node 24 required for `actions/checkout` and `actions/cache`; Alpine, CentOS 7, and Ubuntu 18.04 are not supported). On ARM64 Linux, the prebuilt `blockwatch` binary needs **glibc 2.30+**, which rules out RHEL 8 and Debian 10 there.
- **git 2.18+**
- **bash** (Git Bash on Windows)
- **curl**, plus **tar** (Linux) or **unzip** (macOS/Windows)
- **Python 3.7+**, as `python3` or `python` on `PATH` (the action's logic runs in Python)
- Network access to `github.com` and `crates.io`
- Writable `CARGO_HOME` or `CARGO_INSTALL_ROOT`

## Pinning and supply chain

The `@v1` tag moves with each release; pin to an exact release tag or a full commit SHA for reproducible runs.

Every action this one calls is pinned by commit SHA, so a SHA pin here fixes all the action code that runs and the `blockwatch` version. On a cache miss, the `blockwatch` archive is downloaded from its GitHub Release and must carry a valid signature from the key in that version's crate, or the install fails—so only a binary the blockwatch project signed can be installed.

Three gaps remain:

1. The signature fixes who signed the binary, not its exact bytes.
2. The `cargo-binstall` executable is downloaded by version and not checksum-verified.
3. A binary restored from the GitHub Actions cache runs without re-verifying its signature.
