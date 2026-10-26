# 项目整理与导入验收

## 离线整理

从 `inspect-project` 的 `description_source` 或实际项目页正文读取介绍，排除相关推荐、导航和团队名单。将整理结果写入配置的 `project_metadata`，键使用采集队列里的准确项目 URL。原文和中文简介分别保存：

```json
{
  "https://example.com/work/project": {
    "display_name": "Mathaf 阿拉伯现代艺术博物馆",
    "category_tags": ["文化"],
    "industry_tags": ["博物馆", "艺术机构"],
    "description_status": "present",
    "description_source": "从实际项目页复制的介绍原文，不能用本示例占位文字。",
    "summary": "根据原文整理的中文项目简介，不能用本示例占位文字。",
    "media_scope_reviewed": true
  }
}
```

`media_scope_reviewed` 表示已检查当前案例的素材边界，不是默认值。文章归档型 新采集记录会保存 `source_description`、当前 `adapter_revision` 标记；原文由适配器保留，不被配置中的空值覆盖。其他适配器可用 `description_source` 保存检查过的原文。`prepare` 将配置中的审核字段登记到本地队列；它不会自动翻译或把未知状态标成审核完成。

原站无介绍时，核实后用 `description_status: absent`，不填写原文与简介。有原文却无中文简介时必须继续整理，不能当成“没有介绍”。中文简介允许保留品牌专名；不得夹带英文原文整段、来源链接、下载记录或采集标记。

名称要简短且含中文和英文；同客户不同案例保留区别，例如 `Miu Miu Jazz Club 缪缪爵士俱乐部`。名称超过 80 字符需缩短。标签采用行业大类加具体关键词，不用工作室名、客户名、品牌参考或视频等媒体类型代替行业标签。确实无法判断大类时保留空数组并记录 `category_status: unknown`，不得凑标签。

Vimeo 模式在 `collection.external_video_mode` 中明确选择：

Adobe CCV 当前只适配 files；正文中暂存的外链记录表示待下载视频，不能直接导入为书签或计为文件完成。具体命令见 [专用流程](project-media.md)。

| 用户要求 | 配置 | 验收内容 |
| --- | --- | --- |
| 获取视频文件 | `files` | 对应视频文件完整校验并进入队列；书签不导入 |
| 仅保留外链 | `bookmarks` | URL 书签、中文注释、标签及可解码预览 |
| 文件和外链都保留 | `both` | 两类分别验收、分别计数 |

模式依据本次及此前用户要求设置。默认不把文件下载模式降级为书签。用户明确要求无法下载的视频保存为 Eagle 链接时，可额外设置 `collection.download_failure_fallback: "bookmarks"`；作者明确关闭下载时回退该视频链接，其他下载失败须累计达到 `download_failure_fallback_after` 次独立尝试才回退。回退记录保留失败原因，并按 URL 书签、中文注释、标签及可解码预览验收；不要把它统计成视频文件。

## 强制检查与写入

```sh
python3 <skill-dir>/scripts/collector.py --config <config> preflight
python3 <skill-dir>/scripts/collector.py --config <config> import
python3 <skill-dir>/scripts/collector.py --config <config> import --execute
```

`preflight` 不访问网络或 Eagle，检查队列中的中英文名称、简介来源与中文内容、标签审核、归属审核、旧版适配器、同名目录冲突、视频模式、本地文件存在性和哈希。报告写入 `reports/preflight.json`；不合格时返回非零退出码。默认 `import` 仍可查看有问题的计划，但带有 `preflight.ready: false`；实际执行会在连接 Eagle 之前拒绝整批。

多个项目并行整理时，`preflight --project-url <URL>` 与 `import --project-url <URL>` 可限定本次检查及导入范围，参数可重复。范围预检仍验证目标项目的全部待导入素材，并用整个状态库检查项目文件夹重名；它只隔离其他项目的独立缺项。范围预检保留已导入视频作为依赖证据。实际导入仍逐项回读，新提交失败停止当前批；恢复时已有 uncertain 查无匹配只登记待核实，可继续其他独立记录。没有指定项目范围时沿用全量预检和全量导入。

这类自动检查能发现缺项，不能代替事实审核和翻译质量检查。先检查代表案例的原文、名称、标签、正文素材及外链，再执行已有授权的写入。普通条目回读名称、URL、中文注释、标签和文件夹，文件另核实 Eagle 原件哈希。跨项目共享条目按 [对账与恢复](recovery.md) 保留原元数据并登记审核或复用状态；书签另读取 Eagle `/api/item/thumbnail` 返回的路径，只读验证图片可解码。Eagle 预览异步生成时先轮询；仍缺失则调用 `/api/item/refreshThumbnail` 并等待后复核。空白占位或无法验证的预览不能视为完成。首次失败或未核实的书签会停止后续提交，保留状态供对账；已创建条目保持 `uncertain`，默认恢复只对账与刷新预览；明确重试授权的例外及持久次数限制见 [对账与恢复](recovery.md)。

## 暂停与旧记录

```sh
python3 <skill-dir>/scripts/collector.py --config <config> pause-import
python3 <skill-dir>/scripts/collector.py --config <config> preflight --include-imported
```

暂停命令写入与状态库同目录的 `.import-paused` 标记，不等待长任务持有的数据库锁。当前已发出的请求可能完成；必须确认旧进程退出或 `.operation.lock` 已释放才报告已暂停。执行器在每条提交前和每次 Eagle 写入前检查标记。用户明确要求继续导入后，运行 `resume-import` 解除标记，它先核实操作锁可用且不会自动启动导入。

`preflight --include-imported` 审计历史记录，输出 `reports/preflight-all.json`，不改 Eagle。旧队列缺少审核字段会被拦截；先检查原页并补齐证据，不批量填 `true` 或 `absent`。旧版 文章归档型 的相关推荐视频可能被归入当前项目，需用修正后的适配器重新提取清单，按 URL/哈希对比，明确列出受影响条目。

已提交、复用、元数据待审核或 `uncertain` 记录保留原来的名称、顺序和 Eagle ID 用于对账。重新 `prepare` 不能覆盖这些记录的命名、标签、简介或归属信息；需要变更时先用 SQLite backup 保存状态、用 API 读取旧项目与素材映射，再根据现有授权制定迁移操作，修改后逐项回读。不要删除素材、直接修改 Eagle 库内部文件，或为修复元数据重复导入。暂停期间仅优化 Skill 和离线测试，不以验证为由解除暂停。

`preflight.ready` 表示离线条件允许执行，不是入库完成。单条范围仍保留同项目全部依赖证据及全局重名检查；项目完成程度看 `scope_summary`，其含义及占位链接的替代关系见 [对账与恢复](recovery.md)。
