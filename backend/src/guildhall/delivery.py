"""Explicit human delivery: immutable review snapshot, protected local files, fast-forward only."""
from pathlib import Path
import os
import fcntl
import subprocess
import tempfile
from . import gitutil
from .questmd import in_scope, scope_patterns


def pending_operation(repo):
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer'):
        path = Path(gitutil._run(['rev-parse', '--git-path', name], cwd=repo).stdout.strip())
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return name
    return None


def target_changes(repo):
    # Compare index and worktree separately: staged changes must not cancel out.
    paths = set()
    for args in (['diff', '--name-only', '--no-renames', '-z'],
                 ['diff', '--cached', '--name-only', '--no-renames', '-z']):
        paths.update(filter(None, gitutil._run(args, cwd=repo).stdout.split('\0')))
    return sorted(paths)


def local_collisions(repo, wt, base):
    # Include ignored files: Git otherwise permits overwriting them during merge.
    local = list(filter(None, gitutil._run(
        ['ls-files', '--others', '-z'], cwd=repo).stdout.split('\0')))
    incoming = gitutil.changed_paths(wt, base)
    insensitive = gitutil._run(['config', '--get', 'core.ignorecase'], cwd=repo, check=False).stdout.strip() == 'true'
    def key(path):
        return path.casefold() if insensitive else path
    return sorted(p for p in local if any(
        key(p) == key(q) or key(p).startswith(key(q) + '/') or key(q).startswith(key(p) + '/')
        for q in incoming))


def preview(store):
    state = store.read_state()
    repo, wt = Path(store.project), Path(state['worktree'])
    branch = gitutil._run(['symbolic-ref', '--quiet', '--short', 'HEAD'], cwd=repo, check=False).stdout.strip()
    reason = None
    blocking_files = []
    if not state.get('review_snapshot'):
        reason = '旧验收没有代码快照，请重新验收后交付'
    elif store.read_quest_md() != state.get('review_quest') or gitutil.tracked_snapshot(wt) != state['review_snapshot']:
        reason = '委托书或代码已在验收后变化，请重新验收'
    elif any(in_scope(p, scope_patterns(store.read_quest_md() or '')) for p in gitutil.untracked_paths(wt)):
        reason = '存在未纳入验收快照的新文件，请纳入 Git 后重新验收'
    elif not branch or branch != state.get('target_branch', branch):
        reason = '目标分支已改变或处于 detached HEAD'
    elif operation := pending_operation(repo):
        reason = f'目标仓库有未完成的 Git 操作（{operation}），请先完成或取消'
    elif blocking_files := target_changes(repo):
        reason = '目标工作区存在已跟踪文件或暂存区改动，请先处理'
    elif blocking_files := local_collisions(repo, wt, state['base_commit']):
        reason = '交付可能覆盖本地未跟踪或已忽略文件，请先移走冲突文件'
    elif gitutil.head_commit(repo) not in (state['base_commit'], state.get('delivery_commit')):
        reason = '目标分支已经前进，本期仅支持无冲突的快进合并'
    return {'target_branch': branch, 'base_commit': state.get('base_commit'),
            'ready': reason is None, 'reason': reason, 'blocking_files': blocking_files,
            'files': gitutil.changed_paths(wt, state['base_commit']) if state.get('base_commit') else []}


def accept(store):
    repo = Path(store.project)
    common = Path(gitutil._run(['rev-parse', '--git-common-dir'], cwd=repo).stdout.strip())
    if not common.is_absolute():
        common = repo / common
    # Serialize Guildhall deliveries across quests/processes sharing this repository.
    with (common / 'guildhall-delivery.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('同一仓库正在交付另一张需求单，请稍后重新检查') from None
        return _accept_locked(store)


def _accept_locked(store):
    info = preview(store)
    if not info['ready']:
        raise RuntimeError(info['reason'] + (': ' + ', '.join(info['blocking_files']) if info['blocking_files'] else ''))
    state = store.read_state()
    repo, wt = Path(store.project), Path(state['worktree'])
    commit = state.get('delivery_commit')
    if not commit:
        # Separate index: do not alter the adventurer's index or checkout.
        with tempfile.TemporaryDirectory(prefix='guildhall-delivery-') as tmp:
            env = {**os.environ, 'GIT_INDEX_FILE': str(Path(tmp) / 'index')}
            def git(*args):
                result = subprocess.run(['git', *args], cwd=wt, env=env, capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError(result.stderr.strip())
                return result.stdout.strip()
            git('read-tree', state['review_snapshot'].get('index') or 'HEAD')
            # Include every tracked/staged file, preserving staged additions.
            git('add', '-u')
            tree = git('write-tree')
            if gitutil.tracked_snapshot(wt) != state['review_snapshot']:
                raise RuntimeError('生成交付提交时验收代码发生变化，请重新验收')
            commit = git('commit-tree', tree, '-p', state['base_commit'], '-m', f'feat: deliver {store.quest_id}')
        # Durable receipt before merge allows retry if server stops immediately after merge.
        store.update_state(delivery_commit=commit, target_branch=info['target_branch'])
    # Recheck after preparing the receipt; external Git/editor activity is not locked.
    info = preview(store)
    if not info['ready']:
        raise RuntimeError(info['reason'])
    gitutil._run(['merge', '--ff-only', '--no-overwrite-ignore', commit], cwd=repo)
    if gitutil.head_commit(repo) != commit:
        raise RuntimeError('合并后 HEAD 与交付提交不一致')
    if target_changes(repo):
        raise RuntimeError('交付提交已合并，但工作区出现新的已跟踪改动，请检查')
    return commit
