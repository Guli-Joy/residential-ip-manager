# Security Policy

## 支持范围

安全修复优先应用于最新 Release 和 `main` 分支。本项目会处理网络路由、OpenVPN 配置和
本机 Clash 运行信息，因此请只从本仓库 Releases 下载构建产物，并核对随 Release 提供的
`SHA256SUMS.txt`。

## 报告安全问题

请通过 GitHub Security Advisory 的 **Report a vulnerability** 私密报告安全问题，不要在
公开 Issue 中提交订阅地址、控制器密钥、完整日志、OpenVPN 凭据或可识别个人网络的信息。

报告建议包含：

- 受影响版本与 Windows 版本；
- 可复现的最小步骤；
- 预期行为与实际行为；
- 已脱敏的日志或配置片段；
- 对路由、DNS、凭据或进程所有权的实际影响。

## 信任边界

- VPNGate 节点和远程 OpenVPN 配置均按不可信输入处理；
- 程序不会保证公共 VPN 节点的运营者、内容或长期可用性；
- IP 类型判断依赖第三方情报，只能降低误判，不能提供永久的住宅属性保证；
- Clash 订阅、控制器密钥和本机生成的运行配置不应提交到公开仓库。
