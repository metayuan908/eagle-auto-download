"""Offline acceptance checks shared by dry runs and every Eagle import."""
import re
from collections import Counter, defaultdict
from pathlib import Path

from media import sha256
from ledger import ACCEPTED


def pause_path(config):
    return config['_paths']['state_db'].with_suffix('.import-paused')


def external_mode(config):
    return config.get('collection', {}).get('external_video_mode')


def selected(record, mode):
    if mode == 'files' and record.get('media_type') == 'bookmark':
        fallback = record.get('download_fallback')
        fallback = fallback if isinstance(fallback, dict) else {}
        return (record.get('external_provider') == 'vimeo' and
                fallback.get('reason') in ('download_disabled', 'retry_limit_exhausted'))
    if (mode == 'bookmarks' and record.get('external_provider') and
            record.get('media_type') == 'video'):
        return False
    return True


def validate_queue(config, rows, verify_files=True, include_imported=False, target_ids=None):
    """No network or Eagle writes. Rows are (id, decoded record, status)."""
    issues, projects, labels = [], {}, defaultdict(set)
    mode = external_mode(config)
    checked_files = {}

    def issue(record, code, message):
        value = {'project_url': record['project_url'], 'code': code, 'message': message}
        if value not in issues:
            issues.append(value)

    # Keep imported video files in the lookup: a source need not be downloaded twice.
    videos = set()
    for _, record, _ in rows:
        name = record.get('project_display_name') or record['project_title']
        labels[(record['studio'], name)].add(record['project_url'].rstrip('/'))
        if record.get('media_type') == 'video' and record.get('external_provider'):
            for url in [record['asset_url']] + record.get('sources', []):
                videos.add((record['project_url'].rstrip('/'), url.rstrip('/')))

    counts = Counter()
    for asset_id, record, status in rows:
        if target_ids is not None and asset_id not in target_ids:
            continue
        if status in ACCEPTED and not include_imported:
            continue
        counts['checked'] += 1
        counts[status] += 1
        url = record['project_url'].rstrip('/')
        projects[url] = record
        name = record.get('project_display_name') or record['project_title']
        labels[(record['studio'], name)].add(url)
        if not re.search(r'[\u3400-\u9fff]', name) or not re.search(r'[A-Za-z]', name):
            issue(record, 'bilingual_name', '项目名称需为简短中英文组合；不能直接使用英文标题或 URL slug')
        if len(name) > 80:
            issue(record, 'name_too_long', '项目名称超过 80 字符，请缩短并保留客户与案例识别信息')
        if not record.get('category_tags') and record.get('category_status') != 'unknown':
            issue(record, 'category_tags', '缺少行业大类标签；确实无法判断时需在 metadata 中注明 category_status=unknown')

        source = record.get('source_description', '')
        summary = (record.get('project_summary') or '').strip()
        description_status = record.get('description_status')
        if description_status == 'present':
            if not source or not re.search(r'[\u3400-\u9fff]', summary):
                issue(record, 'chinese_summary', '原站有介绍：需保留来源原文，并填写整理后的中文项目简介')
        elif description_status == 'absent':
            if source or summary:
                issue(record, 'description_conflict', '标为无介绍，但仍存在原文或简介，需重新核对')
        else:
            issue(record, 'description_unreviewed', '尚未核实原站是否提供介绍；缺少简介不等于原站无介绍')
        if 'collector-id:' in summary or 'https://' in summary or 'http://' in summary:
            issue(record, 'annotation_provenance', '注释只放中文项目简介，技术标记和来源链接保留在本地')
        if record.get('media_scope_reviewed') is not True:
            issue(record, 'media_scope_unreviewed', '尚未审核正文素材归属与相关推荐边界')
        if record['studio'] == '2x4' and record.get('adapter_revision') != '2x4-main-v2':
            issue(record, 'stale_adapter', '旧版 2x4 素材可能混入相关推荐，需重新提取正文清单并对账')
        if record['studio'] == 'LANDOR 朗涛' and record.get('adapter_revision') != 'landor-content-v1':
            issue(record, 'stale_adapter', 'Landor 素材需按已核实的正文 CMS 内容块提取')

        if record.get('external_provider'):
            if record.get('external_provider') == 'adobe-ccv' and mode != 'files':
                issue(record, 'adobe_file_mode', 'Adobe CCV 当前只适配视频文件；请设置 external_video_mode: files')
            if mode not in ('files', 'bookmarks', 'both'):
                issue(record, 'external_mode', '请按用户要求设置 external_video_mode：files、bookmarks 或 both')
            if record.get('media_type') == 'bookmark' and mode in ('files', 'both'):
                fallback = record.get('download_fallback')
                fallback = fallback if isinstance(fallback, dict) else {}
                fallback_enabled = config.get('collection', {}).get('download_failure_fallback') == 'bookmarks'
                valid_fallback = (fallback_enabled and fallback.get('reason') in
                                  ('download_disabled', 'retry_limit_exhausted'))
                if (url, record['asset_url'].rstrip('/')) not in videos and not valid_fallback:
                    if fallback.get('reason') in ('download_disabled', 'retry_limit_exhausted') and not fallback_enabled:
                        issue(record, 'external_fallback_disabled', '记录要求以书签回退，但配置未启用此选项')
                    else:
                        issue(record, 'external_file_missing', '外链尚无已校验的视频文件；书签不能替代视频下载')
        if not selected(record, mode):
            counts['excluded_by_mode'] += 1
            continue
        counts[record.get('media_type', 'image')] += 1
        if verify_files and record.get('media_type') != 'bookmark':
            path = Path(record.get('local_path') or '')
            key = (str(path), record.get('sha256'))
            if key not in checked_files:
                try:
                    checked_files[key] = path.is_file() and sha256(path) == record.get('sha256')
                except OSError:
                    checked_files[key] = False
            if not checked_files[key]:
                issue(record, 'file_integrity', '本地文件不存在或哈希已变化，需重新准备')

    for (studio, name), urls in labels.items():
        if len(urls) > 1:
            for url in urls & projects.keys():
                issue(projects[url], 'folder_collision', '不同项目使用了同一文件夹名称，请保留简短案例区分信息')
    return {'ready': not issues and not pause_path(config).exists(),
            'paused': pause_path(config).exists(), 'projects': len(projects),
            'counts': dict(counts), 'issues': issues}


