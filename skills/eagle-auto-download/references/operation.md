# 运行指南

执行器目前面向 macOS、Python 3.10+ 与 Eagle 4.0，使用 `fcntl`，Windows 尚未支持。运行前确认 Eagle 本机 API 可用，并将 [配置模板](config.example.json) 复制为用户工作目录中的本地配置，填入站点和 `expected_library_path`。不要把个人资源库路径或凭据写入共享模板。

通过本 Skill 目录中的 `scripts/collector.py` 执行命令；可先建立虚拟环境并安装 `scripts/requirements.txt`。示例中的 `<skill-dir>` 指已安装的 Skill 目录，`<work-dir>` 指用户指定的采集工作目录：

```sh
python3 <skill-dir>/scripts/collector.py --config <work-dir>/config.local.json check-config
python3 <skill-dir>/scripts/collector.py --config <work-dir>/config.local.json inspect-eagle
python3 <skill-dir>/scripts/collector.py --config <work-dir>/config.local.json discover
python3 <skill-dir>/scripts/collector.py --config <work-dir>/config.local.json inspect-project --project-url <project-url>
python3 <skill-dir>/scripts/collector.py --config <work-dir>/config.local.json collect --project-url <project-url>
python3 <skill-dir>/scripts/collector.py --config <work-dir>/config.local.json import
```

`discover` 列出项目；`baseline` 记录已知项目但不下载历史素材；`scan` 找出新增项目；`run` 下载并保留待导入队列，`run --execute` 还会尝试导入。`status` 查看本地完成摘要，`status --details` 查看逐条原始状态。默认 `import` 只显示计划；只有目标资源库路径核对正确且用户已授权本次写入时，才使用 `import --execute`。

如果 `/api/library/info` 返回的活动库路径与 `eagle.expected_library_path` 不同，先判断用户是否已授权切换到配置中的这个目标库。已有授权时，通过 `POST /api/library/switch` 提交 `{"libraryPath":"<expected_library_path>"}`，再调用 `/api/library/info` 复核完全一致后继续；这一步用本机 HTTP API 完成，无需 Eagle 前端操作。没有授权时停止写入并询问一次，不能自行切库。切换或复核失败时保持队列不变并报告错误；不要要求用户手动来回切库后再启动任务。

准备元数据与导入前必须执行 [导入验收](acceptance.md)。`preflight` 离线报告缺项，实际导入强制执行同一检查；旧队列缺少审核证据时不再直接沿用英文标题和空简介。`pause-import` 持久暂停导入，`resume-import` 仅在用户要求继续后解除暂停。优化 Skill 或测试通过不恢复真实任务。

队列包含多个独立项目且仅部分项目已完成审核时，可对指定项目范围执行预检和导入：

```sh
python3 <skill-dir>/scripts/collector.py --config <work-dir>/config.local.json preflight --project-url <project-url>
python3 <skill-dir>/scripts/collector.py --config <work-dir>/config.local.json import --project-url <project-url>
python3 <skill-dir>/scripts/collector.py --config <work-dir>/config.local.json import --execute --project-url <project-url>
```

可重复提供 `--project-url` 处理多个项目。范围内项目仍须通过全部适用检查；文件夹重名按整个状态库检查。范围外项目自身的元数据缺项不会阻塞这次范围导入。未指定范围时仍检查整个待导入队列。

配置中 `paths` 的相对路径以配置文件父目录的父目录为基准。`root_folder: null` 时，工作室文件夹位于库顶层，项目文件夹位于工作室之下。状态数据库与下载目录应保持在用户指定的工作目录中，以便失败后续跑和对账。

若用户明确选择下载失败后保留 Vimeo 链接，可设 `collection.download_failure_fallback: "bookmarks"`，并将 `download_failure_fallback_after` 设为允许的独立尝试次数。默认模板关闭此选项。作者明确关闭下载时不必等待重试；其他逐条失败达到次数后才回退。下载器会在素材记录上保留回退原因，`files` 模式只选择这些明确标记的书签。

Interbrand、Wolff Olins、Saffron、2x4 和 LANDOR 的 `project_metadata` 都以项目 URL 为键，包含 `display_name`、`category_tags`、`industry_tags` 和可选 `summary`。项目名使用简短中英文组合（如 `LEGO 乐高`）；标签结合适用大类与具体行业关键词；只根据页面实际提供的介绍填写中文简介。素材注释仅放中文项目简介，来源和处理信息留在本地状态库与报告。

