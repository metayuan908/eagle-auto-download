# 项目正文与嵌入视频

## 站点配置与正文审核

每份配置仍只有一个站点。同一工作室的官网和 Behance 可使用独立配置与状态库，设置相同 studio 与预期 Eagle 库，在原工作室下归档。不要复用绑定其他资源库的 Eagle ID。

官网配置使用与正文结构匹配的适配器 ID，`studio` 填用户指定的归档名称，`list_url` 填用户提供的项目列表页；具体端点由适配器维护，不在说明中列举目标官网。

Behance 使用 `id: behance`，`studio` 填用户指定的归档名称，`list_url` 填用户提供的公开作者主页；`skip_project_urls` 默认空列表。只支持公开案例。不能把作者不匹配、登录限制或空列表当成已遍历全部作品。

沿用运行指南的 discover → inspect-project → 逐案例 project_metadata → collect。这两个新适配器的 fetch_html 返回公开 CMS / 项目 API 的 JSON 文本；不要求将每页渲染成 HTML。名称、行业标签与中文简介仍须根据实际正文整理；模块提取成功不自动把 media_scope_reviewed 标为 true。

公开 CMS 型适配器通过其已核实的列表 API 发现项目，按实际 pageNum / totalPage / total 遍历并核对重复 ID；详情 `/works/ID` 必须身份相符且未隐藏／删除。只读取详情 albumPics 和 video，不将 banner、移动端裁剪图、奖项或首页推广混入案例。

Behance 从主页 HTML 实际给出的 after 链接继续分页，不自行猜下一页，循环与重复页面停止。项目正文使用公开的 `/v2/projects/ID?client_id=BehanceWebSusi1`，核对项目 ID 及配置主页的 owner。只读取 modules 的 image、text、media_collection、video 和已适配的 Vimeo embed；不递归扫描 covers、相关推荐、头像或其他页面数据。未知模块停止供检查。图片优先 API 明确提供的 original / source，其次按声明尺寸选择非 WebP／非 still 变体；原地址不可访问时报告失败，不自行替换路径或宣称已取最高画质。

## 补采与跨站同案

用标题、产品系列及正文画面对照官网与 Behance。同品牌、同客户或名称相似不代表同一案例；`_` 等空 slug 也不能作为已收录证据。新站点的 Eagle 已收录检查采用准确来源 URL；状态库已有身份和哈希仍按恢复指南对账。

人工确认官网已有的同案，可把准确的 Behance 项目 URL 填入站点的 skip_project_urls。保留对照依据在任务文件中，不将一次任务的排除名单、品牌名单或历史项目数量硬编码进 Skill。此排除项只影响发现与新项目扫描，不删除旧队列或 Eagle 内容。内容同字节跨项目复用仍按 shared_metadata_policy，不用新提交解决元数据冲突。

## Adobe CCV 文件流程

Behance 的 video 模块只显示播放器地址，不足以判断视频不可下载。2026-10-26 的公开正文快照含 `www-ccv.adobe.io/v1/player/ccv/ID/embed?api_key=behance1`；播放器 HTML 的 source 及 ccv$serverData 公布 HLS master.m3u8 与 MP4，yt-dlp 的 HTML5 提取器可解析。实际任务的 18 个模块均已下载并完整解码、导入验收，但不能把这些历史数量当作未来成功保证。

适配器把无查询参数的稳定播放器 URL 登记为待下载的外链记录，保留正文模块 ID 与实际项目 ID；它不算已下载文件。设置 collection.external_video_mode 为 files，然后执行：

```sh
python3 <skill-dir>/scripts/external_video.py --config <config> plan
python3 <skill-dir>/scripts/external_video.py --config <config> --limit 1 download
python3 <skill-dir>/scripts/external_video.py --config <config> download
python3 <skill-dir>/scripts/collector.py --config <config> preflight
python3 <skill-dir>/scripts/collector.py --config <config> import
python3 <skill-dir>/scripts/collector.py --config <config> import --execute
```

下载使用 yt-dlp bv+ba/b，需要时无损合并，不转码。必须解析到带尺寸的有效画质清单，媒体 URL 属于同一 CCV ID 的 Adobe CDN；不把只剩低清 MP4 的不完整提取当成最高画质。签名链接与临时播放 Cookie 只在当前进程内使用，不写入 info JSON、状态库、回执或最终来源字段；缓存只保存稳定来源、文件路径、大小、质量与 SHA-256。每个视频完整解码后原子保存回执、登记待导入队列，再继续调度。不同案例引用同一视频时共享带文件锁的缓存，项目元数据仍分别保留。

沿用外链下载器的 pause / reconcile；最多两路下载，不跳过失败分片，未完成的分段文件可续跑。collection.external_video_retries 控制单视频完整尝试次数（默认 3，1–5）；普通连接失败有限重试，每次重新解析稳定播放器 URL。认证、访问拒绝、验证和限流按原外链流程停止，不搬用浏览器登录凭据。Adobe 当前不提供书签导入或下载失败书签回退，files 模式缺视频时仍保持未完成。

collection.external_video_proxy 为 null 时保留系统网络设置；只有诊断确认代理连接异常而直连可用时，才显式设为 `""`，也可设本机实际代理 URL。不硬编码端口，不因为一次 TLS EOF 自动更改全局代理设置。此选项当前只作用于 Adobe 视频下载，不更改 Vimeo、图片请求或应用设置。

播放版本最高 1080p 不意味着原视频只有 1080p；API 的 inputWidth / inputHeight 也不代表可下载的上传原件。以实际选中且完整解码的文件尺寸报告。

官方接口未适配或持续失败时保存待办和错误，不自动改用 Downie。用户选择 Downie 后可参考其官方自动化接口，仍需获取实际文件、完整解码与 Eagle 回读，不能把投递下载队列算作完成。
