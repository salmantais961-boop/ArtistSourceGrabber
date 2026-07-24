# Artist Source Grabber V2

## v2.1.0（2026-07-25）

本次 Release 基于已合并到 `main` 的 V2.x 更新：

- 透明图片可选自动铺设用户指定的纯色底，并在作品卡片显示处理状态；
- 新增起始页 / 结束页抓取范围限制；
- 新增 Danbooru 角色、通用标签和多 Tag AND 搜索；
- 优化 LLM 证据优先提示词，降低幻觉、冲突和重复标签；
- WD14 支持通用阈值、角色阈值和 CUDA 优先 / CPU 回退；
- 合并 [buxinzi2233](https://github.com/buxinzi2233/ArtistSourceGrabber) 的 Linux 与角色/标签搜索贡献；

下载文件：`ArtistSourceGrabber-V2.1.0.zip`

## v2.0.0 主要更新

- 全面重做现代简约 WebUI；
- 图片按真实宽高比展示；
- 新增原图预览与逐图打标详情；
- X / Pixiv 专用登录 Profile 持久化和浏览器进程清理；
- 多来源统一目录、文件名前缀和 SHA-256 去重；
- LLM JSON 解析、预设提示词和 ONNX 标签合并策略修复；
- 新增完整依赖一键安装脚本。

## 安装

1. 下载并解压 `ArtistSourceGrabber-V2.1.0.zip`。
2. 先运行 `先运行这个.bat`。
3. 安装完成后运行 `start.bat`。
4. 浏览器打开本机 WebUI 后再配置来源、登录和打标器。

## 重要风险提示

本项目是纯 VIBE Coding / AI 辅助编程产物，未经专业安全或法律审计。使用非官方工具访问 X、Pixiv 等平台可能导致验证码、限流、强制退出、Cookie 失效、账号限制或永久封禁。

请低频、小规模测试，不要在重要主账号上冒险，不要用多个账号绕过封禁或限流，不要绕过付费、年龄、地域、关注者限定或其他访问控制。

公开可访问不等于获得下载、训练、再分发或商业使用许可。作品版权、站点条款、账号安全、数据使用和当地法律合规责任均由使用者承担。

完整说明请阅读仓库 [README](https://github.com/salmantais961-boop/ArtistSourceGrabber#readme)。

## 文件校验

`ArtistSourceGrabber-V2.1.0.zip`

```text
SHA-256: 335FDF4575D6DAD16510C5117A76006C8188C9112FBDC8FB159578DC8AAA3921
```
