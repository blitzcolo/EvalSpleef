# Windows Golden VHDX 构建与使用指南

本文档依据 `TECH-DESIGN.md` §7.4.6 (`hyperv`)、§7.7 与 `PRD.md` FR-SBX-08 编写，说明 EvalSpleef Windows 金镜像（Golden VHDX）的构建步骤、配置要求与沙箱生命周期管理。

---

## 1. 金镜像构建目标

生产评测中，Windows 任务通过 Windows Server / Windows 11 Pro 宿主机上的 Hyper-V 虚拟机运行。每个 trial 基于金镜像创建独立的**差分虚拟硬盘（Differencing VHDX）**，并在 trial 结束后销毁，确保测试隔离与环境纯净。

金镜像必须满足以下四个硬性安全与评测契约：
1. **非管理员运行**：创建标准用户 `agent`（位于 `Users` 组，严禁加入 `Administrators` 组）；
2. **OpenSSH Server 就绪**：启用 `sshd` 服务，默认 Shell 配置为 `powershell -NoProfile -NonInteractive`，仅允许控制面专用公钥登录；
3. **关闭遥测、云上报与自动更新**：
   - 禁用 Windows Telemetry (`AllowTelemetry = 0`)；
   - 禁用 Windows Update (`wuauserv` 禁用并停止)；
   - 禁用 Windows Error Reporting (WER)；
   - 禁用 Windows Defender MAPS 遥测与自动样本上传（`Set-MpPreference -MAPSReporting Disabled -SubmitSamplesConsent 2`）；
4. **Sysprep 封装**：使用通用化封装工具清理硬件与机器专用标识，固定金镜像 sha256 校验和。

---

## 2. 构建步骤（基于 Packer 或 Hyper-V 无人值守安装）

### 步骤 1：准备无人值守安装应答文件
使用本目录下的 `autounattend.xml` 挂载为软盘/虚拟光盘镜像（Floppy/ISO）。

### 步骤 2：初始化虚拟机并安装操作系统
在 Hyper-V 宿主机上执行 PowerShell：
```powershell
# 1. 创建基础虚拟磁盘
New-VHD -Path "C:\Hyper-V\Golden\base.vhdx" -SizeBytes 60GB -Dynamic

# 2. 创建 Gen2 虚拟机
New-VM -Name "WinGolden-Build" `
       -MemoryStartupBytes 4GB `
       -VHDPath "C:\Hyper-V\Golden\base.vhdx" `
       -Generation 2 `
       -SwitchName "Default Switch"

# 3. 挂载安装 ISO 与应答软盘/ISO
Set-VMDvdDrive -VMName "WinGolden-Build" -Path "C:\ISOs\WindowsServer2022.iso"
Set-VMProcessor -VMName "WinGolden-Build" -Count 4
Start-VM -Name "WinGolden-Build"
```

### 步骤 3：配置非管理员用户与工作空间
在系统安装完成后通过 PowerShell 运行加固与初始化脚本：
```powershell
# 确认 agent 用户为标准受限用户
net user agent /add
net localgroup Users agent /add
net localgroup Administrators agent /delete

# 创建工作空间目录并授予 agent 完全控制权限
New-Item -ItemType Directory -Force -Path "C:\workspace"
icacls "C:\workspace" /grant "agent:(OI)(CI)F" /inheritance:e

# 配置 OpenSSH 服务
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'

# 注入控制面 SSH 公钥
$sshDir = "C:\Users\agent\.ssh"
New-Item -ItemType Directory -Force -Path $sshDir
Copy-Item "C:\evalspleef-control.pub" "$sshDir\authorized_keys"
icacls "$sshDir\authorized_keys" /inheritance:r /grant "agent:R" /grant "SYSTEM:F"

# 设置默认 Shell 为 PowerShell
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
                 -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
                 -PropertyType String -Force
```

### 步骤 4：禁用遥测、自动更新与日志上报
```powershell
# 关闭遥测
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection" `
                 -Name "AllowTelemetry" -Type DWord -Value 0

# 禁用 Windows Update
Stop-Service -Name wuauserv -Force -ErrorAction SilentlyContinue
Set-Service -Name wuauserv -StartupType Disabled

# 禁用 Defender 云端样本提交
Set-MpPreference -MAPSReporting Disabled -SubmitSamplesConsent 2
```

### 步骤 5：执行 Sysprep 封装金镜像
```powershell
# 执行系统通用化并关机
& C:\Windows\System32\Sysprep\sysprep.exe /oobe /generalize /shutdown /mode:vm
```

### 步骤 6：锁定金镜像并记录校验哈希
关机后将虚拟磁盘设置为只读，并生成 sha256 签名存入模板注册表：
```powershell
Set-ItemProperty -Path "C:\Hyper-V\Golden\windows-golden.vhdx" -Name IsReadOnly -Value $true
Get-FileHash -Algorithm SHA256 "C:\Hyper-V\Golden\windows-golden.vhdx" | Export-Clixml "C:\Hyper-V\Golden\windows-golden.sha256"
```

---

## 3. Trial 运行时生命周期（Hyper-V 适配器）

在执行评测 Trial 时，`evalspleef-sbx-hyperv` 适配器按如下流程管理：

1. **创建差分磁盘**：
   ```powershell
   New-VHD -ParentPath "C:\Hyper-V\Golden\windows-golden.vhdx" `
           -Path "C:\Hyper-V\Trials\<trial-id>\disk.vhdx" `
           -Differencing
   ```
2. **启动轻量隔离 VM**：
   绑定专用 vSwitch（断网或出站白名单网桥），分配 CPU 与内存限制。
3. **命令执行（Exec）**：
   通过 `asyncssh` 连接虚拟机上的 OpenSSH 服务，以 `agent` 用户身份在 `C:\workspace` 执行。
4. **清理与销毁**：
   ```powershell
   Stop-VM -Name "<vm-id>" -TurnOff
   Remove-VM -Name "<vm-id>" -Force
   Remove-Item -Path "C:\Hyper-V\Trials\<trial-id>" -Recurse -Force
   ```

---

## 4. 隔离预检项（§7.7 探针）

控制面在接收沙箱后执行以下 PowerShell 探针验证隔离合规性：
- `(whoami /groups) -notmatch "Administrators|S-1-5-32-544"`（非管理员）
- `(Get-SmbShare).Count -eq 0`（无宿主 SMB 共享泄露）
- `(Get-ChildItem Env:).Name -notmatch "EVAL|TASK|TRIAL|SECRET"`（去评测标识化）
- `Test-NetConnection -ComputerName example.com -Port 443 -InformationLevel Quiet`（在断网模式下必须返回 False）
