#!/usr/bin/env python3
"""Versioned offline Skill export, integrity verification and backup-first install."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SKILL_ID = 'eagle-auto-download'
IGNORED = {'.DS_Store', 'Thumbs.db', '__pycache__', '.pytest_cache', '.git'}


def files(root):
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(part in IGNORED for part in relative.parts) or path.suffix in ('.pyc', '.pyo'):
            continue
        if path.is_symlink():
            raise ValueError('迁移内容不能包含符号链接：' + str(relative))
        if path.is_file():
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def version_of(skill, allow_unversioned=False):
    path = skill / 'VERSION'
    if not path.exists() and allow_unversioned:
        return None
    version = path.read_text().strip()
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version):
        raise ValueError('VERSION 必须是主版本.次版本.修订号')
    frontmatter = (skill / 'SKILL.md').read_text().split('---', 2)[1]
    match = re.search(r'^  version:\s*[\"\']?([0-9.]+)', frontmatter, re.M)
    if not match or match.group(1) != version:
        raise ValueError('VERSION 与 SKILL.md metadata.version 不一致')
    return version


def instructions(version):
    return f'''# Eagle自动化下载 v{version} 迁移包

适用于 macOS、Python 3.10+、Eagle 4.0。本包包含 Skill，不包含资源库、下载素材、个人配置、队列、暂停标记、凭据、Python 环境及自动任务。

解压后进入本文件所在目录，在终端依次运行：

```sh
python3 eagle-auto-download/scripts/migrate.py verify
python3 eagle-auto-download/scripts/migrate.py install
```

默认安装到 `$CODEX_HOME/skills/eagle-auto-download`，未设置 CODEX_HOME 时为 `~/.codex/skills/eagle-auto-download`。安装前完整备份旧版；同版本内容不同会停止，较旧版本默认禁止覆盖。它不会调用 Eagle，也不会开始下载或导入。

安装后新开一轮 Codex 会话，按 Skill 运行指南建立本机 Python 环境、安装锁定依赖，并填写实际资源库路径。

在其他电脑更新后，先同步 Skill 中 `VERSION`、`SKILL.md` 的 `metadata.version` 和 `CHANGELOG.md`。修复与新增功能按语义版本递增，不兼容变更用主版本号。两台电脑都改过时先合并，不能只按版本号覆盖。

用户要求迁移时，先准备输出目录，再导出更新版（在当前安装目录执行工具）：

```sh
python3 ~/.codex/skills/eagle-auto-download/scripts/migrate.py export --output-dir ~/Documents/Codex/Eagle迁移/outputs
```

自定义 CODEX_HOME 的电脑请使用实际安装路径。将新 ZIP 带回本机，解压后再次 verify 和 install。完整流程见 `eagle-auto-download/references/migration.md`，变更记录见 `eagle-auto-download/CHANGELOG.md`，文件 SHA-256 见 `manifest.json`。
'''


def export_skill(source, output):
    version = version_of(source)
    allowed_dirs = {'agents', 'scripts', 'references', 'assets', 'tests'}
    allowed_files = {'SKILL.md', 'VERSION', 'CHANGELOG.md'}
    source_files = files(source)
    for name in source_files:
        path = PurePosixPath(name)
        if (path.parts[0] not in allowed_dirs and name not in allowed_files) or (
                path.name.endswith(('.local.json', '.sqlite', '.sqlite-wal', '.sqlite-shm', '.lock')) or
                path.name.startswith('.env')):
            raise ValueError('发现非迁移文件，先移出 Skill 目录：' + name)
    output = output.expanduser().resolve()
    if not output.is_dir():
        raise ValueError('输出目录不存在：' + str(output))
    package = output / ('Eagle自动化下载_v' + version)
    archive = output / (package.name + '.zip')
    if package.exists() or archive.exists():
        raise ValueError('同名迁移包已存在，不覆盖；请检查变更并增加版本号或选择其他输出目录')
    with tempfile.TemporaryDirectory(prefix='eagle-package-') as temporary:
        staged = Path(temporary) / package.name
        skill = staged / SKILL_ID
        for name in source_files:
            target = skill / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        (staged / '迁移说明.md').write_text(instructions(version))
        manifest = {'package_schema': 1, 'skill_id': SKILL_ID, 'version': version,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'files': files(staged)}
        (staged / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        verify_package(staged)
        shutil.copytree(staged, package)
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in files(package):
            bundle.write(package / name, package.name + '/' + name)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip():
            raise ValueError('ZIP 文件校验失败')
    return {'version': version, 'folder': str(package), 'zip': str(archive),
            'zip_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'files': len(manifest['files']), 'verified': True}


def verify_package(package):
    manifest = json.loads((package / 'manifest.json').read_text())
    if manifest.get('package_schema') != 1 or manifest.get('skill_id') != SKILL_ID:
        raise ValueError('迁移包格式不匹配')
    expected = manifest['files']
    for name in expected:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or name == 'manifest.json':
            raise ValueError('迁移清单包含非法路径')
    actual = files(package)
    actual.pop('manifest.json', None)
    if actual != expected:
        raise ValueError('迁移文件缺失、内容变更或出现清单外文件，请重新导出原包')
    version = version_of(package / SKILL_ID)
    if manifest.get('version') != version:
        raise ValueError('迁移包与 Skill 版本不一致')
    return {'version': version, 'files': len(expected), 'verified': True}


def install_package(package, skills_dir, allow_downgrade=False):
    result = verify_package(package)
    source = package / SKILL_ID
    skills_dir = skills_dir.expanduser().resolve()
    target = skills_dir / SKILL_ID
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError('目标不是普通 Skill 目录，停止安装')
    old_version = version_of(target, allow_unversioned=True) if target.exists() else None
    if target.exists() and files(target) == files(source):
        return {**result, 'installed': str(target), 'status': 'unchanged'}
    if old_version == result['version']:
        raise ValueError('同版本内容不同：先比较、合并改动并增加版本号，不能直接覆盖')
    if (old_version and tuple(map(int, old_version.split('.'))) > tuple(map(int, result['version'].split('.')))
            and not allow_downgrade):
        raise ValueError('迁移包比当前版本旧；仅明确回滚时使用 --allow-downgrade')
    skills_dir.mkdir(parents=True, exist_ok=True)
    backup = None
    with tempfile.TemporaryDirectory(prefix='.eagle-install-', dir=skills_dir) as temporary:
        staged = Path(temporary) / SKILL_ID
        shutil.copytree(source, staged)
        if files(staged) != files(source):
            raise ValueError('暂存文件校验失败，未更改已安装 Skill')
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = skills_dir.parent / 'skill-backups' / SKILL_ID / (
                (old_version or 'unversioned') + '-' + stamp + '-' + uuid.uuid4().hex[:8])
            backup.parent.mkdir(parents=True, exist_ok=True)
            target.rename(backup)
        try:
            staged.rename(target)
        except Exception:
            if backup and not target.exists():
                backup.rename(target)
            raise
    return {**result, 'installed': str(target), 'status': 'installed',
            'previous_version': old_version, 'backup': str(backup) if backup else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    export.add_argument('--output-dir', type=Path, required=True)
    for command in ('verify', 'install'):
        action = sub.add_parser(command)
        action.add_argument('--package', type=Path, default=Path(__file__).resolve().parents[2])
        if command == 'install':
            codex_dir = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
            action.add_argument('--skills-dir', type=Path, default=codex_dir / 'skills')
            action.add_argument('--allow-downgrade', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'export':
            result = export_skill(args.source.resolve(), args.output_dir)
        elif args.command == 'verify':
            result = verify_package(args.package.resolve())
        else:
            result = install_package(args.package.resolve(), args.skills_dir, args.allow_downgrade)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, IndexError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
