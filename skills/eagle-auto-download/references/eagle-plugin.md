# 可选的官方 Eagle 插件接口

只在需要库内检索、读取当前选中项、计数、批注或插件能力诊断时读本页。它不替代网页正文判断、yt-dlp 下载、完整视频解码或原生入库验收。

Eagle 原生 HTTP API 默认 41595，当前入库执行器用它核对目标库、保存来源 URL、提交文件、回读 Eagle ID 与只读校验库中原件哈希。官方 MCP Server 插件默认 127.0.0.1:41596，需 Eagle 和插件同时运行；Skill 方式通过 HTTP 调用工具，不强制配置 MCP 客户端，也不要求另装官方 Skill 才能用接口。

## 实时只读诊断

```sh
python3 <skill-dir>/scripts/eagle_plugin.py diagnose
python3 <skill-dir>/scripts/eagle_plugin.py tools
python3 <skill-dir>/scripts/eagle_plugin.py help item_get
python3 <skill-dir>/scripts/eagle_plugin.py call item_get --json '{"ids":["实际 Eagle ID"],"limit":1}'
```

该脚本只允许查询工具；不启动服务、不启用关闭的工具、不安装 AI Search、不修改资料库。服务端口不同可显式传 --server。未安装或未运行插件时仍沿用原有 41595 流程，不为采集任务自动增加依赖。脚本需要 Python 标准库，不依赖 Shell 中必须能找到 node。

通过 /api/status 和 /api/tools/list 检查实际启用状态与单工具 schema。官方 CLI 的 list 输出来自脚本内嵌元数据，不能证明所有工具已启用。AI 搜索另外要求 AI Search 插件 installed / ready；智能文件夹工具也可能被用户关闭。不把缺少依赖描述成“无搜索结果”。

## 官方 0.3.1 实测的接入差异

- item_get、item_count、item_get_selected、标签与目录查询可作为读取补充。增量标签／目录关联工具及视频时间点批注有使用价值，但仅在用户当前任务授权相应修改时使用，保留原有项目与共享素材规则。
- 随附 SKILL.md 的旧 tag_merge 示例写 sourceTags / targetTag，而实际 schema 为 `operations:[{"source":"旧标签","target":"保留标签"}]`。这会改全库标签，不能为了验证示例对真实库执行。以运行时 schema 为准。
- HTTP 200、CLI 退出码 0 和顶层 success:true 均可能不足以证明批量操作成功。参数验证失败会返回 success:false；批量接口可能把各项失败包在顶层成功响应内。检查业务状态、逐项 success、itemId 和计数，并做实际内容与元数据回读。诊断脚本对这些失败返回非零退出码。
- item_add 的 path 分支可接本地文件，但 0.3.1 的每项参数没有来源 URL；item_update 同样未暴露来源 URL。不要为了接官方插件而丢掉项目来源，继续使用现有原生导入链路。以后版本可重新检查，不把此限制永远硬编码为 Eagle 能力限制。
- 官方工具包装本身不等于目标库绑定、原件哈希、uncertain 恢复和共享元数据验收。这些仍由当前采集器保证。不要直接改写库内 metadata.json，也不要安装插件或合并标签来测试采集 Skill 更新。

本机检查时插件有 37 个工具，其中 31 个启用、6 个智能文件夹工具关闭，AI Search 未安装；这是 2026-10-26 的状态，后续应重新诊断，不硬编码其数量和安装结果。官方介绍：[Eagle Skill / MCP](https://eagle.cool/blog/post/eagle-plugin-mcp-skill)。
