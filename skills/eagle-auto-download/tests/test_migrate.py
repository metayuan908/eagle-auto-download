import json
import sys
import tempfile
import unittest
from pathlib import Path

from _support import WORK
from migrate import export_skill, install_package, verify_package


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / 'source'
        self.source.mkdir()
        (self.source / 'scripts').mkdir()
        (self.source / 'scripts' / 'collector.py').write_text('version = 1\n')
        self.set_version('1.0.0')
        self.output = self.directory / 'exports'
        self.output.mkdir()
        self.skills_dir = self.directory / 'codex' / 'skills'

    def set_version(self, version):
        (self.source / 'VERSION').write_text(version + '\n')
        (self.source / 'SKILL.md').write_text(
            '---\nname: eagle-auto-download\ndescription: test\nmetadata:\n  version: "' + version + '"\n---\n')
        (self.source / 'CHANGELOG.md').write_text(version + '\n')

    def export(self):
        return Path(export_skill(self.source, self.output)['folder'])

    def test_export_integrity_and_refusal_to_overwrite_or_accept_tampering(self):
        package = self.export()
        self.assertEqual(verify_package(package)['version'], '1.0.0')
        self.assertTrue((self.output / 'Eagle自动化下载_v1.0.0.zip').is_file())
        with self.assertRaisesRegex(ValueError, '已存在'):
            self.export()
        (package / 'eagle-auto-download' / 'scripts' / 'collector.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, '文件缺失、内容变更'):
            verify_package(package)

    def test_same_version_different_content_cannot_destroy_local_changes(self):
        package = self.export()
        first = install_package(package, self.skills_dir)
        self.assertEqual(first['status'], 'installed')
        self.assertEqual(install_package(package, self.skills_dir)['status'], 'unchanged')
        local = self.skills_dir / 'eagle-auto-download' / 'scripts' / 'collector.py'
        local.write_text('local unpublished change')
        with self.assertRaisesRegex(ValueError, '同版本内容不同'):
            install_package(package, self.skills_dir)
        self.assertEqual(local.read_text(), 'local unpublished change')

    def test_upgrade_backup_return_export_and_explicit_rollback(self):
        old = self.export()
        install_package(old, self.skills_dir)
        self.set_version('1.0.1')
        (self.source / 'scripts' / 'collector.py').write_text('version = 2\n')
        new = self.export()
        result = install_package(new, self.skills_dir)
        backup = Path(result['backup'])
        self.assertEqual((backup / 'VERSION').read_text().strip(), '1.0.0')
        installed = self.skills_dir / 'eagle-auto-download'
        returned_dir = self.directory / 'returned'; returned_dir.mkdir()
        returned = Path(export_skill(installed, returned_dir)['folder'])
        home_skills = self.directory / 'original-computer' / 'skills'
        install_package(old, home_skills)
        self.assertEqual(install_package(returned, home_skills)['version'], '1.0.1')
        with self.assertRaisesRegex(ValueError, '比当前版本旧'):
            install_package(old, self.skills_dir)
        rollback = install_package(old, self.skills_dir, allow_downgrade=True)
        self.assertEqual((Path(rollback['backup']) / 'VERSION').read_text().strip(), '1.0.1')
        self.assertEqual((installed / 'VERSION').read_text().strip(), '1.0.0')

    def test_version_mismatch_and_runtime_data_cannot_be_exported(self):
        (self.source / 'VERSION').write_text('1.0.1\n')
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.export()
        self.set_version('1.0.0')
        (self.source / '.env').write_text('private runtime data')
        with self.assertRaisesRegex(ValueError, '非迁移文件'):
            self.export()


if __name__ == '__main__':
    unittest.main()
