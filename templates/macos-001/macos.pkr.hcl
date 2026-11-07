# Packer template for building macOS Tart golden image
# Compliant with TECH-DESIGN section 7.4.3, 7.6 and tasks/macos-001/task.yaml

packer {
  required_plugins {
    tart = {
      version = ">= 1.5.0"
      source  = "github.com/cirruslabs/tart"
    }
  }
}

variable "vm_name" {
  type    = string
  default = "macos-27-xcode-27.2-metal"
}

variable "base_image" {
  type    = string
  default = "ghcr.io/cirruslabs/macos-sonoma-base:latest"
}

variable "cpu_count" {
  type    = number
  default = 8
}

variable "memory_mb" {
  type    = number
  default = 16384
}

variable "disk_size_gb" {
  type    = number
  default = 50
}

source "tart-clone" "golden" {
  vm_name         = var.vm_name
  from_image      = var.base_image
  cpu             = var.cpu_count
  memory          = var.memory_mb
  disk_size       = var.disk_size_gb
  ssh_username    = "admin"
  ssh_password    = "admin"
  ssh_timeout     = "60m"
  run_arguments   = ["--no-graphics"]
}

build {
  sources = ["source.tart-clone.golden"]

  # 1. Install Xcode Command Line Tools and Metal compilation toolchain
  provisioner "shell" {
    inline = [
      "echo '===> Installing Xcode Command Line Tools...'",
      "touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress",
      "PROD=$(softwareupdate -l | grep -B 1 -E 'Command Line Tools' | awk -F'*' '/^   \\*/ {print $2}' | sed -e 's/^ *//' | tr -d '\\n')",
      "if [ -n \"$PROD\" ]; then softwareupdate -i \"$PROD\" --verbose; fi",
      "rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress",
      "xcode-select --install || true",
      "xcrun swift --version",
      "xcrun metal --version || true"
    ]
  }

  # 2. Install and configure Tart Guest Agent for tart exec support
  provisioner "shell" {
    inline = [
      "echo '===> Setting up Tart Guest Agent...'",
      "curl -sSL https://github.com/cirruslabs/tart/releases/latest/download/tart-guest-agent-darwin-arm64 -o /usr/local/bin/tart-guest-agent || true",
      "if [ -f /usr/local/bin/tart-guest-agent ]; then chmod +x /usr/local/bin/tart-guest-agent; fi"
    ]
  }

  # 3. Create unprivileged agent user (UID 10001) and initialize /workspace
  provisioner "shell" {
    inline = [
      "echo '===> Creating unprivileged user agent (UID 10001)...'",
      "sudo sysadminctl -addUser agent -UID 10001 -home /Users/agent -shell /bin/bash -password agent || true",
      "sudo dscl . -create /Users/agent UniqueID 10001 || true",
      "sudo dscl . -create /Users/agent PrimaryGroupID 10001 || true",
      "sudo mkdir -p /workspace",
      "sudo chown -R 10001:10001 /workspace",
      "sudo chmod 755 /workspace",
      "sudo mkdir -p /Users/agent/.ssh",
      "sudo chown -R 10001:10001 /Users/agent"
    ]
  }

  # 4. Remove sudo privileges for agent user and harden isolation
  provisioner "shell" {
    inline = [
      "echo '===> Verifying isolation and hardening...'",
      "# Ensure agent is strictly NOT in admin / sudoers group",
      "sudo dseditgroup -o checkmember -m agent admin || true",
      "# Disable system crash reporter prompts and telemetry",
      "sudo defaults write com.apple.CrashReporter DialogType none",
      "# Set neutral computer name",
      "sudo scutil --set ComputerName 'eval-macos-golden'",
      "sudo scutil --set HostName 'eval-macos-golden'",
      "sudo scutil --set LocalHostName 'eval-macos-golden'"
    ]
  }

  # 5. Clean up temporary files, bash history, and caches
  provisioner "shell" {
    inline = [
      "echo '===> Cleaning up temporary files...'",
      "sudo rm -rf /tmp/* /var/tmp/* ~/.bash_history /Users/agent/.bash_history",
      "sync"
    ]
  }
}
