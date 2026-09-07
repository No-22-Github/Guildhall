import asyncio
import json
from pathlib import Path
import pytest
from guildhall import delivery, flow, gitutil, questmd, store as st
from guildhall.runtime import QuestRuntime
from test_state_machine import _valid_quest_md


def valid_result(md):
    import re
    return {'checks': [{'index': i, 'step': re.sub(r'^\d+\.\s*', '', s), 'result': 'pass', 'evidence': 'pytest: exit 0'} for i,s in enumerate(questmd.acceptance_steps(md), 1)], 'touched_tests': False, 'out_of_scope_files': [], 'summary': '已执行全部检查'}

@pytest.mark.parametrize('change', ['empty', 'missing', 'duplicate', 'wrong_step', 'no_summary', 'bad_type', 'no_evidence'])
def test_appraisal_rejects_incomplete_contract(make_quest, change):
    md = _valid_quest_md(make_quest())
    result = valid_result(md)
    if change == 'empty': result['checks'] = []
    if change == 'missing': result['checks'].pop()
    if change == 'duplicate': result['checks'][1]['index'] = 1
    if change == 'wrong_step': result['checks'][1]['step'] = '相信实现者'
    if change == 'no_summary': result.pop('summary')
    if change == 'bad_type': result['touched_tests'] = 'false'
    if change == 'no_evidence': result['checks'][0]['evidence'] = ' '
    assert flow.parse_appraisal(json.dumps(result), md) is None


def test_appraisal_step_prefix_tolerates_multiline_and_ws(make_quest):
    md = _valid_quest_md(make_quest())
    result = valid_result(md)
    # appraiser 把命令块一并复制进 step,且空白重排:前缀匹配应放行
    result['checks'][0]['step'] += '\n\n     go build ./... && go test ./...\n\n   pass = 退出码全为 0。'
    assert flow.parse_appraisal(json.dumps(result), md) is not None
    # 标题行本身改写(非空白差异):仍拒绝
    rewritten = valid_result(md)
    rewritten['checks'][0]['step'] = '构建全绿(改写): go build'
    assert flow.parse_appraisal(json.dumps(rewritten), md) is None


def test_gates_and_globs(make_quest):
    md = _valid_quest_md(make_quest())
    assert questmd.acceptance_gate_error(md) is None
    assert questmd.acceptance_gate_error(md.replace('【负向】', ''))
    assert questmd.acceptance_gate_error(md.replace('Sources/PreenCore/Quant/**', '../**'))
    assert questmd.negative_step_violation('## 验收步骤\n1. 【负向】`git diff HEAD --exit-code`') is None
    assert questmd.in_scope('src/deep/a.py', ['src/**'])
    assert not questmd.in_scope('src/deep/a.py', ['src/*.py'])
    assert questmd.in_scope('src/a.py', ['src/**/*.py'])
    assert flow.detect_touched_tests('--- a/Tests/Quant.swift\n+++ b/Tests/Quant.swift')


def test_binary_and_head_snapshot(demo_repo):
    p = demo_repo / 'data.bin'
    p.write_bytes(b'\0one')
    gitutil._run(['add', '.'], cwd=demo_repo)
    gitutil._run(['commit', '-qm', 'binary'], cwd=demo_repo)
    before = gitutil.tracked_snapshot(demo_repo)
    p.write_bytes(b'\0two')
    assert gitutil.tracked_snapshot(demo_repo) != before
    gitutil._run(['add', '.'], cwd=demo_repo)
    gitutil._run(['commit', '-qm', 'changed'], cwd=demo_repo)
    assert gitutil.diff_head_sha256(demo_repo) == 'sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    assert gitutil.tracked_snapshot(demo_repo) != before


def prepare_delivery(make_quest, demo_repo):
    q = make_quest()
    state = q.read_state()
    wt = Path(state['worktree'])
    base = gitutil.create_worktree(demo_repo, q.quest_id, wt, state['branch'])
    (wt/'src/main.py').write_text('def main():\n    return 2\n')
    q.write_quest_md(_valid_quest_md(q))
    q.update_state(base_commit=base, review_snapshot=gitutil.tracked_snapshot(wt), review_quest=q.read_quest_md())
    return q, wt


def test_delivery_merges_reviewed_bytes_and_retry(make_quest, demo_repo):
    q, wt = prepare_delivery(make_quest, demo_repo)
    before = gitutil.tracked_snapshot(wt)
    commit = delivery.accept(q)
    assert gitutil.head_commit(demo_repo) == commit
    assert 'return 2' in (demo_repo/'src/main.py').read_text()
    assert gitutil.tracked_snapshot(wt) == before
    assert delivery.accept(q) == commit

