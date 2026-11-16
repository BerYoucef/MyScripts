# 🦀 Rust Without Visual Studio

A Python-based setup script for installing and configuring the official **Rust MSVC toolchain** on Windows without installing Visual Studio.

It provides the components required to build native Windows applications using Rust, with optional C/C++ compilation support through LLVM.

## ✨ Features

- 🦀 **Rust toolchain** — Installs Rust using the official `rustup` installer.
- 🔗 **Windows linker** — Uses Rust's bundled `rust-lld` instead of requiring Microsoft's `link.exe`.
- 📦 **Windows SDK & CRT** — Downloads and configures the required libraries and headers using [xwin](https://github.com/Jake-Shadle/xwin).
- ⚙️ **Optional LLVM** — Installs `clang-cl` and `llvm-lib` for crates that compile native C/C++ code.
- 🧩 **Cargo configuration** — Automatically configures the linker, library paths, and C/C++ compiler settings.
- 🧪 **Build verification** — Tests Rust compilation and, when LLVM is installed, C and C++ compilation.
- 🔄 **Safe updates** — Supports updating Rust and refreshing the Windows SDK.
- 🗑️ **Uninstallation** — Removes the components and configuration managed by the script without uninstalling Rust itself.

## 📋 Requirements

| Requirement | Details |
|---|---|
| OS | Windows 10 / 11 (x64) |
| Python | 3.8 or newer (64-bit) |
| Architecture | x86_64 |
| Internet | Required for downloads |
| Disk space | At least 5 GB free |
| Permissions | Developer Mode or Administrator for symbolic links |

Visual Studio and Visual Studio Build Tools are not required.

## 🚀 Installation

Clone the repository:

```powershell
git clone https://github.com/BerYoucef/MyScripts.git
cd MyScripts
```

Run the installer:

```powershell
python setup_rust.py
```

The installer will:

1. Check system compatibility and available disk space.
2. Install or configure the Rust MSVC toolchain.
3. Download the Windows CRT and SDK using xwin.
4. Optionally install LLVM for C/C++ compilation.
5. Configure Cargo automatically.
6. Run build tests to verify the installation.

**Note:** The Microsoft CRT and Windows SDK license must be accepted before downloading these components.

## 🛠️ Usage

| Command | Description |
|---|---|
| `python setup_rust.py` | Full installation with optional LLVM prompt |
| `python setup_rust.py --with-llvm` | Install everything, including LLVM |
| `python setup_rust.py --no-llvm` | Install Rust without LLVM |
| `python setup_rust.py --check` | Verify the existing installation |
| `python setup_rust.py --update` | Update Rust and refresh CRT/SDK |
| `python setup_rust.py --dry-run` | Preview operations without making changes |
| `python setup_rust.py --uninstall` | Remove script-managed components |
| `python setup_rust.py --set-default` | Set the MSVC toolchain as rustup default |
| `python setup_rust.py --accept-license` | Accept the Microsoft license non-interactively |
| `python setup_rust.py --yes` | Accept default answers (does not accept licenses) |
| `python setup_rust.py --no-color` | Disable colored terminal output |

### Examples

Install Rust with C/C++ support:

```powershell
python setup_rust.py --with-llvm
```

Verify an existing installation:

```powershell
python setup_rust.py --check
```

Update the installation:

```powershell
python setup_rust.py --update
```

Preview an installation:

```powershell
python setup_rust.py --dry-run
```

## 🧪 Testing Your Installation

After installation, open a new terminal and run:

```powershell
cargo new hello
cd hello
cargo run
```

Expected output:

```text
Hello, world!
```

To verify the installed toolchain:

```powershell
rustc --version
cargo --version
rustup show
```

When LLVM is installed, the installer also tests native C and C++ compilation.

## ⚙️ Installed Components

| Component | Purpose | Installation location |
|---|---|---|
| Rust / Cargo | Rust compiler and package manager | `~/.cargo` and `~/.rustup` |
| xwin | Windows CRT and SDK extraction | `~/.xwin` |
| LLVM | C/C++ compiler and librarian (optional) | `~/.llvm` |
| Cargo config | Linker and compiler configuration | `~/.cargo/config.toml` |

The script manages its Cargo configuration through a dedicated block:

```toml
# --- xwin begin ---

# Managed configuration

# --- xwin end ---
```

Existing configuration is backed up before modification when necessary.

## 📁 Repository Structure

```text
.
├── setup_rust.py    # Main installation and configuration script
├── vc_redist.py     # Visual C++ runtime companion
└── README.md
```

### About vc_redist.py

The companion script handles the Microsoft Visual C++ Redistributable (x64).

It:

- Checks whether the required runtime DLLs are present.
- Downloads the official Microsoft installer.
- Verifies its Authenticode digital signature.
- Requests user consent before installation.
- Installs the runtime silently through Windows UAC.

The runtime is only needed to **run** programs that depend on the Visual C++ runtime, not to compile pure Rust applications.

Keep both Python scripts in the same directory.

## 🔒 Security & Safety

- Downloads Rust from the official Rust distribution servers.
- Downloads xwin and LLVM releases from GitHub.
- Verifies available SHA-256 checksums.
- Verifies the Microsoft Visual C++ Redistributable digital signature before execution.
- Uses temporary staging directories when replacing SDK and LLVM installations.
- Preserves previous installations when a replacement fails.
- Does not automatically install the Visual C++ runtime without consent.

**Important:** When `COMPANION_SHA256` is empty, the companion script is checked for basic structural validity, but its authenticity is not cryptographically pinned. Review the source before executing it.

## 🗑️ Uninstallation

To remove the components installed by this script:

```powershell
python setup_rust.py --uninstall
```

This removes:

- xwin files and its cache.
- LLVM files.
- The managed Cargo configuration block.
- Downloads created by the installer.
- LLVM's PATH entry.

Rust itself is preserved.

To uninstall Rust separately:

```powershell
rustup self uninstall
```

## 📜 License

See the repository's [LICENSE](LICENSE) file for licensing information.

---
 

**Built for developers who want a lightweight Rust MSVC environment on Windows without installing the full Visual Studio IDE or Build Tools.** 🦀
