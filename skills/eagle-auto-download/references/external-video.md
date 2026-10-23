# Vimeo 外链视频下载

用户要求视频文件时，运行 `scripts/external_video.py`。它消费状态库中的 Vimeo 书签，自动查找官方允许下载的文件；书签和视频文件分别记录。默认优先走官方公开视频页的下载按钮流程，再考虑 yt-dlp 播放器提取。

导入模式及验收字段见 [导入验收](acceptance.md)。用户要文件时配置 `collection.external_video_mode: files`（若同时要书签则为 `both`），不能用仅 URL 条目代替视频文件。外链下载器的暂停与 Eagle 导入的 `pause-import` 分别保留；优化 Skill 或完成离线检查不会自动解除任一暂停。

## 已验证的匿名官方下载流程

2026-10-16，Vimeo 新版网页下载按钮采用 API `3.4.12` 和公开页面签发的 `anon_signature`。yt-dlp 2026.8.19 的播放器路径可能返回 401，而该下载按钮流程仍可匿名获取作者开放的转码文件。AlphaSense 1098728142 已下载官方 1920×1080 MP4，57.34 秒、含音轨并通过完整解码校验。因此，不能把单个播放器/API 的 401 推断为所有下载方式都需要登录。

1. 请求源视频公开页。代码使用 yt-dlp 已采用的 `?action=load_download_config` 请求：当前站返回含 `__NEXT_DATA__` 的正常视频页 HTML。脚本解析的是当前 HTML 数据，不把旧站 JSON 结构当成已验证能力。
2. 核对 `pageProps.clipId` 与源视频相同，读取 `clip.privacy.download`。明确为 `false` 时跳过，不尝试其他提取路径绕过下载设置。
3. 在无 Cookie 的公开请求中，读取该页 `viewerBootstrap.jwt` 和 `pageMetadata.clipSignature`。它们是 Vimeo 正常签发给匿名观众的访问数据；仅在内存中用于当前官方 API 请求，不能写入状态库、日志、报告或文件名。不得搬运浏览器登录/连接验证令牌或伪造签名。
4. 同网页下载按钮一样请求 `https://api.vimeo.com/videos/ID?fields=download&anon_signature=页面签发值`，使用页面的 guest JWT 和 `Accept: application/vnd.vimeo.*+json;version=3.4.12`。
5. 从返回的 `download` 数组取真实链接，优先 Original/Source（仅当官方实际提供），否则选择最高分辨率。不得根据 ID、缩略图或其他画质猜测文件路径。只接受匹配该视频的 `player.vimeo.com/progressive_redirect/download/ID/...` 及其 Vimeo CDN 跳转。
6. 后端下载完整 MP4，以 HTTP `Content-Length` 核对传输完整性，并核实分辨率、完整 FFmpeg 解码。菜单的 `size` 仅作参考：实测多份完整可解码 MP4 与菜单标注字节数略有差异，不能要求二者严格相等。每完成一个文件就登记本地待导入队列并保存报告，不等整批结束。来源字段保留无参数的 Vimeo 视频 URL，签名文件链接不持久化。配置中的并发数目前最多为 2；多个项目引用同一视频时共享带文件锁的缓存，按项目分别登记，并按内容去重。

