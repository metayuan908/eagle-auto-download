# Eagle 自动化下载

当前版本：**1.5.0**。供 Codex 使用的品牌案例采集 Skill，通过 Eagle 本机接口整理并导入图片、动图和视频。

按项目正文采集素材；遇到新站点时，先检查页面结构并适配采集规则。

## 环境与运行

面向 macOS、Python 3.10+、Eagle 4.0；当前使用 `fcntl`，不支持 Windows。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r skills/eagle-auto-download/scripts/requirements.txt
.venv/bin/python -m unittest discover -s skills/eagle-auto-download/tests -v
```

从 [SKILL.md](skills/eagle-auto-download/SKILL.md) 开始，运行步骤见 [运行指南](skills/eagle-auto-download/references/operation.md)。复制 [配置模板](skills/eagle-auto-download/references/config.example.json) 到自己的采集工作目录，填写目标资源库与本地路径；执行前核实目标库和导入计划。

## 采集规则

- 目录使用“工作室 → 简短中英文项目名称”，行业标签与中文简介依据项目正文填写。
- 静态 WebP 转 JPG；动画保留原件并生成 JPG 预览；安全校验后保留 SVG。
- Vimeo 获取可下载视频；已启用失败回退时，将无法下载的视频保存为带预览的 Eagle 项目链接。
- 通过 Eagle ID、原件哈希和项目关联对账；不确定的提交先核实，避免重复导入。
- 写入前核对资源库，使用本机 API，不直接修改资源库内部文件。

## 安装、迁移和更新

Skill 位于 `skills/eagle-auto-download/`。可使用 Codex 的 skill-installer 从该仓库路径安装；安装或迁移已有版本前应保留旧版并比较改动。

版本信息见 [VERSION](skills/eagle-auto-download/VERSION) 和 [CHANGELOG.md](skills/eagle-auto-download/CHANGELOG.md)。需要可校验的离线迁移包时，使用 `skills/eagle-auto-download/scripts/migrate.py export`，再按 [版本与迁移](skills/eagle-auto-download/references/migration.md) 校验和安装。日常更新不自动生成桌面压缩包。

仓库只包含 Skill 源码、说明、模板和模拟测试，不包含资源库、下载素材、个人配置、任务队列、凭据或定时任务。测试通过不代表真实素材已经导入。