@pytest.mark.parametrize('change', ['source', 'quest', 'dirty_target', 'advanced_target'])
def test_delivery_refuses_stale_or_dirty(make_quest, demo_repo, change):
    q, wt = prepare_delivery(make_quest, demo_repo)
    if change == 'source': (wt/'src/main.py').write_text('wrong')
    if change == 'quest': q.write_quest_md('different')
    if change == 'dirty_target': (demo_repo/'src/main.py').write_text('local changes')
    if change == 'advanced_target': gitutil._run(['commit', '--allow-empty', '-qm', 'advanced'], cwd=demo_repo)
    with pytest.raises(RuntimeError): delivery.accept(q)


@pytest.mark.asyncio
async def test_phase_reentry_rejected(make_quest):
    rt = QuestRuntime(make_quest(), None)
    ready = asyncio.Event()
    async def work(): await ready.wait()
    rt.start_phase(work)
    with pytest.raises(RuntimeError): rt.start_phase(work)
    ready.set()
    await rt.phase_task
    assert not rt.phase_busy()


def test_event_counter_across_store_instances(make_quest):
    q = make_quest()
    other = st.QuestStore(q.project, q.quest_id)
    assert q.append_event('appraiser', {'n': 0}) == 0
    assert other.append_event('appraiser', {'n': 1}) == 1
    assert q.append_event('appraiser', {'n': 2}) == 2


def test_sse_last_event_id_wins_over_initial_offset(client, make_quest):
    q = make_quest()
    client.post("/api/projects", json={"path": q.project})
    for i in range(3): q.append_event('appraiser', {'n': i})
    response = client.get(f'/api/quests/{q.quest_id}/events/appraiser?offset=0', headers={'Last-Event-ID': '1'})
    assert 'id: 0' not in response.text
    assert 'id: 1' not in response.text
    assert 'id: 2' in response.text


def test_api_mutation_gates(client, make_quest):
    q = make_quest()
    client.post("/api/projects", json={"path": q.project})
    md = _valid_quest_md(q)
    response = client.put(f'/api/quests/{q.quest_id}/quest', content=md.replace('【负向】', ''))
    assert response.status_code == 409
    assert not q.quest_md.exists()
    q.write_quest_md(md)
    q.transition('posted')
    assert client.put(f'/api/quests/{q.quest_id}/quest', content=md).status_code == 409
    assert client.post(f'/api/quests/{q.quest_id}/generate').status_code == 409


def test_new_file_delivery(make_quest, demo_repo):
    q, wt = prepare_delivery(make_quest, demo_repo)
    (wt/'new.py').write_text('new = True\n')
    gitutil._run(['add', 'new.py'], cwd=wt)
    q.update_state(review_snapshot=gitutil.tracked_snapshot(wt))
    delivery.accept(q)
    assert (demo_repo/'new.py').read_text() == 'new = True\n'


def test_staged_deletion_delivery(make_quest, demo_repo):
    q, wt = prepare_delivery(make_quest, demo_repo)
    (wt/'src/main.py').unlink()
    gitutil._run(['add', '-u'], cwd=wt)
    q.update_state(review_snapshot=gitutil.tracked_snapshot(wt))
    delivery.accept(q)
    assert not (demo_repo/'src/main.py').exists()


def test_allowed_test_change_is_warning_not_failure(client, demo_repo, tmp_guildhall, fake_acp):
    from test_api_flow import wired, _generate, _wait_state
    # Reuse standard scripts without invoking a fixture directly.
    from test_api_flow import QUEST_MD_TEMPLATE
    from fake_acp import chunk
    async def receptionist(text):
        if text.strip() == '生成需求单':
            return ([], QUEST_MD_TEMPLATE.format(qid='x',project='x').replace('- `src/**`','- `src/**`\n- `tests/**`'), 'end_turn')
        return ([], 'ready', 'end_turn')
    async def adventurer(text):
        q = next((tmp_guildhall/'worktrees').iterdir())
        (q/'tests/test_main.py').write_text('def test_main():\n    assert 1 == 1\n')
        return ([], 'done', 'end_turn')
    async def appraiser(text):
        md = text.split('<quest>')[1].split('</quest>')[0].strip()
        return ([], json.dumps(valid_result(md)), 'end_turn')
    for role, fn in [('receptionist',receptionist),('adventurer',adventurer),('appraiser',appraiser)]: fake_acp.set_script(role,fn)
    client.post('/api/projects',json={'path':str(demo_repo)})
    qid=client.post('/api/quests',json={'project':str(demo_repo),'message':'test'}).json()['id']
    assert _generate(client,qid)
    assert client.post(f'/api/quests/{qid}/transition',json={'to':'posted'}).status_code==200
    client.post(f'/api/quests/{qid}/transition',json={'to':'in_progress'})
    assert _wait_state(client,qid,{'appraised','disputed','failed'})=='appraised'
    assert client.get(f'/api/quests/{qid}/appraisal').json()['touched_tests'] is True