def validate_project_scope(config, rows, project_urls, verify_files=True, target_ids=None,
                           include_imported=False):
    """Validate selected pending projects while checking folder-name collisions globally."""
    selected_urls = {url.rstrip('/') for url in project_urls if url and url.strip()}
    if not selected_urls:
        raise ValueError('项目范围不能为空')
    known_urls = {record['project_url'].rstrip('/') for _, record, _ in rows}
    unknown = selected_urls - known_urls
    if unknown:
        raise ValueError('项目范围中包含状态库未登记的 URL: ' + ', '.join(sorted(unknown)))

    evidence = [(asset_id, record, status) for asset_id, record, status in rows
                if record['project_url'].rstrip('/') in selected_urls]
    report = validate_queue(config, evidence, verify_files=verify_files,
                            include_imported=include_imported, target_ids=target_ids)
    issues = [issue for issue in report['issues']
              if issue['project_url'].rstrip('/') in selected_urls]

    labels = defaultdict(set)
    records_by_url = {}
    for _, record, _ in rows:
        url = record['project_url'].rstrip('/')
        name = record.get('project_display_name') or record['project_title']
        labels[(record['studio'], name)].add(url)
        records_by_url[url] = record
    for (_, _), urls in labels.items():
        if len(urls) > 1:
            for url in urls & selected_urls:
                value = {'project_url': url, 'code': 'folder_collision',
                         'message': '不同项目使用了同一文件夹名称，请保留简短案例区分信息'}
                if value not in issues:
                    issues.append(value)

    report['projects'] = len(selected_urls)
    report['scope_project_urls'] = sorted(selected_urls)
    report['issues'] = issues
    report['ready'] = not issues and not pause_path(config).exists()
    return report
