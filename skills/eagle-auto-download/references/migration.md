# 版本与迁移

`VERSION` 和 `SKILL.md` 的 `metadata.version` 必须一致，格式为 `主版本.次版本.修订号`。v1.0.0 是首次编号的迁移基线；当前版本以两个版本文件中的相同值为准。

- 修复问题或调整规则：增加修订号，例如 1.0.0 → 1.0.1。
- 新增兼容功能或适配网站：增加次版本号，例如 1.0.1 → 1.1.0。
- 不兼容的配置或状态结构变更：增加主版本号，并说明迁移办法。

每次更改 Skill 后，同步更新两处版本及 `CHANGELOG.md`，在当前电脑完成相应测试并同步安装；仅用户要求导出或迁移时生成新迁移包，不因版本更新自动打包到桌面。升级 Skill 不迁移任务数据、不启动采集，也不解除既有暂停。

Skill 附带 `tests/` 回归测试。安装依赖后，在任意工作目录运行（自定义 CODEX_HOME 时替换实际路径）：

```sh
python3 -m unittest discover -s ~/.codex/skills/eagle-auto-download/tests -v
```

测试在临时目录模拟采集、导入和迁移，使用模拟接口，不访问实际 Eagle 资源库；测试通过不等于真实导入已获准或已完成。

## 导出

仅在用户要求迁移时，从当前已安装 Skill 导出；先准备用户指定的输出目录。以下路径是 Codex 默认安装位置；设置了 `CODEX_HOME` 时，使用对应目录。

```sh
python3 ~/.codex/skills/eagle-auto-download/scripts/migrate.py export --output-dir ~/Documents/Codex/Eagle迁移/outputs
```

生成 `Eagle自动化下载_v版本号/` 和同名 ZIP。包含完整可安装 Skill、版本记录、迁移说明、SHA-256 清单；排除缓存及运行数据。不包含 Eagle 资源库、已下载文件、个人配置、SQLite 队列、暂停标记、凭据、Python 环境或定时任务。源代码无需绑定原电脑用户名。

## 到另一台电脑安装或迁移回来

解压 ZIP，进入解压后的文件夹，先验证再安装：

```sh
python3 eagle-auto-download/scripts/migrate.py verify
python3 eagle-auto-download/scripts/migrate.py install
```

工具默认安装到 `$CODEX_HOME/skills/eagle-auto-download`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills/eagle-auto-download`。`--skills-dir <目录>` 可指定其他安装根目录。它核对版本、文件清单和 SHA-256，在替换旧版前将旧目录完整备份到安装根目录旁的 `skill-backups/eagle-auto-download/`，然后切换到新版；不会写入 Eagle。

目标与包版本相同但内容不一致时拒绝覆盖，避免一台电脑的改动丢失；先比较变更、合并并增加版本号。版本较旧时默认拒绝降级；仅在用户明确要求回滚后使用 `install --allow-downgrade`。无版本的旧 Skill 可安装升级，仍会先备份。两台电脑都改过时不能仅根据版本大小认定可以丢弃另一份改动，应先查看各自变更记录并合并。

其他电脑修改完成后，在那台电脑同步版本及变更记录，再运行 `export`；把生成的新 ZIP 带回本机并按同一流程安装即可。不用手动覆盖同名目录。迁移过程中保留旧包，校验清单用于检查文件完整性，不提供来源签名认证。

安装后，新开一轮 Codex 会话确认 Skill 可被发现。运行采集前，使用 Python 3.10+ 建立本机虚拟环境并安装 `scripts/requirements.txt`；从配置模板新建本机配置，填写实际 Eagle 资源库路径。当前脚本使用 macOS 的 `fcntl`，未验证 Windows 支持。若要续传本机未完成任务，必须另外迁移工作目录和素材文件，并按旧记录对账、修正路径；不能把仅迁移 Skill 当成任务状态已同步。

v1.4.0 对任务状态采用兼容扩展：原 assets/projects/settings 保留，首次访问非空旧库前自动创建 SQLite backup，再增加身份、关联与尝试日志表。迁移 Skill 本身不会访问或升级任务库；首次运行任务命令才检查状态结构。升级不替用户接受共享素材元数据策略，也不将旧 uncertain 自动标为完成。
