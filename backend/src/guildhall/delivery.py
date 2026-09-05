"""Explicit human delivery: immutable review snapshot, clean target, fast-forward only."""
from pathlib import Path
import os
import subprocess
import tempfile
from . import gitutil
from .questmd import in_scope, scope_patterns


def preview(store):
    state = store.read_state()
    repo, wt = Path(store.project), Path(state['worktree'])
    branch = gitutil._run(['symbolic-ref', '--quiet', '--short', 'HEAD'], cwd=repo, check=False).stdout.strip()
    reason = None
    if not state.get('review_snapshot'):
        reason = '旧验收没有代码快照，请重新验收后交付'
    elif store.read_quest_md() != state.get('review_quest') or gitutil.tracked_snapshot(wt) != state['review_snapshot']:
        reason = '委托书或代码已在验收后变化，请重新验收'
    elif any(in_scope(p, scope_patterns(store.read_quest_md() or '')) for p in gitutil.untracked_paths(wt)):
        reason = '存在未纳入验收快照的新文件，请纳入 Git 后重新验收'
    elif not branch or branch != state.get('target_branch', branch):
        reason = '目标分支已改变或处于 detached HEAD'
    elif gitutil._run(['status', '--porcelain'], cwd=repo).stdout:
        reason = '目标工作区不干净，请先处理本地改动'
    elif gitutil.head_commit(repo) not in (state['base_commit'], state.get('delivery_commit')):
        reason = '目标分支已经前进，本期仅支持无冲突的快进合并'
    return {'target_branch': branch, 'base_commit': state.get('base_commit'),
            'ready': reason is None, 'reason': reason,
            'files': gitutil.changed_paths(wt, state['base_commit']) if state.get('base_commit') else []}


def accept(store):
    info = preview(store)
    if not info['ready']:
        raise RuntimeError(info['reason'])
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
            commit = git('commit-tree', tree, '-p', state['base_commit'], '-m', f'feat: deliver {store.quest_id}')
        # Durable receipt before merge allows retry if server stops immediately after merge.
        store.update_state(delivery_commit=commit, target_branch=info['target_branch'])
    gitutil._run(['merge', '--ff-only', commit], cwd=repo)
    if gitutil.head_commit(repo) != commit or gitutil._run(['status', '--porcelain'], cwd=repo).stdout:
        raise RuntimeError('合并后 HEAD 与交付提交不一致')
    return commit