@pytest.mark.parametrize('ignored', [False, True])
def test_delivery_preserves_unrelated_local_data(make_quest, demo_repo, ignored):
    q, wt = prepare_delivery(make_quest, demo_repo)
    local = demo_repo / 'local data.jsonl'
    local.write_bytes(b'\x00local training data\n')
    if ignored:
        (demo_repo / '.git/info/exclude').write_text('local data.jsonl\n')
    assert delivery.preview(q)['ready']
    commit = delivery.accept(q)
    assert local.read_bytes() == b'\x00local training data\n'
    assert gitutil.head_commit(demo_repo) == commit
    assert delivery.accept(q) == commit


@pytest.mark.parametrize('shape', ['same', 'ancestor', 'descendant'])
@pytest.mark.parametrize('ignored', [False, True])
def test_delivery_blocks_local_path_collisions(make_quest, demo_repo, shape, ignored):
    q, wt = prepare_delivery(make_quest, demo_repo)
    incoming = 'new/file.txt' if shape == 'ancestor' else 'new'
    local = 'new/file.txt' if shape == 'descendant' else 'new'
    target = wt / incoming
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('delivered')
    gitutil._run(['add', '--', incoming], cwd=wt)
    q.update_state(review_snapshot=gitutil.tracked_snapshot(wt))
    path = demo_repo / local
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('local')
    if ignored:
        (demo_repo / '.git/info/exclude').write_text('new\n')
    before = gitutil.head_commit(demo_repo)
    assert delivery.preview(q)['blocking_files'] == [local]
    with pytest.raises(RuntimeError, match='覆盖'):
        delivery.accept(q)
    assert path.read_text() == 'local'
    assert gitutil.head_commit(demo_repo) == before


def test_delivery_rechecks_target_after_commit_creation(make_quest, demo_repo, monkeypatch):
    q, wt = prepare_delivery(make_quest, demo_repo)
    original = q.update_state
    def update(**kwargs):
        result = original(**kwargs)
        if 'delivery_commit' in kwargs:
            (demo_repo / 'src/main.py').write_text('external edit')
        return result
    monkeypatch.setattr(q, 'update_state', update)
    before = gitutil.head_commit(demo_repo)
    with pytest.raises(RuntimeError, match='已跟踪'):
        delivery.accept(q)
    assert gitutil.head_commit(demo_repo) == before
    assert (demo_repo / 'src/main.py').read_text() == 'external edit'


def test_delivery_blocks_staged_change_even_when_worktree_matches_head(make_quest, demo_repo):
    q, wt = prepare_delivery(make_quest, demo_repo)
    path = demo_repo / 'src/main.py'
    original = path.read_bytes()
    path.write_text('staged')
    gitutil._run(['add', 'src/main.py'], cwd=demo_repo)
    path.write_bytes(original)
    assert delivery.preview(q)['blocking_files'] == ['src/main.py']
    with pytest.raises(RuntimeError):
        delivery.accept(q)


def test_delivery_refuses_pending_merge_with_clean_files(make_quest, demo_repo):
    q, wt = prepare_delivery(make_quest, demo_repo)
    (demo_repo / '.git/MERGE_HEAD').write_text(gitutil.head_commit(demo_repo) + '\n')
    with pytest.raises(RuntimeError, match='未完成的 Git 操作'):
        delivery.accept(q)


def test_delivery_refuses_concurrent_delivery(make_quest, demo_repo):
    import fcntl
    q, wt = prepare_delivery(make_quest, demo_repo)
    with (demo_repo / '.git/guildhall-delivery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='正在交付'):
            delivery.accept(q)


def test_git_preserves_ignored_collision_created_after_last_preview(make_quest, demo_repo, monkeypatch):
    q, wt = prepare_delivery(make_quest, demo_repo)
    (wt / 'new').write_text('delivered')
    gitutil._run(['add', 'new'], cwd=wt)
    q.update_state(review_snapshot=gitutil.tracked_snapshot(wt))
    (demo_repo / '.git/info/exclude').write_text('new\n')
    original = gitutil._run
    def run(args, **kwargs):
        if args[0] == 'merge':
            (demo_repo / 'new').write_text('external local data')
        return original(args, **kwargs)
    monkeypatch.setattr(gitutil, '_run', run)
    before = gitutil.head_commit(demo_repo)
    with pytest.raises(RuntimeError):
        delivery.accept(q)
    assert gitutil.head_commit(demo_repo) == before
    assert (demo_repo / 'new').read_text() == 'external local data'
