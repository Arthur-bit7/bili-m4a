# Bili M4A

把 B 站视频链接变成可试听、可下载的 M4A 纯音频。一个在自己电脑上运行的开源小工具。

[GitHub 仓库](https://github.com/Arthur-bit7/bili-m4a) · [自动化测试](https://github.com/Arthur-bit7/bili-m4a/actions)

## Windows 开始使用

1. 安装 **Python 3.10 或更新版本**，安装时勾选 **Add Python to PATH**。
2. 将整个项目解压到一个文件夹，双击 **start.bat**。
3. 首次启动会联网安装依赖；完成后自动打开 <http://127.0.0.1:8765>。
4. 粘贴 B 站视频链接或 BV 号，点击“获取音频”，按提示提取，完成后试听或下载音频。

运行期间保留启动窗口；使用网页“退出并清理缓存”或按 `Ctrl+C` 正常退出。退出会清理本次缓存，请先下载需要保留的音频。无需单独安装 FFmpeg：imageio-ffmpeg 在常见 Windows/macOS/Linux 平台通过 wheel 提供 FFmpeg。其他架构可能需要手动安装 FFmpeg 并设置 `IMAGEIO_FFMPEG_EXE`。

## macOS / Linux / 手动启动

```sh
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 则用 .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

不自动开浏览器：`python app.py --no-browser`。端口被占用：`python app.py --port 8766`。

## 输入和输出

- 支持 `https://www.bilibili.com/video/BV...`、`av...` 视频链接、完整 BV 号和 `b23.tv` 分享短链。
- 支持包含链接的分享文字。
- 粘贴链接后自动识别分 P，支持搜索和跨页多选。默认勾选第 1 P；链接中的 `?p=2` 会默认勾选第 2 P。
- 第一版面向无需登录的普通公开视频；不提供登录、会员权限绕过、直播、番剧或整个合集批量下载。
- 优先选取 AAC 音轨，直接封装 M4A，避免重复编码；其他音轨转为 192 kbps AAC（转码不意味着提升源音质）。极少数只有混合媒体的老视频需要先下载含画面的源文件，最终输出仍只保留音频。
- 输出仅包含一条音轨，不添加视频或封面轨。先解析 M4A 时长和声道，再由 FFmpeg 完整解码；通过校验后才显示下载。
- 文件保存在 `downloads/<任务ID>/audio.m4a`，网页下载会使用视频标题作为文件名。临时源文件处理后自动删除。
- 任务记录保存在内存，刷新页面仍可查看，重启后清空。关闭网页不会停止任务；正常退出会取消等待任务，待当前提取和传输结束后清理本次缓存。启动前已有文件和浏览器另存的文件不受影响；异常退出不会在下次启动时自动补清理。
- 每批最多 50 个分 P，全局最多 100 个未完成任务；超过上限时请减少选择或等待任务完成后重试。按 P 编号串行处理，单项失败不影响后续任务，可取消等待中的任务。
- 任务按批次展示，支持逐个试听、下载，也可将本批次当前已完成的音频打包为 ZIP。
- 已结束批次最多保留 500 条任务记录；旧记录移出网页不会提前删除缓存。关闭程序后队列不恢复。

## 常见问题

**解析失败 / 412 / 403 / 请求过多**：先确认视频在 B 站能正常打开，等待一段时间再试。平台接口或风控会变化，无法保证任意视频都能下载。Windows 可双击 `update.bat` 更新解析器；手动更新：

```sh
python -m pip install --upgrade -r requirements.txt
```

**需要登录**：这一版不读取浏览器 Cookie、不保存账号信息，仅支持无需登录即可获取的资源。

**启动后没有自动开网页**：手动打开启动窗口中的本地网址。首次安装需要网络，依赖或代理问题会显示在窗口中。

**为什么是 M4A 而非 MP3？** M4A 是容器，输出音频使用 AAC，可在多数手机、电脑播放器中播放；直接复制 AAC 可保留源音质。

## 开发

```sh
python -m unittest discover -s tests -v
```

前端交互测试（需要 Node.js 18+）：`node --test tests/test_frontend.cjs`。

测试不请求 B 站：覆盖链接验证、指定分 P、权限检查、任务错误、下载及 Range 请求；使用真实 FFmpeg 合成音视频样本，检查无视频轨、AAC 复制、WAV 转 AAC 和损坏文件拒绝。网络和 B 站实测请见 `VALIDATION.md`，不要将离线通过当作线上成功。

```text
app.py             Flask API、单 worker 队列、本地服务
core.py            链接处理、yt-dlp 下载、FFmpeg 转换和校验
templates/         页面
static/            样式和浏览器脚本
tests/             自动化测试
```

默认只监听 `127.0.0.1`，校验 Host 和提交令牌，不是可以直接暴露到公网的托管下载站。没有统计埋点；音视频请求由本机发往 B 站及其 CDN。

本项目代码采用 MIT 许可证，依赖保留各自许可证。分发 FFmpeg 二进制时需另行遵守其具体构建的许可；源码包不包含二进制、虚拟环境、Cookie 或下载音频。仅处理你有权下载的内容。

实现参考：[yt-dlp 官方文档](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp)、[FFmpeg 官方文档](https://ffmpeg.org/ffmpeg.html)、[imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)。
