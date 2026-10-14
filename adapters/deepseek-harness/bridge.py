"""Private JSON-lines bridge from DSH Cordis to SDK Runtime (development stack)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import subprocess
import sys
import fcntl

sys.path.insert(0, os.environ.get('BIANBU_MEMORY_SDK_PATH', '/usr/lib/python3/dist-packages'))
from bianbu_agent_sdk.memory.runtime import MemoryBridge
from bianbu_agent_sdk.memory.temporal import current_time_context
from bianbu_agent_sdk.memory.ontology import validate_memory_text

def main():
    state = Path(sys.argv[1])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (state / 'bridge.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    secret = state / 'account-secret'
    if not secret.exists():
        with os.fdopen(os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
            f.write(secrets.token_hex(32))
    socket = state / 'memory.sock'
    log = (state / 'memoryd.log').open('a')
    dev_confirmation = secrets.token_urlsafe(32)
    daemon = subprocess.Popen([
        '/usr/libexec/bianbu-agent-memoryd', '--development-identity',
        '--development-agent-name', 'deepseek-harness', '--socket', str(socket),
        '--database', str(state / 'memory.db'), '--key-package', str(state / 'memory.keypkg'),
        '--account-secret-file', str(secret), '--initialize-key-package',
        '--adapter-confirmation-secret-file', str(secret),
    ], env=dict(os.environ, AGENT_MEMORY_EMBEDDING_DISABLED='1',
                BIANBU_MEMORY_DEV_CONFIRM_TOKEN=dev_confirmation), stdout=log, stderr=log)
    loop = asyncio.new_event_loop()
    bridge = None
    try:
        deadline = time.monotonic() + 15
        while not socket.is_socket():
            if daemon.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('isolated memoryd startup failed')
            time.sleep(.05)
        bridge = MemoryBridge(socket_path=str(socket))
        for line in sys.stdin:
            request = json.loads(line)
            try:
                if request['op'] == 'clock':
                    result = current_time_context()
                elif request['op'] == 'recall':
                    records = loop.run_until_complete(bridge.recall_structured(request['query'], top_k=5, max_chars=6000))
                    result = [r.to_dict() for r in records]
                elif request['op'] == 'instructions':
                    client = loop.run_until_complete(bridge._client_or_none())
                    if client is None:
                        raise ConnectionError('memory_unavailable')
                    items = loop.run_until_complete(client.instructions.list(effective_only=True))
                    result = [{'text': i.text, 'scope': i.scope.value, 'priority': i.priority,
                               'revision': i.revision} for i in items[:32]]
                elif request['op'] == 'save':
                    result = loop.run_until_complete(bridge.upsert_explicit(
                        content=request['content'], target=request['target'],
                        run_id='dsh-tool:' + request['id'], session_id='dsh:' + request['session']))
                elif request['op'] == 'update':
                    client = loop.run_until_complete(bridge._client_or_none())
                    records = loop.run_until_complete(client.long_term.get_many([request['memory_id']]))
                    if not records:
                        raise ValueError('memory_not_found')
                    record = records[0]
                    from bianbu_agent_sdk.memory.types import MemoryKind
                    target = 'user' if record.kind in {MemoryKind.PROFILE, MemoryKind.PREFERENCE} else 'memory'
                    if request['target'] != target:
                        raise ValueError('target_mismatch')
                    from bianbu_agent_sdk.memory.temporal import normalize_relative_dates
                    content = normalize_relative_dates(request['content'].strip())
                    failure = validate_memory_text(request.get('target', 'memory'), content)
                    if failure:
                        raise ValueError(failure)
                    receipt = loop.run_until_complete(client.long_term.revise(
                        record.memory_id, text=content, expected_revision=int(request['expected_revision']),
                        summary=record.summary, tags=list(record.tags),
                        evidence=record.evidence,
                        idempotency_key='dsh-update:' + request['id']))
                    result = {'success': True, 'code': 'revised', 'memory_id': receipt.memory_id, 'revision': receipt.revision}
                elif request['op'] == 'delete_preview':
                    client = loop.run_until_complete(bridge._client_or_none())
                    records = loop.run_until_complete(client.long_term.get_many([request['memory_id']]))
                    if not records:
                        raise ValueError('memory_not_found')
                    record = records[0]
                    result = {'memory_id':record.memory_id,'text':record.text,'revision':record.revision}
                elif request['op'] == 'remove':
                    if not request.get('confirmed'):
                        raise ValueError('confirmation_required')
                    client = loop.run_until_complete(bridge._client_or_none())
                    records = loop.run_until_complete(client.long_term.get_many([request['memory_id']]))
                    if not records or records[0].revision != request['expected_revision']:
                        raise ValueError('confirmation_stale')
                    expiry = int(time.time()*1000)+60000
                    nonce = secrets.token_urlsafe(24)
                    payload = f'v1:{expiry}:{nonce}:long_term.forget'.encode()
                    token = dev_confirmation
                    job = loop.run_until_complete(client.long_term.forget(request['memory_id'],confirm_token=token,
                        reason_code='user_request',idempotency_key='dsh-remove:'+request['id']))
                    result = {'success':True,'code':'removed','job_id':job.job_id,'memory_id':request['memory_id']}
                elif request['op'] == 'turn':
                    user, assistant = request['user'], request['assistant']
                    digest = hashlib.sha256((user + '\0' + assistant).encode()).hexdigest()
                    result = loop.run_until_complete(bridge.record_turn(
                        run_id=f"dsh:{request['session']}:{request['turn']}:{digest}",
                        session_id='dsh:' + request['session'], agent_id='deepseek-harness',
                        user_text=user, assistant_text=assistant))
                    if not result:
                        raise RuntimeError('turn ingestion failed')
                elif request['op'] == 'run_started':
                    result = loop.run_until_complete(bridge.record_run_started(
                        run_id=request['run_id'], session_id='dsh:' + request['session'],
                        agent_id='deepseek-harness', user_text=request.get('user', '')))
                elif request['op'] == 'interrupted':
                    result = loop.run_until_complete(bridge.record_interrupted_turn(
                        run_id=request['run_id'], session_id='dsh:' + request['session'],
                        agent_id='deepseek-harness', user_text=request.get('user', ''),
                        status=request.get('status', 'interrupted'), error=request.get('error')))
                elif request['op'] == 'session':
                    result = True
                elif request['op'] == 'compress':
                    from bianbu_agent_sdk.memory import SessionSummaryPatch
                    client = loop.run_until_complete(bridge._client_or_none())
                    if client is None:
                        raise ConnectionError('memory_unavailable')
                    sid = 'dsh:' + request['session']
                    context = loop.run_until_complete(client.sessions.get_context(sid))
                    revision = int((context.get('summary') or {}).get('revision', 0))
                    # Host summary is untrusted, bounded, and never promoted to L3.
                    summary = request.get('summary', '')
                    from bianbu_agent_sdk.memory.runtime import _contains_credential
                    if _contains_credential(summary):
                        summary = '[host summary withheld: credential-like content]'
                    result = loop.run_until_complete(client.sessions.update_summary(sid,
                        patch=SessionSummaryPatch(current_status=summary[:2000] or 'DSH compaction boundary',
                            context_pointers=tuple(request.get('pointers', [])[:32])),
                        source_event_sequence=int(context.get('last_event_sequence', 0)),
                        expected_revision=revision, producer='deepseek-harness',
                        idempotency_key='dsh-compaction:' + request['compaction_id']))
                    loop.run_until_complete(client.runtime.prepare_checkpoint(sid,
                        idempotency_key='dsh-compaction-checkpoint:' + request['compaction_id']))
                    result = {'success': True, 'code': 'compaction_checkpointed',
                              'summary_revision': revision + 1}
                else:
                    raise ValueError('unsupported operation')
                response = {'id': request['id'], 'result': result}
            except Exception as error:
                response = {'id': request['id'], 'error': {
                    'type': type(error).__name__,
                    'code': getattr(error, 'code', None) or type(error).__name__.lower(),
                    'message': str(error) or type(error).__name__,
                }}
            print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        if bridge is not None:
            loop.run_until_complete(bridge.close())
        loop.close()
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()
        log.close()
        lock.close()


if __name__ == '__main__':
    main()