定期采集时先 `scan`，仅处理新增项目：对每个新案例运行 `inspect-project`，核实名字、标签、简介和明确的项目 Logo，再运行 `run --execute`。没有新增时仍可运行 `run --execute`，以对账并导入未提交队列；uncertain 默认不重发，恢复细节见 [对账与恢复](recovery.md)。不要重新初始化已有基线来掩盖待处理项目。

待导入文件必须仍可从本机原路径读取。队列优先按保存或返回的 Eagle ID、原件哈希及项目关联对账，名称和 URL 只作辅助，兼容旧标记。`uncertain` 查无匹配时保留状态；只有用户明确要求重试才按 [对账与恢复](recovery.md) 限定单条，最多重提一次。不能手工重置状态或尝试次数来绕过保护。

Interbrand 适配器从项目正文读取明确的素材链接，并仅把正文 `heroGroup.brandLogo` 识别为项目 Logo。Wolff Olins 适配器收集项目页主体中的 Contentful 原始图片与视频链接，排除“Related Projects”推荐内容；内容管理系统另提供的 ZIP 包不下载。Wolff Olins 只在文件名明确标注 Logo、Wordmark 或 Logotype 时标记为项目 Logo。Saffron 适配器读取 Next.js `__NEXT_DATA__` 中的 `pageBlocks` 和当前案例顶部 `cover`，排除推荐卡片；CMS 直链图片和视频按原格式采集。Vimeo 视频文件获取参阅 [外链视频下载](external-video.md)，使用最高可用画质和音轨，无损合并并完整校验。外链书签使用公开 oEmbed 海报生成预览；书签不算已下载视频。Eagle 未能核实的书签保留 `uncertain`，只对账、不重发。目录和封面通过 Eagle 本机 API 操作，不直接改库内部文件。封面接口依赖 Eagle 4.0 本机脚本接口，其他版本需先验证。SVG 安全校验后保留 SVG 原格式；静态 WebP 转为 JPG，动画 WebP 保留动画并另生成 JPG 预览。

2x4 适配器从 `/filter/all/` 的完整项目卡片归档发现案例，当前页面通常列出约 200 个项目；从每个 `/work/{slug}/` 主案例文章提取 `srcset` 最大尺寸图片、站点直链视频与 Vimeo 播放器外链。Related Projects 实际嵌套在主 article 内，排除 aside 和嵌套 article，不能只截到主 article 结束。卡片标题和客户名仅作为命名素材，最终名称须整理成简短中英文组合；原站正文保存在 `source_description`，中文简介另填。Sector 分类映射为中文大类及行业标签，配置未提供覆盖值时保留自动分类。旧版队列需重新提取并对账，不能直接更改适配器版本标记。遇到 429 时遵循 `Retry-After` 并退避重试；超大图仍受 Pillow 解码保护，不应为单个素材关闭像素上限。项目页结构变化或归档项目数异常时停止并检查适配器。

新工作室网站需先检查并扩展适配器。Vimeo 的公开非 DRM 分段媒体由 yt-dlp 处理；DRM、YouTube 和自动 Eaglepack 导出未适配。列表分页、页面结构或本机 API 行为变化时，先检查并更新适配器，不猜测资源地址。下载中断会记录失败并允许重新运行；CMS 直链目前不支持 HTTP 断点续传，Vimeo 官方 MP4 会复用已校验的完整文件缓存，未完成的 MP4 重新获取官方地址下载；yt-dlp 分段媒体可续跑。

共享素材策略、兼容状态库升级和完成统计见 [对账与恢复](recovery.md)。默认 `shared_metadata_policy: review`，只有用户接受保留共享条目原元数据时才设为 `preserve_existing`；升级或预检通过均不代表历史项目已修复完成。

LANDOR 适配器使用英文站 `/en/our-work/` 的 `caseStudiesListing.items` 发现项目；从当前 `PageData.contents` 的 Hero、Image、Video、双图和三图布局读取 `cms.landor.com/media/` 原件。核对面包屑中的案例 URL，保留正文 Text 与 ProjectInfoBody，不采集 TeamBody、Footer 的 Up Next 或视频 placeholder。未知内容块停止，不能递归扫描整份 Next.js 数据。静态图片和原生视频直接下载并验证；原件回执核对 URL、大小和哈希后才复用缓存，最多两个并发下载。简介、分类和中英文名称需按每个案例实际内容整理，不默认填已审核。