该流程来自实际公开页面所加载的 Vimeo 官方客户端代码：[视频页匿名请求参数](https://f.vimeocdn.com/next-server/vimeo-next/_next/static/chunks/1aeqsvmfg81tm.js)、[下载列表与画质选择](https://f.vimeocdn.com/next-server/vimeo-next/_next/static/chunks/1jue3kuz11ss_.js)。这些构建文件名可能变化；接口变化时应重新检查当前页面实际引用的代码，不搬用过期签名。

```sh
python3 <skill-dir>/scripts/external_video.py --config <config> plan
python3 <skill-dir>/scripts/external_video.py --config <config> --limit 1 download
python3 <skill-dir>/scripts/external_video.py --config <config> download
python3 <skill-dir>/scripts/external_video.py --config <config> pause
python3 <skill-dir>/scripts/external_video.py --config <config> reconcile
python3 <skill-dir>/scripts/collector.py --config <config> import
python3 <skill-dir>/scripts/collector.py --config <config> import --execute
```

`plan` 只检查本地清单与文件哈希，分别列出需要下载、已有缓存待登记的项目，并显示暂停状态；不访问下载接口或写 Eagle。`download` 表示显式恢复：清除暂停标记后继续，从有效缓存恢复而不重复下载。导入仍须单独执行 `import --execute`，并核对目标库。

用户明确要求“无法下载的视频以 Eagle 链接保存”时，在配置中启用 `collection.download_failure_fallback: "bookmarks"`。Vimeo 页面明确声明 `privacy.download: false` 时立即标记为可回退书签；其他逐条失败按唯一下载尝试事件累计次数；报告保留 run_id 和 attempt_id，同一事件重放不增加次数。达到 `download_failure_fallback_after` 后标记。不要把限流、连接验证或其他平台级 `blocked` 状态当作视频文件成功；即使保存链接，也报告阻塞状态。此标记允许 `files` 模式仅将该条 Vimeo URL 作为回退书签导入，其余仍按文件模式处理。下载器应跳过已标记的终态回退项，除非用户重新要求尝试文件。

Eagle 书签可能先建成 URL 条目、稍后才生成预览。导入器按 `/api/item/thumbnail` 回传路径轮询，并对路径解码；必要时用 Eagle 官方 `/api/item/refreshThumbnail` 请求刷新一次后继续等待。刷新不会重新提交书签。达到等待上限仍无法解码时，保留 `uncertain` 状态并停止批次；下一次先对账已有 ID 与 URL，核实项目关联后只刷新/验收。仅在全库确认条目不存在且用户明确要求重试时，按恢复指南最多重提一次。

`pause` 写入暂停请求，下载器停止派发新任务；当前最多两个文件完成后逐项保存并退出。SIGINT/SIGTERM 采用同样的停止派发和保存机制，正常暂停不输出错误堆栈。暂停请求不等于进程已经退出，可用 `.download.lock` 是否已释放来确认。

`reconcile` 完全离线：核对已有文件的路径、哈希和解码结果，再登记本地待导入状态，补救断电或旧版本中断留下的“已有文件、未登记”情况。它不会调用下载函数、切换资源库或写 Eagle，也不会解除暂停。只能在用户要求恢复后再次执行 `download`；不能借调试为由自动恢复。

## 已有官方文件链接

若已取得官方菜单中的真实链接，可用 `--official-links <json>`。只处理该 JSON 指定的任务视频，带签名地址会过期，不保存到最终来源字段。示例：

```json
[{"source_url":"https://vimeo.com/视频ID","downloads":[{"url":"官方下载菜单实际提供的完整下载链接","width":1920,"height":1080}]}]
```

## 其他提取路径与失败状态

官方下载流程未提供数据时，才尝试 yt-dlp。优先读取 Vimeo 公开页 `twitter:player` 声明的完整播放器 URL（包括页面提供的 `h` 参数）；若 Vimeo 公开视频页返回 404/410，而来源案例页明确嵌入 `player.vimeo.com/video/{同一ID}`，可使用从案例页提取并校验过的播放器 URL。以实际案例页作 Referer，选 `bv+ba/b`，不转码、不放大，必要时无损合并到 MP4/MKV。不得从数字 ID 推测下载 URL。来源：[yt-dlp 维护者建议](https://github.com/yt-dlp/yt-dlp/issues/5492)、[当前 Vimeo 提取器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/vimeo.py)。

错误同时保留请求阶段：公开页、官方下载 API、播放器或实际媒体文件。API 的 401 可用正常公开页重新签发的匿名访问数据重试一次，仍拒绝则记录认证/API 拒绝；不读取浏览器凭据。403 仅表示访问拒绝，只有实际验证页面/信息才标为连接验证；429 标为限流并停止派发，不连续请求剩余视频。作者关闭下载、DRM、普通网络错误分别记录。明确的单视频下载关闭可跳过；平台级阻塞暂停本批并保存已经成功的文件。

HTTP 200 既可能是验证页，也可能是含新站数据的正常 HTML，必须检查内容。视频 ID 或 URL 中恰好包含 401/403 不能被当成 HTTP 状态。任何路径都须完整解码成功后才写入成功缓存；不得把播放器可播、MediaSource blob 或下载器返回了路径当成校验完成。

Vimeo 官方说明，观众下载须由作者开启；公开视频原始源文件要求登录，匿名观众可下载官方提供的转码版本。Vimeo 网页没有内置批量下载功能；此脚本逐条调用每个获准视频的官方下载流程。[下载说明](https://help.vimeo.com/hc/en-us/articles/12426502581265-How-to-download-a-video-on-Vimeo)、[源文件与转码版本](https://help.vimeo.com/hc/en-us/articles/25080921162001-Troubleshooting-Source-file-unavailable-to-download)。YouTube 按用户要求暂缓，不声称已支持。

书签通过公开 oEmbed 海报建立预览，按项目文件夹与 URL 核实。`uncertain` 条目只对账；海报请求在提交 Eagle 前完成，不能把书签导入成功算作视频下载成功。

重新检查已有回退视频时，显式运行 `external_video.py --config <config> --recheck-fallbacks plan`，用户要求重新尝试文件后再将 plan 改为 download。该选项仅作用于本次命令，不删除原链接、不修改作者下载权限。已有视频记录的恢复仍按其本地文件和队列验收，不重复下载。若获取到文件，其验收完成后记录书签的替代关系；下载完成本身不能算入库完成。
